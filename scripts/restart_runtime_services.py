#!/usr/bin/env python3
"""Schedule a safe detached restart of Maestro's dedicated runtime services."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import urlopen


DEFAULT_RUNTIME_DIR = Path.home() / "Maestro-runtime"
DEFAULT_TAILSCALE_IP = "100.66.109.2"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--tailscale-ip", default=DEFAULT_TAILSCALE_IP)
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    if not (runtime_dir / ".git").exists():
        raise SystemExit(f"Runtime checkout not found: {runtime_dir}")
    if args.execute:
        return execute_restart(runtime_dir, delay=args.delay, tailscale_ip=args.tailscale_ip)
    return schedule_restart(runtime_dir, delay=args.delay, tailscale_ip=args.tailscale_ip)


def schedule_restart(runtime_dir: Path, *, delay: float, tailscale_ip: str) -> int:
    log_dir = Path.home() / "Library" / "Logs" / "Maestro"
    log_dir.mkdir(parents=True, exist_ok=True)
    supervisor_log = (log_dir / "runtime-restart.log").open("a", encoding="utf-8")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--execute",
        "--runtime-dir",
        str(runtime_dir),
        "--delay",
        str(max(0.5, delay)),
        "--tailscale-ip",
        tailscale_ip,
    ]
    process = subprocess.Popen(
        command,
        cwd=runtime_dir,
        stdin=subprocess.DEVNULL,
        stdout=supervisor_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    supervisor_log.close()
    print(
        json.dumps(
            {
                "status": "scheduled",
                "supervisor_pid": process.pid,
                "delay_seconds": max(0.5, delay),
                "runtime_dir": str(runtime_dir),
            }
        )
    )
    return 0


def execute_restart(runtime_dir: Path, *, delay: float, tailscale_ip: str) -> int:
    time.sleep(max(0.5, delay))
    _log(f"Restarting runtime at {runtime_dir}")
    for port in (8000, 5173):
        _stop_runtime_listener(port, runtime_dir)

    python = runtime_dir / ".venv" / "bin" / "python"
    alembic = runtime_dir / ".venv" / "bin" / "alembic"
    if not python.exists() or not alembic.exists():
        raise RuntimeError("Runtime virtual environment is unavailable.")
    subprocess.run([str(alembic), "upgrade", "head"], cwd=runtime_dir, check=True)

    log_dir = Path.home() / "Library" / "Logs" / "Maestro"
    log_dir.mkdir(parents=True, exist_ok=True)
    backend_log = (log_dir / "backend.log").open("a", encoding="utf-8")
    subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "app.api.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--reload",
        ],
        cwd=runtime_dir,
        stdin=subprocess.DEVNULL,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    backend_log.close()

    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is not available on PATH.")
    frontend_log = (log_dir / "frontend.log").open("a", encoding="utf-8")
    subprocess.Popen(
        [npm, "run", "dev", "--", "--host", "0.0.0.0"],
        cwd=runtime_dir / "frontend",
        env={**os.environ, "VITE_API_BASE_URL": f"http://{tailscale_ip}:8000"},
        stdin=subprocess.DEVNULL,
        stdout=frontend_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    frontend_log.close()

    backend_ok = _wait_for_url("http://127.0.0.1:8000/health", timeout=30)
    frontend_ok = _wait_for_url("http://127.0.0.1:5173", timeout=30)
    _log(f"Restart complete backend={backend_ok} frontend={frontend_ok}")
    return 0 if backend_ok and frontend_ok else 1


def _stop_runtime_listener(port: int, runtime_dir: Path) -> None:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    process_ids = {int(value) for value in result.stdout.split() if value.isdigit()}
    process_groups: set[int] = set()
    for process_id in process_ids:
        cwd = _process_cwd(process_id)
        if cwd is None or not _is_within(cwd, runtime_dir):
            _log(f"Leaving port {port} process {process_id} untouched; cwd={cwd}")
            continue
        process_groups.add(os.getpgid(process_id))
    for process_group in process_groups:
        if process_group == os.getpgrp():
            raise RuntimeError("Refusing to terminate the restart supervisor process group.")
        _log(f"Stopping process group {process_group} on port {port}")
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            continue
    if process_groups:
        time.sleep(2)
    for process_group in process_groups:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            continue
        _log(f"Force stopping process group {process_group}")
        os.killpg(process_group, signal.SIGKILL)


def _process_cwd(process_id: int) -> Path | None:
    result = subprocess.run(
        ["lsof", "-a", "-p", str(process_id), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    path_line = next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), "")
    return Path(path_line).resolve() if path_line else None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _wait_for_url(url: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def _log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
