# Sandbox image for generated code and QA's pytest runs (research R6).
# BASE_IMAGE and PYTEST_VERSION come from tools/python_exec.py as build arguments,
# so the pinned versions live in one place.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG PYTEST_VERSION
RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}"
