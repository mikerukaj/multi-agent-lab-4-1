# Contract: Container Deployment (Dockerfile + docker-compose.yml)

Research: R16, R17. Requirements: FR-227–FR-234, directive 6.

## Application image: `Dockerfile` (repository root)

| Property | Value |
|---|---|
| Base | `python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` (same as the sandbox base) |
| Docker CLI | `COPY --from=docker:26.1.3-cli@sha256:46f45f59366d443fec0ecb4a44fa4fb65f11b3305ca6b138edc176d8971e05b4 /usr/local/bin/docker /usr/local/bin/docker` |
| Python deps | `pip install --no-cache-dir -r requirements.txt` (all `==` pins; `pysqlite3-binary` kept for old-SQLite hosts) |
| Embedding model | a build step instantiates Chroma's `DefaultEmbeddingFunction` and embeds one string, so the ONNX model is cached in the image under `/home/app/.cache/chroma` |
| User | `app` (non-root). The UID and GID are overridden at run time to the host user (Compose `user:`). `HOME=/home/app` must be readable by that UID. |
| Workdir | `/app` (source copied; `sandbox/Dockerfile` included so `ensure_sandbox` can build the sandbox image through the host engine) |
| Entrypoint | `["python", "-m", "orchestration.team"]` |
| Stages | `app` (everything above), `test` (`FROM app`, adds `requirements-dev.txt`, i.e. `coverage`, for running the suite in the image), `runtime` (`FROM app`, last, the default target). Compose builds `runtime`, so the deployed image has no dev tools. |
| Excluded (`.dockerignore`) | `.env*`, `.git`, `workspace/`, `.chroma/`, `.cache/`, `docs/cost_reports/*.json`, `benchmarks/results/`, `specs/`, `.venv/`, `__pycache__/` |
| Secrets | none. `docker history` and the layers contain no key (SC-208 scan in the quickstart). |
| Network interface | none opened (FR-232) |

## `docker-compose.yml`

```yaml
# Pinned digests and health-check command as implemented (T081, research R16).
services:
  chroma:
    image: chromadb/chroma:1.5.9@sha256:1e0b73a187a28757c572acba508c46f48c9e8b0acaf5c20e6d95cdedce1acdf6
    volumes: [chroma-data:/data]
    environment: [ANONYMIZED_TELEMETRY=FALSE]
    healthcheck:
      # The pinned image has bash but no curl, wget or python3 (research R16).
      test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/8000 && printf 'GET /api/v2/heartbeat HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q nanosecond <&3"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s
    # no ports: published; reachable only on the compose network

  agent:
    build: {context: ., target: runtime}
    image: multi-agent-lab:local
    depends_on:
      chroma:
        condition: service_healthy        # directive 6: agent waits for Chroma
    env_file: [.env]                      # run-time secrets only (ANTHROPIC_API_KEY, LANGSMITH_*)
    environment:
      CHROMA_HOST: chroma
      CHROMA_PORT: "8000"
      CODER_WORKSPACE: ${PWD}/workspace
      QA_OUTPUT_ROOT: ${PWD}/workspace/.qa-out
      COST_REPORT_DIR: /app/docs/cost_reports
    user: "${HOST_UID:?set HOST_UID}:${HOST_GID:?set HOST_GID}"
    group_add: ["${DOCKER_GID:?set DOCKER_GID}"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock   # TRUST EXCEPTION (FR-233)
      - ${PWD}/workspace:${PWD}/workspace            # same absolute path: sandbox bind mounts resolve on the host
      - ${PWD}/docs/cost_reports:/app/docs/cost_reports
      - ${PWD}/benchmarks/results:/app/benchmarks/results
      - ${PWD}/.cache:/app/.cache
    command: ["${TEAM_REQUIREMENT:-<default realistic requirement>}"]

volumes:
  chroma-data:
```

### Invariants (checked by `tests/integration/test_compose_config.py` via `docker compose config`)

1. `agent.depends_on.chroma.condition == "service_healthy"`, and `chroma.healthcheck` is
   defined.
2. Only `agent` mounts `/var/run/docker.sock`. `chroma` has no engine access. Sandbox
   containers started by the agent never get it either (`tools/python_exec.py` passes no
   socket mount; covered by the existing sandbox tests).
3. No service's `environment` contains a value for `*KEY*`, `*TOKEN*`, `*SECRET*`,
   `*PASSWORD*` or `*CREDENTIAL*`. Secrets come only through `env_file`.
4. Every pulled image (the `chroma` service's `image:` and every Dockerfile `FROM` and
   `COPY --from` that names an external image) carries a `@sha256:` digest. The locally
   built `multi-agent-lab:local` tag and internal stage references (`FROM app AS test`,
   `FROM app AS runtime`) are exempt.
5. The workspace host path equals its container path.
6. No `ports:` on any service.
7. `agent.build.target == "runtime"`.

### Running the suite in the image (FR-238, US5 scenario 3)

```bash
docker build --target test -t multi-agent-lab:test .
docker run --rm --user "$HOST_UID:$HOST_GID" --group-add "$DOCKER_GID" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/workspace:$PWD/workspace" -e CODER_WORKSPACE="$PWD/workspace" \
  -e QA_OUTPUT_ROOT="$PWD/workspace/.qa-out" \
  --entrypoint scripts/test.sh multi-agent-lab:test
```

The mounts match the `agent` service, so Docker-gated tests run (or skip) exactly as on
the host. No API key is passed: the suite is offline.

### Waiting for Chroma (two layers)

1. Compose holds `agent` until the `chroma` health check passes.
2. `SemanticMemory` connects through the `chroma` `DependencyGuard`: up to 3 retries
   with backoff on connection errors. If Chroma is still unavailable, memory degrades
   (empty recall, skipped writes, `degraded` events) and the run continues (resilience
   contract).

### One-shot behaviour

`docker compose up --build --abort-on-container-exit --exit-code-from agent` runs one
pipeline: `agent` runs `TEAM_REQUIREMENT` and exits, Compose stops `chroma`, and the
command exits with the agent's exit code (FR-201a). Other forms:

- `docker compose run --rm agent "<requirement>"` runs another requirement;
- `docker compose run --rm agent --resume <run_id>` resumes a run;
- `docker compose run --rm --entrypoint python agent -m orchestration.health` runs the
  health check.

### Host prerequisites (documented in README "Deployment")

Docker Engine 24+ with Compose v2, and a `.env` containing `ANTHROPIC_API_KEY`,
`HOST_UID=$(id -u)`, `HOST_GID=$(id -g)` and
`DOCKER_GID=$(stat -c %g /var/run/docker.sock)`.

### Trust exception (README text requirement, FR-233)

The README must state the following:

- the `agent` container can control the host's Docker engine, which is equivalent to
  root on the host;
- only the agent, never Chroma and never a sandbox, gets the socket;
- how to limit the exposure: a dedicated host or VM, rootless Docker, or a
  socket-proxy that allows only `containers/create|start|wait|kill|delete`, `images`
  and `build`.

### Upgrade and removal

- **Upgrade**: `git pull && docker compose build --pull && docker compose up …`. The
  Chroma data volume persists. Bumping `chromadb` requires bumping the server image tag
  in step.
- **Removal**: `docker compose down -v` (removes `chroma-data`),
  `docker image rm multi-agent-lab:local multi-agent-sandbox:py3.12-pytest9.1.1`. The
  workspaces and reports on the host are the operator's to delete.
