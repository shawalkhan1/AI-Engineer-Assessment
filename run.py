#!/usr/bin/env python3
"""Portable launcher: python run.py --cases cases --output artifacts/run."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import venv

ROOT = Path(__file__).resolve().parent


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def bootstrap() -> Path:
    environment = Path(os.environ.get("AERLINK_VENV", str(ROOT / ".venv"))).resolve()
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        log("Creating project virtual environment.")
        venv.EnvBuilder(with_pip=True).create(environment)
    check = (
        "import importlib.metadata as m, pathlib; "
        "pins=[line.strip().split('==') for line in "
        "pathlib.Path('requirements.txt').read_text().splitlines() "
        "if line.strip() and not line.lstrip().startswith('#')]; "
        "assert all(m.version(name)==version for name,version in pins)"
    )
    if subprocess.run([str(executable), "-c", check], cwd=ROOT,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        log("Installing the pinned dependencies (network access required).")
        subprocess.run([str(executable), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
                       cwd=ROOT, check=True)
    return executable


def probe_service(base_url: str) -> str | None:
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
            data = json.load(response)
        return data.get("service", "unknown") if isinstance(data, dict) else "unknown"
    except (OSError, ValueError, urllib.error.URLError):
        return None


def start_server(config):
    """Return a process we own, or None when using an existing server."""
    service = probe_service(config.ops_base_url)
    if service == "aerlink-ops":
        log("Using the existing operations server; it will remain running.")
        return None
    if service:
        raise RuntimeError("The configured port belongs to an unrecognised service.")
    parsed = urlsplit(config.ops_base_url)
    host, port = parsed.hostname, parsed.port or 80
    if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Start the configured operations API before running the worker.")
    try:
        with socket.create_connection((host, port), timeout=1):
            raise RuntimeError("The configured port is occupied but has no valid Aerlink health response.")
    except (ConnectionRefusedError, socket.timeout):
        pass
    server_env = dict(os.environ)
    server_env.update(OPS_HOST=host, OPS_PORT=str(port), OPS_API_KEY=config.ops_api_key)
    (ROOT / "state").mkdir(exist_ok=True)
    # The stdlib-only server uses the base interpreter. Windows venv launchers spawn
    # a child; terminating that launcher otherwise leaves the actual server alive.
    interpreter = getattr(sys, "_base_executable", sys.executable)
    with (ROOT / "state" / "ops-server.log").open("w", encoding="utf-8") as server_log:
        process = subprocess.Popen(
            [interpreter, str(ROOT / "env" / "ops_server.py")], cwd=ROOT,
            env=server_env, stdout=server_log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        deadline = time.monotonic() + float(os.environ.get("AERLINK_HEALTH_TIMEOUT_S", "30"))
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The operations server exited during startup; see state/ops-server.log.")
            if probe_service(config.ops_base_url) == "aerlink-ops":
                log("Started the operations server at {}.".format(config.ops_base_url))
                return process
            time.sleep(0.2)
        raise RuntimeError("The operations server did not become healthy before the startup deadline.")
    except BaseException:
        stop_server(process)
        raise


def stop_server(process) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run(argv: list[str]) -> int:
    from aerlink.cli import build_parser, main as worker_main
    from aerlink.config import ConfigError, load_config, redact

    if "--test" in argv:
        test_args = [arg for arg in argv if arg != "--test"]
        return subprocess.call([sys.executable, "-m", "pytest", str(ROOT / "tests"), *test_args], cwd=ROOT)
    args = build_parser().parse_args(argv)
    if args.dry_run and args.reset_ops:
        log("configuration error: --dry-run cannot be combined with --reset-ops.")
        return 2
    config = None
    server = None
    try:
        config = load_config(require_openai_key=not args.no_model)
        server = start_server(config)
        return worker_main(argv)
    except (ConfigError, RuntimeError, OSError) as exc:
        secrets = (config.openai_api_key, config.ops_api_key) if config else ()
        log("configuration error: " + redact(str(exc), *secrets))
        return 2
    finally:
        if server is not None:
            stop_server(server)
            log("Stopped the operations server started by this command.")


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        log("Python 3.11 or newer is required.")
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        executable = bootstrap()
        if Path(sys.prefix).resolve() != executable.parent.parent.resolve():
            return subprocess.call([str(executable), str(Path(__file__).resolve()), *args])
        return run(args)
    except (OSError, subprocess.CalledProcessError) as exc:
        log("setup error: {}".format(type(exc).__name__))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
