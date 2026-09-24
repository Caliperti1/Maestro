from __future__ import annotations

from pathlib import Path
from typing import Any

from maestro_node.api import LeasedJob
from maestro_node.config import NodeConfig, NodePaths
from maestro_node.journal import ExecutionJournal
from maestro_node.runner import NodeRunner
from maestro_node.security import create_device_key


class FakeAPI:
    def __init__(self) -> None:
        self.completions: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.renewals = 0
        self.closed = False

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.heartbeats.append(payload)
        return {"status": "active"}

    def lease(self, payload: dict[str, Any], *, wait_seconds: int):
        return None

    def renew(self, job: LeasedJob) -> dict[str, Any]:
        self.renewals += 1
        return {"renewed": True}

    def complete(self, job: LeasedJob, payload: dict[str, Any]) -> dict[str, Any]:
        self.completions.append(payload)
        return {"accepted": True}

    def fail(self, job: LeasedJob, payload: dict[str, Any]) -> dict[str, Any]:
        self.failures.append(payload)
        return {"accepted": True}

    def close(self) -> None:
        self.closed = True


def make_runner(tmp_path: Path) -> tuple[NodeRunner, FakeAPI]:
    paths = NodePaths(tmp_path)
    create_device_key(paths.private_key)
    config = NodeConfig(
        api_base_url="https://example.test",
        node_id="node-1",
        display_name="Test Mac",
        credential="credential",
        private_key_path=str(paths.private_key),
    )
    api = FakeAPI()
    runner = NodeRunner(
        config,
        paths,
        api=api,  # type: ignore[arg-type]
        journal=ExecutionJournal(paths.journal),
    )
    return runner, api


def job(capability: str = "diagnostic.echo") -> LeasedJob:
    return LeasedJob(
        job_id="job-1",
        idempotency_key="idem-1",
        capability=capability,
        input_envelope={"value": "hello"},
        lease_token="lease-token",
        lease_generation=1,
        lease_seconds=30,
    )


def test_diagnostic_echo_is_completed_and_signed(tmp_path) -> None:
    runner, api = make_runner(tmp_path)
    try:
        runner.process_job(job())
    finally:
        runner.close()

    assert not api.failures
    assert api.completions[0]["result_envelope"]["result"] == {"value": "hello"}
    assert api.completions[0]["result_envelope"]["result_digest"]
    assert api.completions[0]["signature"]


def test_replayed_job_returns_journaled_result(tmp_path) -> None:
    runner, api = make_runner(tmp_path)
    try:
        runner.process_job(job())
        runner.process_job(job())
    finally:
        runner.close()

    assert len(api.completions) == 2
    assert api.completions[0]["result_envelope"]["result"] == api.completions[1][
        "result_envelope"
    ]["result"]


def test_unlisted_capability_is_rejected_without_execution(tmp_path) -> None:
    runner, api = make_runner(tmp_path)
    try:
        runner.process_job(job("local.shell"))
    finally:
        runner.close()

    assert not api.completions
    assert api.failures == [
        {
            "error_code": "capability_not_allowed",
            "message": "Capability 'local.shell' is not installed.",
            "retryable": False,
        }
    ]


def test_reused_idempotency_key_for_different_job_is_rejected(tmp_path) -> None:
    runner, api = make_runner(tmp_path)
    first = job()
    second = LeasedJob(
        job_id="job-2",
        idempotency_key=first.idempotency_key,
        capability=first.capability,
        input_envelope={"value": "different"},
        lease_token="lease-token-2",
        lease_generation=1,
        lease_seconds=30,
    )
    try:
        runner.process_job(first)
        runner.process_job(second)
    finally:
        runner.close()

    assert len(api.completions) == 1
    assert api.failures == [
        {
            "error_code": "idempotency_conflict",
            "message": "The idempotency key belongs to a different local execution.",
            "retryable": False,
        }
    ]


def test_run_once_sends_heartbeat_and_nonblocking_poll(tmp_path) -> None:
    runner, api = make_runner(tmp_path)
    try:
        runner.run(once=True)
    finally:
        runner.close()

    assert len(api.heartbeats) == 1
    assert api.heartbeats[0]["capabilities"] == [
        {"key": "diagnostic.echo", "version": "1.0.0", "mode": "read"}
    ]
