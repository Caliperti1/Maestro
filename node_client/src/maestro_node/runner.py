"""Long-polling node runtime."""

from __future__ import annotations

import logging
import os
import platform
import random
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .api import LeasedJob, NodeAPI
from .capabilities import CapabilityError, execute, manifest
from .config import NodeConfig, NodePaths
from .journal import ExecutionJournal
from .security import sign_payload

logger = logging.getLogger("maestro_node")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LeaseRenewer:
    def __init__(self, api: NodeAPI, job: LeasedJob) -> None:
        self.api = api
        self.job = job
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> LeaseRenewer:
        interval = max(5.0, self.job.lease_seconds / 3)

        def renew_loop() -> None:
            while not self.stop_event.wait(interval):
                try:
                    self.api.renew(self.job)
                except Exception:
                    logger.exception("Lease renewal failed for job %s", self.job.job_id)

        self.thread = threading.Thread(target=renew_loop, name="maestro-lease-renew", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


class NodeRunner:
    def __init__(
        self,
        config: NodeConfig,
        paths: NodePaths,
        *,
        api: NodeAPI | None = None,
        journal: ExecutionJournal | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.paths = paths
        self.api = api or NodeAPI.from_config(config)
        self.journal = journal or ExecutionJournal(paths.journal)
        self.sleep = sleep
        self.stop_event = threading.Event()
        self.started_at = now_iso()
        self.last_heartbeat_at: str | None = None
        self.last_job_at: str | None = None

    def close(self) -> None:
        self.api.close()
        self.journal.close()

    def stop(self) -> None:
        self.stop_event.set()

    def heartbeat_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.config.protocol_version,
            "node_id": self.config.node_id,
            "client_version": __version__,
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "capabilities": manifest(self.config.capabilities),
            "started_at": self.started_at,
            "sent_at": now_iso(),
        }

    def write_runtime_state(self, *, state: str, detail: str | None = None) -> None:
        self.paths.write_runtime_state(
            {
                "state": state,
                "detail": detail,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_job_at": self.last_job_at,
                "updated_at": now_iso(),
            }
        )

    def send_heartbeat(self) -> None:
        self.api.heartbeat(self.heartbeat_payload())
        self.last_heartbeat_at = now_iso()
        self.write_runtime_state(state="connected")

    def process_job(self, job: LeasedJob) -> None:
        existing = self.journal.start(
            job_id=job.job_id,
            idempotency_key=job.idempotency_key,
            capability=job.capability,
            lease_generation=job.lease_generation,
        )
        if existing and (
            existing.job_id != job.job_id or existing.capability != job.capability
        ):
            logger.error(
                "Idempotency conflict for job %s: journal belongs to job %s (%s)",
                job.job_id,
                existing.job_id,
                existing.capability,
            )
            self.api.fail(
                job,
                {
                    "error_code": "idempotency_conflict",
                    "message": "The idempotency key belongs to a different local execution.",
                    "retryable": False,
                },
            )
            return
        if existing and existing.status == "completed" and existing.result is not None:
            logger.info("Returning journaled result for replayed job %s", job.job_id)
            self._complete(job, existing.result, existing.result_digest)
            return
        if existing:
            error = {
                "error_code": "duplicate_execution_uncertain",
                "message": "This idempotency key has an unfinished local execution.",
                "retryable": False,
            }
            self.api.fail(job, error)
            return

        with LeaseRenewer(self.api, job):
            try:
                result = execute(job.capability, job.input_envelope)
            except CapabilityError as exc:
                self.journal.fail(job.idempotency_key, error_code=exc.code)
                self.api.fail(
                    job,
                    {"error_code": exc.code, "message": str(exc), "retryable": exc.retryable},
                )
                return
            except Exception as exc:
                logger.exception("Job %s failed locally", job.job_id)
                self.journal.fail(job.idempotency_key, error_code="local_execution_error")
                self.api.fail(
                    job,
                    {
                        "error_code": "local_execution_error",
                        "message": type(exc).__name__,
                        "retryable": True,
                    },
                )
                return

        # Journal the local result before reporting it. If completion transport fails, the main
        # loop reconnects and a repeated lease returns this exact result without executing again.
        entry = self.journal.complete(job.idempotency_key, result)
        self._complete(job, result, entry.result_digest)
        self.last_job_at = now_iso()

    def _complete(
        self, job: LeasedJob, result: dict[str, Any], result_digest: str | None
    ) -> None:
        envelope = {
            "protocol_version": self.config.protocol_version,
            "node_id": self.config.node_id,
            "job_id": job.job_id,
            "idempotency_key": job.idempotency_key,
            "capability": job.capability,
            "result_schema_version": "1",
            "result": result,
            "result_digest": result_digest,
            "completed_at": now_iso(),
        }
        signature = sign_payload(Path(self.config.private_key_path), envelope)
        self.api.complete(job, {"result_envelope": envelope, "signature": signature})

    def run(self, *, once: bool = False, wait_seconds: int = 25) -> None:
        backoff = 1.0
        heartbeat_interval = 20.0
        next_heartbeat = 0.0
        self.write_runtime_state(state="starting")
        while not self.stop_event.is_set():
            try:
                monotonic_now = time.monotonic()
                if monotonic_now >= next_heartbeat:
                    self.send_heartbeat()
                    next_heartbeat = monotonic_now + heartbeat_interval
                job = self.api.lease(
                    {
                        "protocol_version": self.config.protocol_version,
                        "node_id": self.config.node_id,
                        "capabilities": [
                            item["key"] for item in manifest(self.config.capabilities)
                        ],
                        "wait_seconds": 0 if once else wait_seconds,
                    },
                    wait_seconds=0 if once else wait_seconds,
                )
                if job:
                    self.process_job(job)
                backoff = 1.0
                if once:
                    return
            except (httpx.HTTPError, OSError) as exc:
                if once:
                    raise
                delay = min(60.0, backoff) * random.uniform(0.8, 1.2)
                logger.warning("Control plane unavailable (%s); retrying in %.1fs", exc, delay)
                self.write_runtime_state(state="disconnected", detail=type(exc).__name__)
                self.stop_event.wait(delay)
                backoff = min(60.0, backoff * 2)
            except Exception as exc:
                if once:
                    raise
                logger.exception("Node loop error; retrying")
                self.write_runtime_state(state="degraded", detail=type(exc).__name__)
                self.stop_event.wait(5.0)
