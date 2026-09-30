from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ExecutionNode,
    NodeCapability,
    NodeEnrollmentToken,
    NodeJob,
    NodeJobEvent,
)
from app.db.repositories import (
    ExecutionNodeRepository,
    NodeCapabilityRepository,
    NodeEnrollmentTokenRepository,
    NodeJobEventRepository,
    NodeJobRepository,
)
from app.nodes.security import issue_token, token_hash, token_matches, token_resource_id


class NodeServiceError(Exception):
    status_code = 400


class NodeAuthenticationError(NodeServiceError):
    status_code = 401


class NodeAuthorizationError(NodeServiceError):
    status_code = 403


class NodeNotFoundError(NodeServiceError):
    status_code = 404


class NodeConflictError(NodeServiceError):
    status_code = 409


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NodeService:
    OFFLINE_AFTER = timedelta(seconds=90)

    def __init__(self, session: Session):
        self.session = session
        self.nodes = ExecutionNodeRepository(session)
        self.capabilities = NodeCapabilityRepository(session)
        self.enrollment_tokens = NodeEnrollmentTokenRepository(session)
        self.jobs = NodeJobRepository(session)
        self.events = NodeJobEventRepository(session)

    def create_enrollment_token(
        self,
        *,
        expires_in_seconds: int = 900,
        allowed_capabilities: Sequence[str] = (),
        created_by_user_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[NodeEnrollmentToken, str]:
        now = utcnow()
        enrollment = NodeEnrollmentToken(
            expires_at=now + timedelta(seconds=expires_in_seconds),
            token_hash="pending",
            token_hint="pending",
            allowed_capabilities=sorted(set(allowed_capabilities)),
            created_by_user_id=created_by_user_id,
            metadata_=metadata or {},
        )
        self.session.add(enrollment)
        self.session.flush()
        raw_token = issue_token("mne", enrollment.id)
        enrollment.token_hash = token_hash(raw_token)
        enrollment.token_hint = raw_token[-8:]
        self.session.commit()
        self.session.refresh(enrollment)
        return enrollment, raw_token

    def enroll(
        self,
        *,
        enrollment_code: str,
        display_name: str,
        platform: str,
        client_version: str | None,
        public_key: str | None,
        capabilities: Sequence[str | dict[str, Any]],
    ) -> tuple[ExecutionNode, str]:
        token_id = token_resource_id(enrollment_code, prefix="mne")
        if token_id is None:
            raise NodeAuthenticationError("Invalid enrollment code.")
        enrollment = self.enrollment_tokens.get_for_update(token_id)
        now = utcnow()
        if enrollment is None or not token_matches(enrollment_code, enrollment.token_hash):
            raise NodeAuthenticationError("Invalid enrollment code.")
        if enrollment.used_at is not None:
            raise NodeConflictError("Enrollment code has already been used.")
        if enrollment.revoked_at is not None:
            raise NodeAuthenticationError("Enrollment code has been revoked.")
        if _aware(enrollment.expires_at) <= now:
            raise NodeAuthenticationError("Enrollment code has expired.")

        normalized = self.normalize_capabilities(capabilities)
        allowed = set(enrollment.allowed_capabilities or [])
        requested = {item["key"] for item in normalized}
        if allowed and not requested.issubset(allowed):
            disallowed = ", ".join(sorted(requested - allowed))
            raise NodeAuthorizationError(f"Enrollment code does not allow: {disallowed}")

        node_id = uuid.uuid4()
        access_token = issue_token("mnd", node_id)
        node = ExecutionNode(
            id=node_id,
            display_name=display_name,
            platform=platform,
            public_key=public_key,
            credential_hash=token_hash(access_token),
            status="online",
            client_version=client_version,
            enrolled_at=now,
            credential_issued_at=now,
            last_seen_at=now,
            last_heartbeat_at=now,
        )
        self.session.add(node)
        self.session.flush()
        self._sync_capabilities(node, normalized, now=now)
        enrollment.used_at = now
        self.session.commit()
        self.session.refresh(node)
        return node, access_token

    def authenticate(self, access_token: str | None, *, touch: bool = True) -> ExecutionNode:
        if not access_token:
            raise NodeAuthenticationError("Missing node bearer token.")
        node_id = token_resource_id(access_token, prefix="mnd")
        node = self.nodes.get(node_id) if node_id else None
        if node is None or not token_matches(access_token, node.credential_hash):
            raise NodeAuthenticationError("Invalid node bearer token.")
        if node.revoked_at is not None or node.status == "revoked":
            raise NodeAuthenticationError("Node credential has been revoked.")
        if touch:
            node.last_seen_at = utcnow()
            self.session.commit()
        return node

    def heartbeat(
        self,
        node: ExecutionNode,
        *,
        client_version: str | None = None,
        capabilities: Sequence[str | dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionNode:
        now = utcnow()
        node.last_seen_at = now
        node.last_heartbeat_at = now
        if node.status != "paused":
            node.status = "online"
        if client_version is not None:
            node.client_version = client_version
        if metadata is not None:
            node.metadata_ = metadata
        if capabilities is not None:
            self._sync_capabilities(node, self.normalize_capabilities(capabilities), now=now)
        self.session.commit()
        self.session.refresh(node)
        return node

    def list_nodes(self) -> Sequence[ExecutionNode]:
        return self.nodes.list_recent()

    def get_node(self, node_id: uuid.UUID) -> ExecutionNode:
        node = self.nodes.get(node_id)
        if node is None:
            raise NodeNotFoundError("Unknown execution node.")
        return node

    def revoke_node(self, node_id: uuid.UUID) -> ExecutionNode:
        node = self.get_node(node_id)
        now = utcnow()
        node.status = "revoked"
        node.revoked_at = now
        node.last_seen_at = now
        self.session.commit()
        return node

    def set_paused(self, node_id: uuid.UUID, *, paused: bool) -> ExecutionNode:
        node = self.get_node(node_id)
        if node.revoked_at is not None:
            raise NodeConflictError("A revoked node cannot be resumed.")
        node.status = "paused" if paused else "offline"
        self.session.commit()
        return node

    def create_diagnostic_job(
        self, node_id: uuid.UUID, *, message: str, idempotency_key: str | None = None
    ) -> NodeJob:
        node = self.get_node(node_id)
        if node.revoked_at is not None:
            raise NodeConflictError("Cannot enqueue work for a revoked node.")
        if idempotency_key:
            existing = self.session.scalar(
                select(NodeJob).where(NodeJob.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if existing.node_id != node.id:
                    raise NodeConflictError(
                        "The idempotency key is already bound to a different node."
                    )
                return existing
        now = utcnow()
        status = "queued" if self.effective_status(node, now=now) == "online" else "waiting_for_node"
        job = NodeJob(
            node_id=node.id,
            capability_key="diagnostic.echo",
            job_type="diagnostic.echo",
            status=status,
            priority=100,
            idempotency_key=idempotency_key,
            input_envelope={"value": message},
            available_at=now,
        )
        self.session.add(job)
        self.session.flush()
        if job.idempotency_key is None:
            job.idempotency_key = f"node-job:{job.id}"
        self._event(job, "created", None, status, {"source": "owner_api"})
        self.session.commit()
        self.session.refresh(job)
        return job

    def lease_job(
        self,
        node: ExecutionNode,
        *,
        advertised_capabilities: Sequence[str],
        lease_seconds: int = 60,
    ) -> tuple[NodeJob, str] | None:
        if node.status == "paused":
            return None
        now = utcnow()
        self._release_expired_leases(node.id, now=now)
        registered = {
            capability.capability_key
            for capability in self.capabilities.list_by_node(node.id)
            if capability.status == "ready"
        }
        eligible = sorted(registered.intersection(advertised_capabilities))
        job = self.jobs.claim_candidate(node_id=node.id, capabilities=eligible, now=now)
        if job is None:
            self.session.commit()
            return None
        lease_token = issue_token("mjl", job.id)
        previous = job.status
        job.status = "leased"
        job.lease_token_hash = token_hash(lease_token)
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.attempt_count += 1
        job.started_at = job.started_at or now
        job.error_message = None
        self._event(
            job,
            "leased",
            previous,
            "leased",
            {"attempt": job.attempt_count, "lease_seconds": lease_seconds},
        )
        self.session.commit()
        self.session.refresh(job)
        return job, lease_token

    def renew_job(
        self,
        node: ExecutionNode,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        lease_seconds: int,
        lease_generation: int | None = None,
    ) -> NodeJob:
        job = self._leased_job(node, job_id, lease_token)
        self._validate_lease_generation(job, lease_generation)
        now = utcnow()
        if job.lease_expires_at is None or _aware(job.lease_expires_at) <= now:
            raise NodeConflictError("Job lease has expired.")
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self._event(job, "lease_renewed", "leased", "leased", {"lease_seconds": lease_seconds})
        self.session.commit()
        self.session.refresh(job)
        return job

    def complete_job(
        self,
        node: ExecutionNode,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        output_envelope: dict[str, Any],
        lease_generation: int | None = None,
    ) -> NodeJob:
        job = self._job_for_completion(node, job_id, lease_token)
        self._validate_lease_generation(job, lease_generation)
        if job.status == "completed":
            return job
        now = utcnow()
        previous = job.status
        job.status = "completed"
        job.output_envelope = output_envelope
        job.completed_at = now
        job.lease_expires_at = now
        self._event(job, "completed", previous, "completed", {})
        self.session.commit()
        self.session.refresh(job)
        return job

    def fail_job(
        self,
        node: ExecutionNode,
        job_id: uuid.UUID,
        *,
        lease_token: str,
        error_message: str,
        retryable: bool = False,
        lease_generation: int | None = None,
        error_code: str | None = None,
    ) -> NodeJob:
        job = self._job_for_completion(node, job_id, lease_token)
        self._validate_lease_generation(job, lease_generation)
        if job.status == "failed":
            return job
        now = utcnow()
        previous = job.status
        retry = retryable and job.attempt_count < job.max_attempts
        job.status = "waiting_for_node" if retry else "failed"
        job.error_message = error_message
        job.lease_expires_at = None
        if retry:
            job.lease_token_hash = None
            job.available_at = now
        else:
            job.completed_at = now
        self._event(
            job,
            "retry_scheduled" if retry else "failed",
            previous,
            job.status,
            {
                "error_code": error_code,
                "error_message": error_message,
                "retryable": retry,
            },
        )
        self.session.commit()
        self.session.refresh(job)
        return job

    def effective_status(self, node: ExecutionNode, *, now: datetime | None = None) -> str:
        if node.status in {"revoked", "paused"}:
            return node.status
        if node.last_heartbeat_at is None:
            return "offline"
        if _aware(node.last_heartbeat_at) < (now or utcnow()) - self.OFFLINE_AFTER:
            return "offline"
        return node.status

    def node_payload(self, node: ExecutionNode) -> dict[str, Any]:
        pending = self.session.scalar(
            select(func.count(NodeJob.id)).where(
                NodeJob.node_id == node.id,
                NodeJob.status.in_(["queued", "waiting_for_node"]),
            )
        )
        active = self.session.scalar(
            select(func.count(NodeJob.id)).where(
                NodeJob.node_id == node.id, NodeJob.status == "leased"
            )
        )
        return {
            "id": str(node.id),
            "display_name": node.display_name,
            "platform": node.platform,
            "status": node.status,
            "effective_status": self.effective_status(node),
            "client_version": node.client_version,
            "enrolled_at": node.enrolled_at,
            "last_seen_at": node.last_seen_at,
            "last_heartbeat_at": node.last_heartbeat_at,
            "revoked_at": node.revoked_at,
            "capabilities": [self.capability_payload(item) for item in self.capabilities.list_by_node(node.id)],
            "pending_job_count": int(pending or 0),
            "active_job_count": int(active or 0),
        }

    @staticmethod
    def capability_payload(capability: NodeCapability) -> dict[str, Any]:
        return {
            "key": capability.capability_key,
            "version": capability.version,
            "status": capability.status,
            "details": capability.details,
            "last_seen_at": capability.last_seen_at,
        }

    @staticmethod
    def job_payload(job: NodeJob, *, include_envelopes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(job.id),
            "node_id": str(job.node_id),
            "capability_key": job.capability_key,
            "job_type": job.job_type,
            "status": job.status,
            "priority": job.priority,
            "idempotency_key": job.idempotency_key,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "available_at": job.available_at,
            "lease_expires_at": job.lease_expires_at,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "error_message": job.error_message,
        }
        if include_envelopes:
            payload.update(
                input_envelope=job.input_envelope,
                output_envelope=job.output_envelope,
            )
        return payload

    @staticmethod
    def event_payload(event: NodeJobEvent) -> dict[str, Any]:
        return {
            "id": str(event.id),
            "job_id": str(event.job_id),
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "payload": event.payload,
            "created_at": event.created_at,
        }

    @staticmethod
    def normalize_capabilities(
        capabilities: Sequence[str | dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for item in capabilities:
            value = {"key": item} if isinstance(item, str) else dict(item)
            key = str(value.get("key") or "").strip()
            if not key:
                raise NodeServiceError("Each capability requires a non-empty key.")
            normalized[key] = {
                "key": key,
                "version": value.get("version"),
                "status": value.get("status") or "ready",
                "details": value.get("details") or {},
            }
        return list(normalized.values())

    def _sync_capabilities(
        self, node: ExecutionNode, capabilities: Sequence[dict[str, Any]], *, now: datetime
    ) -> None:
        existing = {item.capability_key: item for item in self.capabilities.list_by_node(node.id)}
        incoming = {item["key"]: item for item in capabilities}
        for key, value in incoming.items():
            capability = existing.get(key)
            if capability is None:
                capability = NodeCapability(node_id=node.id, capability_key=key)
                self.session.add(capability)
            capability.version = value["version"]
            capability.status = value["status"]
            capability.details = value["details"]
            capability.last_seen_at = now
        for key, capability in existing.items():
            if key not in incoming:
                capability.status = "unavailable"

    def _release_expired_leases(self, node_id: uuid.UUID, *, now: datetime) -> None:
        for job in self.jobs.expired_leases(node_id, now=now):
            previous = job.status
            exhausted = job.attempt_count >= job.max_attempts
            job.status = "failed" if exhausted else "waiting_for_node"
            job.error_message = "Node job lease expired."
            job.lease_token_hash = None
            job.lease_expires_at = None
            if exhausted:
                job.completed_at = now
            self._event(job, "lease_expired", previous, job.status, {"exhausted": exhausted})

    def _leased_job(
        self, node: ExecutionNode, job_id: uuid.UUID, lease_token: str
    ) -> NodeJob:
        job = self.jobs.get(job_id)
        if job is None or job.node_id != node.id:
            raise NodeNotFoundError("Unknown node job.")
        if job.status != "leased":
            raise NodeConflictError(f"Node job is {job.status}, not leased.")
        if not token_matches(lease_token, job.lease_token_hash):
            raise NodeAuthenticationError("Invalid job lease token.")
        return job

    def _job_for_completion(
        self, node: ExecutionNode, job_id: uuid.UUID, lease_token: str
    ) -> NodeJob:
        job = self.jobs.get(job_id)
        if job is None or job.node_id != node.id:
            raise NodeNotFoundError("Unknown node job.")
        if not token_matches(lease_token, job.lease_token_hash):
            raise NodeAuthenticationError("Invalid job lease token.")
        if job.status not in {"leased", "completed", "failed"}:
            raise NodeConflictError(f"Node job is {job.status}, not leased.")
        if job.status == "leased":
            now = utcnow()
            if job.lease_expires_at is None or _aware(job.lease_expires_at) <= now:
                raise NodeConflictError("Job lease has expired.")
        return job

    def _event(
        self,
        job: NodeJob,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.session.add(
            NodeJobEvent(
                job_id=job.id,
                node_id=job.node_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                payload=payload,
                created_at=utcnow(),
            )
        )

    @staticmethod
    def _validate_lease_generation(job: NodeJob, lease_generation: int | None) -> None:
        if lease_generation is not None and lease_generation != job.attempt_count:
            raise NodeConflictError("Job lease generation is stale.")
