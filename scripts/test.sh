#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
coverage run -m unittest discover -s tests -t . -v
coverage report --fail-under=85
