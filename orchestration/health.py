"""Check everything a team run needs, without a model call (FR-231, research R17).

``python -m orchestration.health [--json]`` reports each item as ``ok``, ``missing``,
``error`` or ``info``, with a message, and exits 0 when every item that isn't ``info``
is ``ok`` (2 otherwise). ``orchestration.team`` runs the same checks before a run and
refuses to start, naming every failing item, before any model call (US4 scenario 4).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict

from dotenv import load_dotenv

from agents.coder_agent import CoderConfig
from agents.pm_agent import PMConfig
from agents.qa_agent import QAConfig
from cost.pricing import is_priced, load_price_table, price_table_path
from cost.report import cost_report_directory
from orchestration.services import budget_from_env
from tools.python_exec import SANDBOX_DIR, SANDBOX_IMAGE

ItemStatus = Literal["ok", "missing", "error", "info"]
DOCKER_TIMEOUT_SECONDS = 10
HEARTBEAT_TIMEOUT_SECONDS = 3


class HealthItem(TypedDict):
    """One prerequisite and its state (data-model §2.6)."""

    name: str
    status: ItemStatus
    message: str


class HealthReport(TypedDict):
    """Every prerequisite checked; ``ok`` when nothing but ``info`` items failed."""

    ok: bool
    items: list[HealthItem]


def check_health(env: Mapping[str, str], coder_config, qa_config, pm_config) -> HealthReport:
    """Check every prerequisite of a run; never calls a model."""
    models = [pm_config.model, coder_config.model, qa_config.model]
    items = [
        check_credentials(env),
        check_docker(),
        check_sandbox_image(qa_config.sandbox_image),
        check_writable("workspace", coder_config.workspace_root),
        check_writable("cost_report_dir", cost_report_directory(env)),
        check_chroma(coder_config),
        check_price_table(env, models),
        check_langsmith(env),
    ]
    ok = all(item["status"] in ("ok", "info") for item in items)
    return HealthReport(ok=ok, items=items)


def check_credentials(env: Mapping[str, str]) -> HealthItem:
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return HealthItem(name="credentials", status="ok", message="model credentials set")
    return HealthItem(name="credentials", status="missing",
                      message="set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN)")


def check_docker() -> HealthItem:
    if shutil.which("docker") is None:
        return HealthItem(name="docker", status="missing",
                          message="the docker command is not installed")
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return HealthItem(name="docker", status="error",
                          message="the Docker engine did not answer in time")
    if result.returncode != 0:
        return HealthItem(name="docker", status="error",
                          message=f"the Docker engine is not reachable: {result.stderr.strip()}")
    return HealthItem(name="docker", status="ok",
                      message=f"Docker {result.stdout.strip()} reachable")


def check_sandbox_image(image: str) -> HealthItem:
    if shutil.which("docker") is None:
        return HealthItem(name="sandbox_image", status="error",
                          message="cannot check the sandbox image without Docker")
    inspect = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                             text=True, timeout=DOCKER_TIMEOUT_SECONDS, check=False)
    if inspect.returncode == 0:
        return HealthItem(name="sandbox_image", status="ok", message=f"{image} present")
    if image == SANDBOX_IMAGE and (SANDBOX_DIR / "Dockerfile").is_file():
        return HealthItem(name="sandbox_image", status="ok",
                          message=f"{image} will be built from {SANDBOX_DIR}")
    return HealthItem(name="sandbox_image", status="missing",
                      message=f"{image} is not present and cannot be built here")


def check_writable(name: str, folder: Path) -> HealthItem:
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".health-"):
            pass
    except OSError as error:
        return HealthItem(name=name, status="error", message=f"{folder} is not writable: {error}")
    return HealthItem(name=name, status="ok", message=f"{folder} is writable")


def check_chroma(coder_config) -> HealthItem:
    if not coder_config.chroma_host:
        item = check_writable("chroma", coder_config.chroma_path)
        if item["status"] == "ok":
            item["message"] = f"embedded store at {coder_config.chroma_path} (one run at a time)"
        return item
    url = f"http://{coder_config.chroma_host}:{coder_config.chroma_port}/api/v2/heartbeat"
    try:
        with urllib.request.urlopen(url, timeout=HEARTBEAT_TIMEOUT_SECONDS) as response:
            response.read()
    except (urllib.error.URLError, OSError) as error:
        return HealthItem(name="chroma", status="error",
                          message=f"the Chroma server at {url} is not reachable: {error}")
    return HealthItem(name="chroma", status="ok", message=f"Chroma server at {url} healthy")


def check_price_table(env: Mapping[str, str], models: list[str]) -> HealthItem:
    """Prices for every configured model; a gap only blocks a run that has a budget."""
    path = price_table_path(env)
    try:
        table = load_price_table(path)
    except (OSError, ValueError) as error:
        return HealthItem(name="price_table", status="error",
                          message=f"cannot read the price table {path}: {error}")
    unpriced = sorted({model for model in models if not is_priced(model, table)})
    if not unpriced:
        return HealthItem(name="price_table", status="ok",
                          message=f"prices for every model (table {table['version']})")
    message = f"no price for {', '.join(unpriced)}: its cost is reported as unknown"
    if budget_from_env(env) is not None:
        return HealthItem(name="price_table", status="missing",
                          message=f"{message}, so a budget can't be enforced (FR-224)")
    return HealthItem(name="price_table", status="info", message=message)


def check_langsmith(env: Mapping[str, str]) -> HealthItem:
    on = env.get("LANGSMITH_TRACING", "").lower() == "true" and env.get("LANGSMITH_API_KEY")
    message = "tracing on" if on else "tracing off (set LANGSMITH_TRACING and a key to enable)"
    return HealthItem(name="langsmith", status="info", message=message)


def failing_items(report: HealthReport) -> list[str]:
    """``name: message`` for every item that blocks a run."""
    return [f"{item['name']}: {item['message']}" for item in report["items"]
            if item["status"] not in ("ok", "info")]


def main(argv: list[str] | None = None) -> int:
    """Print the health report; 0 when every prerequisite is met, 2 otherwise."""
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m orchestration.health",
                                     description="Check what a team run needs.")
    parser.add_argument("--json", action="store_true", help="print a JSON HealthReport")
    args = parser.parse_args(argv)
    try:
        report = check_health(os.environ, CoderConfig.from_env(), QAConfig.from_env(),
                              PMConfig.from_env())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["items"]:
            print(f"{item['status']:<8}{item['name']:<16}{item['message']}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
