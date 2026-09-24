"""HTTP client for the Maestro node protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import NodeConfig


class ProtocolError(RuntimeError):
    """Raised for a successful HTTP response with an invalid protocol payload."""


@dataclass(frozen=True)
class LeasedJob:
    job_id: str
    idempotency_key: str
    capability: str
    input_envelope: dict[str, Any]
    lease_token: str
    lease_generation: int
    lease_seconds: int
    correlation_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LeasedJob:
        try:
            input_envelope = value.get("input_envelope", {})
            if not isinstance(input_envelope, dict):
                raise TypeError("input_envelope must be an object")
            return cls(
                job_id=str(value["job_id"]),
                idempotency_key=str(value["idempotency_key"]),
                capability=str(value["capability"]),
                input_envelope=input_envelope,
                lease_token=str(value["lease_token"]),
                lease_generation=int(value.get("lease_generation", 1)),
                lease_seconds=max(10, int(value.get("lease_seconds", 60))),
                correlation_id=(
                    str(value["correlation_id"]) if value.get("correlation_id") else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("Malformed leased job response.") from exc


class NodeAPI:
    def __init__(
        self,
        base_url: str,
        *,
        credential: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": "maestro-node/0.1.0", "Accept": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        self.client = client or httpx.Client(headers=headers, timeout=httpx.Timeout(30.0))
        if client:
            self.client.headers.update(headers)

    @classmethod
    def from_config(cls, config: NodeConfig, *, client: httpx.Client | None = None) -> NodeAPI:
        return cls(config.api_base_url, credential=config.credential, client=client)

    def close(self) -> None:
        self.client.close()

    def enroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"{self.base_url}/nodes/enroll", json=payload)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Enrollment response must be an object.")
        return value

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"{self.base_url}/node/v1/heartbeat", json=payload)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Heartbeat response must be an object.")
        return value

    def lease(self, payload: dict[str, Any], *, wait_seconds: int) -> LeasedJob | None:
        timeout = httpx.Timeout(max(30.0, float(wait_seconds + 10)))
        response = self.client.post(
            f"{self.base_url}/node/v1/jobs/lease", json=payload, timeout=timeout
        )
        response.raise_for_status()
        value = response.json()
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ProtocolError("Lease response must be an object or null.")
        job_value = value.get("job", value)
        if job_value is None:
            return None
        if not isinstance(job_value, dict):
            raise ProtocolError("Lease response job must be an object or null.")
        return LeasedJob.from_dict(job_value)

    def renew(self, job: LeasedJob) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/node/v1/jobs/{job.job_id}/renew",
            json={
                "lease_token": job.lease_token,
                "lease_generation": job.lease_generation,
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Lease renewal response must be an object.")
        return value

    def complete(self, job: LeasedJob, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/node/v1/jobs/{job.job_id}/complete",
            json={
                "lease_token": job.lease_token,
                "lease_generation": job.lease_generation,
                **payload,
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Completion response must be an object.")
        return value

    def fail(self, job: LeasedJob, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/node/v1/jobs/{job.job_id}/fail",
            json={
                "lease_token": job.lease_token,
                "lease_generation": job.lease_generation,
                **payload,
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ProtocolError("Failure response must be an object.")
        return value

