# Application image for the PM -> Coder <-> QA team
# (specs/003-production-hardening/contracts/deployment.md).
#
# Stages: "app" (the application), "test" (app + coverage, to run the suite in the
# image) and "runtime" (last, so the default target: the deployed image has no dev
# tools). Every image is pinned by digest. No secret is copied in: credentials arrive at
# run time through Compose's env_file (FR-228).

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS app

# Only the static Docker CLI: the app starts sandboxes through the host's engine (FR-233).
COPY --from=docker:26.1.3-cli@sha256:46f45f59366d443fec0ecb4a44fa4fb65f11b3305ca6b138edc176d8971e05b4 \
     /usr/local/bin/docker /usr/local/bin/docker

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --home-dir /home/app --uid 10001 app
COPY . .

# Bake Chroma's embedding model into the image, so no download happens at run time.
USER app
RUN python -c "import agents.memory; from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warm-up'])"
# Compose runs the container as the host user, so any UID must be able to read HOME.
USER root
RUN chmod -R a+rX /home/app
USER app
ENTRYPOINT ["python", "-m", "orchestration.team"]

FROM app AS test
USER root
RUN pip install --no-cache-dir -r requirements-dev.txt
USER app

FROM app AS runtime
