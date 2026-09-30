"""Command-line entry point for the Maestro node."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import platform
import signal
import socket
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .api import NodeAPI, ProtocolError
from .capabilities import manifest
from .config import ConfigurationError, NodeConfig, NodePaths
from .runner import NodeRunner
from .security import create_device_key, remove_device_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro-node", description="Run a Maestro local node.")
    parser.add_argument("--data-dir", type=Path, help="Override the local node data directory.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll = subparsers.add_parser("enroll", help="Enroll this computer with Maestro.")
    enroll.add_argument("--url", required=True, help="Maestro control-plane base URL.")
    enroll.add_argument("--code", help="Single-use enrollment code (prompted if omitted).")
    enroll.add_argument("--name", default=socket.gethostname(), help="Node display name.")

    run = subparsers.add_parser("run", help="Run the node worker in the foreground.")
    run.add_argument("--once", action="store_true", help="Poll once and exit (for diagnostics).")
    run.add_argument("--poll-seconds", type=int, default=25, help="Long-poll duration.")
    run.add_argument("--verbose", action="store_true")

    status = subparsers.add_parser("status", help="Show local enrollment and runtime status.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def enroll_node(args: argparse.Namespace, paths: NodePaths) -> int:
    if paths.config.exists() or paths.private_key.exists():
        raise ConfigurationError(
            f"A node is already configured in {paths.data_dir}; refusing to replace its identity."
        )
    code = args.code or getpass.getpass("Enrollment code: ")
    if not code.strip():
        raise ConfigurationError("Enrollment code cannot be empty.")
    public_key = create_device_key(paths.private_key)
    api = NodeAPI(args.url)
    try:
        response = api.enroll(
            {
                "enrollment_code": code.strip(),
                "display_name": args.name,
                "platform": platform.platform(),
                "hostname": socket.gethostname(),
                "client_version": __version__,
                "protocol_version": "1",
                "public_key": public_key,
                "capabilities": manifest(("diagnostic.echo",)),
            }
        )
        node_id = str(response["node_id"])
        credential_value = response.get("refresh_token") or response.get("access_token")
        if not credential_value:
            raise ProtocolError("Enrollment response did not include a node credential.")
        config = NodeConfig(
            api_base_url=str(response.get("api_base_url") or args.url).rstrip("/"),
            node_id=node_id,
            display_name=args.name,
            credential=str(credential_value),
            private_key_path=str(paths.private_key),
        )
        paths.save_config(config)
    except Exception:
        remove_device_key(paths.private_key)
        raise
    finally:
        api.close()
    print(f"Enrolled {args.name!r} as node {node_id}.")
    print(f"Configuration stored in {paths.data_dir}.")
    return 0


def run_node(args: argparse.Namespace, paths: NodePaths) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = paths.load_config()
    runner = NodeRunner(config, paths)

    def request_stop(_signum: int, _frame: object) -> None:
        runner.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(f"Maestro node {config.display_name!r} connecting to {config.api_base_url}")
    try:
        runner.run(once=args.once, wait_seconds=max(1, min(args.poll_seconds, 55)))
    finally:
        runner.write_runtime_state(state="stopped")
        runner.close()
    return 0


def node_status(args: argparse.Namespace, paths: NodePaths) -> int:
    config = paths.load_config()
    runtime = paths.read_runtime_state()
    value = {
        "node_id": config.node_id,
        "display_name": config.display_name,
        "api_base_url": config.api_base_url,
        "capabilities": list(config.capabilities),
        "runtime": runtime or {"state": "not_started"},
        "data_dir": str(paths.data_dir),
    }
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(f"Node:         {config.display_name} ({config.node_id})")
        print(f"Control plane: {config.api_base_url}")
        print(f"State:        {value['runtime'].get('state', 'unknown')}")
        print(f"Capabilities: {', '.join(config.capabilities) or 'none'}")
        last_heartbeat = value["runtime"].get("last_heartbeat_at")
        if last_heartbeat:
            print(f"Last heartbeat: {last_heartbeat}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = NodePaths(args.data_dir.expanduser().resolve() if args.data_dir else None)
    try:
        if args.command == "enroll":
            return enroll_node(args, paths)
        if args.command == "run":
            return run_node(args, paths)
        if args.command == "status":
            return node_status(args, paths)
        parser.error(f"Unknown command: {args.command}")
    except (ConfigurationError, ProtocolError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

