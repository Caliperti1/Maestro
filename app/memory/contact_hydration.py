"""Resumable, low-cost historical Gmail hydration for contacts and organizations."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.identity import is_maestro_user_reference, maestro_user_identity
from app.db.models import (
    Agent,
    Contact,
    ContactHydrationCandidate,
    ContactHydrationJob,
    Domain,
    Entity,
    RuntimeSetting,
    Task,
)
from app.llm.client import LLMClient, LLMClientError, OllamaLLMClient, OpenAILLMClient
from app.memory.contact_intelligence import ContactEmbeddingService
from app.memory.organization_intelligence import OrganizationEmbeddingService
from app.memory.routed_hygiene import RoutedHygieneService
from app.memory.routed_service import RoutedMemoryService
from app.prompts import load_prompt
from app.tools.runtime import ToolExecutionRequest, ToolExecutionService


ACTIVE_JOB_STATUSES = {"pending", "scanning", "enriching", "promoting"}
TERMINAL_JOB_STATUSES = {"complete", "failed", "cancelled"}
PERSONAL_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}
AUTOMATED_LOCAL_PARTS = {
    "admin",
    "alerts",
    "billing",
    "calendar-notification",
    "donotreply",
    "hello",
    "info",
    "mailer-daemon",
    "news",
    "newsletter",
    "no-reply",
    "noreply",
    "notifications",
    "support",
}
PERSON_NAME_STOP_TOKENS = {
    "army",
    "civ",
    "ctr",
    "mil",
    "navy",
    "usaf",
    "usarmy",
    "usmc",
    "usn",
    "usa",
}
PERSON_TITLE_TOKENS = {
    "1lt",
    "2lt",
    "capt",
    "captain",
    "col",
    "cpt",
    "dr",
    "gen",
    "lt",
    "ltc",
    "maj",
    "mr",
    "mrs",
    "ms",
    "sgm",
}


class ContactHydrationError(RuntimeError):
    pass


class ContactHydrationService:
    def __init__(
        self,
        session: Session,
        *,
        tool_service: ToolExecutionService | None = None,
        llm_factory: Callable[[str], LLMClient] | None = None,
    ):
        self.session = session
        self.tool_service = tool_service or ToolExecutionService(session)
        self.llm_factory = llm_factory or _llm_client_for_profile

    def create_job(
        self,
        *,
        domain_id: uuid.UUID,
        query: str,
        page_size: int = 25,
        max_messages: int = 200,
        max_contacts: int = 100,
        enable_enrichment: bool = True,
        enable_cloud_fallback: bool = False,
        max_cloud_calls: int = 0,
        local_model_profile: str | None = None,
        cloud_model_profile: str | None = None,
    ) -> ContactHydrationJob:
        agent = self._gmail_agent(domain_id)
        task = Task(
            domain_id=domain_id,
            assigned_agent_id=agent.id,
            status="running",
            priority="low",
            source_type="contact_hydration",
            workflow_key="contact-hydration",
            objective="Hydrate contact and organization intelligence from historical Gmail evidence.",
            input_payload={"query": query},
        )
        self.session.add(task)
        self.session.flush()
        settings = get_settings()
        job = ContactHydrationJob(
            domain_id=domain_id,
            task_id=task.id,
            query=query.strip() or "in:sent newer_than:90d",
            mode="shadow",
            status="pending",
            page_size=max(1, min(page_size, 50)),
            max_messages=max(1, min(max_messages, 10000)),
            max_contacts=max(1, min(max_contacts, 5000)),
            local_model_profile=local_model_profile or settings.llm_qwen_model_profile,
            cloud_model_profile=cloud_model_profile or settings.llm_terra_model_profile,
            enable_enrichment=enable_enrichment,
            enable_cloud_fallback=enable_cloud_fallback,
            max_cloud_calls=max(0, min(max_cloud_calls, 1000)),
            config={"agent_key": agent.key, "max_threads_per_contact": 2},
            stats={"gmail_pages": 0, "local_calls": 0, "local_failures": 0, "cloud_failures": 0},
        )
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def process_once(self, *, owner: str = "contact-hydration-worker") -> ContactHydrationJob | None:
        job = self._claim_job(owner)
        if job is None:
            return None
        job_id = job.id
        task_id = job.task_id
        try:
            if job.status in {"pending", "scanning"}:
                self._scan_page(job)
            elif job.status == "enriching":
                self._enrich_next_candidate(job)
            elif job.status == "promoting":
                self._promote_batch(job)
            job.error_message = None
        except Exception as exc:
            self.session.rollback()
            job = self.session.get(ContactHydrationJob, job_id)
            if job is None:
                raise
            job.status = "failed"
            job.error_message = str(exc)
            task = self.session.get(Task, task_id) if task_id else None
            if task is not None:
                task.status = "failed"
                task.error_message = str(exc)
        finally:
            job.lease_owner = None
            job.lease_expires_at = None
            self.session.commit()
        return job

    def list_jobs(self, *, domain_id: uuid.UUID | None = None, limit: int = 20) -> list[ContactHydrationJob]:
        statement = select(ContactHydrationJob)
        if domain_id is not None:
            statement = statement.where(ContactHydrationJob.domain_id == domain_id)
        return list(
            self.session.scalars(statement.order_by(ContactHydrationJob.created_at.desc()).limit(limit))
        )

    def candidates(
        self,
        job_id: uuid.UUID,
        *,
        candidate_type: str | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[ContactHydrationCandidate]:
        statement = select(ContactHydrationCandidate).where(ContactHydrationCandidate.job_id == job_id)
        if candidate_type:
            statement = statement.where(ContactHydrationCandidate.candidate_type == candidate_type)
        if status:
            statement = statement.where(ContactHydrationCandidate.status == status)
        return list(
            self.session.scalars(
                statement.order_by(
                    ContactHydrationCandidate.confidence.desc(),
                    ContactHydrationCandidate.display_name,
                ).limit(limit)
            )
        )

    def update_job_status(self, job_id: uuid.UUID, action: str) -> ContactHydrationJob:
        job = self._job(job_id)
        if action == "pause" and job.status not in TERMINAL_JOB_STATUSES:
            job.stats = {**(job.stats or {}), "paused_from": job.status}
            job.status = "paused"
        elif action == "resume" and job.status == "paused":
            job.status = str((job.stats or {}).get("paused_from") or "scanning")
        elif action == "cancel" and job.status not in TERMINAL_JOB_STATUSES:
            job.status = "cancelled"
            job.completed_at = datetime.now(UTC)
            task = self.session.get(Task, job.task_id) if job.task_id else None
            if task is not None:
                task.status = "cancelled"
                task.completed_at = job.completed_at
        else:
            raise ContactHydrationError(f"Cannot {action} hydration job in status {job.status}.")
        self.session.commit()
        return job

    def approve_candidates(
        self,
        job_id: uuid.UUID,
        *,
        candidate_ids: list[uuid.UUID] | None = None,
        minimum_confidence: float = 0.8,
    ) -> ContactHydrationJob:
        job = self._job(job_id)
        statement = select(ContactHydrationCandidate).where(
            ContactHydrationCandidate.job_id == job.id,
            ContactHydrationCandidate.status == "review",
            ContactHydrationCandidate.action.in_(["create", "update"]),
        )
        if candidate_ids:
            statement = statement.where(ContactHydrationCandidate.id.in_(candidate_ids))
        else:
            statement = statement.where(ContactHydrationCandidate.confidence >= minimum_confidence)
        approved = list(self.session.scalars(statement))
        if not approved:
            raise ContactHydrationError("No review-ready hydration candidates matched the approval request.")
        for candidate in approved:
            candidate.status = "approved"
        job.status = "promoting"
        job.mode = "live"
        self.session.commit()
        return job

    def review_candidate(
        self,
        candidate_id: uuid.UUID,
        *,
        decision: str,
        existing_object_id: uuid.UUID | None = None,
    ) -> ContactHydrationCandidate:
        candidate = self.session.get(ContactHydrationCandidate, candidate_id)
        if candidate is None:
            raise ContactHydrationError("Hydration candidate not found.")
        if decision == "reject":
            candidate.status = "rejected"
        elif decision in {"create", "update"}:
            if decision == "update" and existing_object_id is None:
                raise ContactHydrationError("Updating a candidate requires an existing object id.")
            candidate.action = decision
            candidate.existing_object_id = existing_object_id
            candidate.status = "approved"
            job = self._job(candidate.job_id)
            job.status = "promoting"
            job.mode = "live"
        else:
            raise ContactHydrationError("Candidate decision must be create, update, or reject.")
        self.session.commit()
        return candidate

    def job_payload(self, job: ContactHydrationJob) -> dict[str, Any]:
        counts = dict(
            self.session.execute(
                select(ContactHydrationCandidate.status, func.count(ContactHydrationCandidate.id))
                .where(ContactHydrationCandidate.job_id == job.id)
                .group_by(ContactHydrationCandidate.status)
            ).all()
        )
        type_counts = dict(
            self.session.execute(
                select(ContactHydrationCandidate.candidate_type, func.count(ContactHydrationCandidate.id))
                .where(ContactHydrationCandidate.job_id == job.id)
                .group_by(ContactHydrationCandidate.candidate_type)
            ).all()
        )
        return {
            "id": str(job.id),
            "domain_id": str(job.domain_id),
            "source": job.source,
            "query": job.query,
            "mode": job.mode,
            "status": job.status,
            "page_size": job.page_size,
            "max_messages": job.max_messages,
            "max_contacts": job.max_contacts,
            "messages_scanned": job.messages_scanned,
            "candidates_found": job.candidates_found,
            "promoted_count": job.promoted_count,
            "excluded_count": job.excluded_count,
            "ambiguous_count": job.ambiguous_count,
            "local_model_profile": job.local_model_profile,
            "cloud_model_profile": job.cloud_model_profile,
            "enable_enrichment": job.enable_enrichment,
            "enable_cloud_fallback": job.enable_cloud_fallback,
            "max_cloud_calls": job.max_cloud_calls,
            "cloud_calls": job.cloud_calls,
            "candidate_status_counts": counts,
            "candidate_type_counts": type_counts,
            "stats": job.stats,
            "error_message": job.error_message,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }

    @staticmethod
    def candidate_payload(candidate: ContactHydrationCandidate) -> dict[str, Any]:
        return {
            "id": str(candidate.id),
            "job_id": str(candidate.job_id),
            "candidate_type": candidate.candidate_type,
            "identity_key": candidate.identity_key,
            "display_name": candidate.display_name,
            "action": candidate.action,
            "status": candidate.status,
            "confidence": candidate.confidence,
            "existing_object_id": str(candidate.existing_object_id) if candidate.existing_object_id else None,
            "promoted_object_id": str(candidate.promoted_object_id) if candidate.promoted_object_id else None,
            "routed_item_id": str(candidate.routed_item_id) if candidate.routed_item_id else None,
            "proposed_data": candidate.proposed_data,
            "evidence": candidate.evidence,
            "source_refs": candidate.source_refs,
            "error_message": candidate.error_message,
            "metadata": candidate.metadata_,
        }

    def _claim_job(self, owner: str) -> ContactHydrationJob | None:
        now = datetime.now(UTC)
        statement = (
            select(ContactHydrationJob)
            .where(
                ContactHydrationJob.status.in_(ACTIVE_JOB_STATUSES),
                (ContactHydrationJob.lease_expires_at.is_(None))
                | (ContactHydrationJob.lease_expires_at < now),
            )
            .order_by(ContactHydrationJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        job = self.session.scalar(statement)
        if job is None:
            return None
        job.lease_owner = owner
        job.lease_expires_at = now + timedelta(minutes=10)
        if job.started_at is None:
            job.started_at = now
        self.session.commit()
        return job

    def _scan_page(self, job: ContactHydrationJob) -> None:
        job.status = "scanning"
        remaining = job.max_messages - job.messages_scanned
        if remaining <= 0:
            self._finish_scan(job)
            return
        output = self._execute_tool(
            job,
            "gmail.message.search",
            {
                "query": job.query,
                "limit": min(job.page_size, remaining),
                "page_token": job.page_token,
            },
        )
        messages = output.get("messages") if isinstance(output.get("messages"), list) else []
        for message in messages:
            if isinstance(message, dict):
                self._ingest_message(job, message)
        job.messages_scanned += len(messages)
        job.page_token = str(output.get("next_page_token") or "").strip() or None
        job.stats = {
            **(job.stats or {}),
            "gmail_pages": int((job.stats or {}).get("gmail_pages") or 0) + 1,
            "result_size_estimate": output.get("result_size_estimate"),
        }
        if not job.page_token or not messages or job.messages_scanned >= job.max_messages:
            self._finish_scan(job)

    def _ingest_message(self, job: ContactHydrationJob, message: dict[str, Any]) -> None:
        self_addresses = {get_settings().user_email.lower()}
        headers = message.get("headers") if isinstance(message.get("headers"), dict) else {}
        raw_fields = [
            str(message.get("from") or headers.get("from") or ""),
            str(message.get("to") or headers.get("to") or ""),
            str(message.get("cc") or headers.get("cc") or ""),
            str(headers.get("bcc") or ""),
        ]
        from_addresses = {email.lower() for _, email in getaddresses([raw_fields[0]]) if email}
        direction = "outbound" if from_addresses & self_addresses else "inbound"
        source_ref = _gmail_source_ref(message)
        sample = {
            "message_id": source_ref.get("message_id"),
            "thread_id": source_ref.get("thread_id"),
            "subject": source_ref.get("subject"),
            "date": source_ref.get("date"),
            "snippet": str(message.get("snippet") or "")[:500],
            "direction": direction,
        }
        seen_addresses: set[str] = set()
        for display_name, email in getaddresses([value for value in raw_fields if value.strip()]):
            normalized_email = email.strip().lower()
            if not normalized_email or normalized_email in seen_addresses or normalized_email in self_addresses:
                continue
            seen_addresses.add(normalized_email)
            if _automated_address(normalized_email):
                self._upsert_excluded_candidate(job, normalized_email, display_name or normalized_email)
                continue
            name, name_source, observed_names = _display_name(display_name, normalized_email)
            if is_maestro_user_reference(name=name, email=normalized_email):
                continue
            candidate = self._upsert_contact_candidate(
                job,
                name=name,
                name_source=name_source,
                observed_names=observed_names,
                email=normalized_email,
                direction=direction,
                sample=sample,
                source_ref=source_ref,
            )
            domain = normalized_email.rsplit("@", 1)[-1]
            if domain not in PERSONAL_EMAIL_DOMAINS:
                organization = self._upsert_organization_candidate(
                    job,
                    domain=domain,
                    source_ref=source_ref,
                    sample=sample,
                )
                proposed = {**candidate.proposed_data, "organization": organization.display_name}
                candidate.proposed_data = proposed

    def _upsert_contact_candidate(
        self,
        job: ContactHydrationJob,
        *,
        name: str,
        name_source: str,
        observed_names: set[str],
        email: str,
        direction: str,
        sample: dict[str, Any],
        source_ref: dict[str, Any],
    ) -> ContactHydrationCandidate:
        candidate = self.session.scalar(
            select(ContactHydrationCandidate).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.candidate_type == "contact",
                ContactHydrationCandidate.identity_key == email,
            )
        )
        if candidate is None:
            existing = self.session.scalar(select(Contact).where(Contact.email.ilike(email)))
            name_collision = self.session.scalar(
                select(Contact).where(
                    Contact.normalized_name == _normalize(name),
                    Contact.status != "archived",
                )
            )
            action = "update" if existing else "create"
            status = "discovered"
            confidence = 0.99 if existing else 0.9
            existing_id = existing.id if existing else None
            if existing is None and name_collision is not None and (name_collision.email or "").lower() != email:
                action = "needs_review"
                status = "review"
                confidence = 0.45
                existing_id = name_collision.id
            canonical_name = existing.name if existing else name
            candidate = ContactHydrationCandidate(
                job_id=job.id,
                candidate_type="contact",
                identity_key=email,
                display_name=canonical_name,
                action=action,
                status=status,
                confidence=confidence,
                existing_object_id=existing_id,
                proposed_data={"name": canonical_name, "email": email},
                evidence={
                    "message_count": 0,
                    "inbound_count": 0,
                    "outbound_count": 0,
                    "observed_names": [],
                    "samples": [],
                },
                source_refs=[],
                metadata_={"name_source": "existing_contact" if existing else name_source},
            )
            self.session.add(candidate)
            self.session.flush()
        evidence = dict(candidate.evidence or {})
        evidence["message_count"] = int(evidence.get("message_count") or 0) + 1
        count_key = "outbound_count" if direction == "outbound" else "inbound_count"
        evidence[count_key] = int(evidence.get(count_key) or 0) + 1
        accumulated_names = {
            str(value).strip()
            for value in evidence.get("observed_names") or []
            if str(value).strip()
        }
        accumulated_names.update(
            value
            for value in observed_names
            if value and not is_maestro_user_reference(name=value, email=email)
        )
        evidence["observed_names"] = sorted(accumulated_names)
        samples = list(evidence.get("samples") or [])
        if len(samples) < 8 and sample.get("message_id") not in {item.get("message_id") for item in samples}:
            samples.append(sample)
        evidence["samples"] = samples
        candidate.evidence = evidence
        candidate.source_refs = _merge_source_refs(candidate.source_refs, [source_ref], limit=30)
        current_source = str((candidate.metadata_ or {}).get("name_source") or "email_local")
        if name_source == "header" and current_source == "email_local":
            candidate.display_name = name
            candidate.proposed_data = {**candidate.proposed_data, "name": name}
            candidate.metadata_ = {**(candidate.metadata_ or {}), "name_source": "header"}
        elif name_source == current_source and _name_quality(name) > _name_quality(candidate.display_name):
            candidate.display_name = name
            candidate.proposed_data = {**candidate.proposed_data, "name": name}
        return candidate

    def _upsert_organization_candidate(
        self,
        job: ContactHydrationJob,
        *,
        domain: str,
        source_ref: dict[str, Any],
        sample: dict[str, Any],
    ) -> ContactHydrationCandidate:
        identity_key = f"domain:{domain}"
        candidate = self.session.scalar(
            select(ContactHydrationCandidate).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.candidate_type == "organization",
                ContactHydrationCandidate.identity_key == identity_key,
            )
        )
        if candidate is None:
            existing = self._organization_for_domain(domain)
            name = _organization_name_from_domain(domain)
            candidate = ContactHydrationCandidate(
                job_id=job.id,
                candidate_type="organization",
                identity_key=identity_key,
                display_name=existing.name if existing else name,
                action="update" if existing else "create",
                status="discovered",
                confidence=0.99 if existing else 0.82,
                existing_object_id=existing.id if existing else None,
                proposed_data={
                    "name": existing.name if existing else name,
                    "website": f"https://{domain}",
                    "email_domain": domain,
                },
                evidence={"message_count": 0, "samples": []},
                source_refs=[],
                metadata_={"inferred_from_email_domain": existing is None},
            )
            self.session.add(candidate)
            self.session.flush()
        evidence = dict(candidate.evidence or {})
        evidence["message_count"] = int(evidence.get("message_count") or 0) + 1
        samples = list(evidence.get("samples") or [])
        if len(samples) < 8 and sample.get("message_id") not in {item.get("message_id") for item in samples}:
            samples.append(sample)
        evidence["samples"] = samples
        candidate.evidence = evidence
        candidate.source_refs = _merge_source_refs(candidate.source_refs, [source_ref], limit=30)
        return candidate

    def _upsert_excluded_candidate(self, job: ContactHydrationJob, email: str, name: str) -> None:
        candidate = self.session.scalar(
            select(ContactHydrationCandidate).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.candidate_type == "contact",
                ContactHydrationCandidate.identity_key == email,
            )
        )
        if candidate is None:
            self.session.add(
                ContactHydrationCandidate(
                    job_id=job.id,
                    candidate_type="contact",
                    identity_key=email,
                    display_name=name,
                    action="exclude",
                    status="excluded",
                    confidence=1.0,
                    proposed_data={"name": name, "email": email},
                    evidence={"reason": "automated or role account"},
                    source_refs=[],
                    metadata_={},
                )
            )

    def _finish_scan(self, job: ContactHydrationJob) -> None:
        contacts = list(
            self.session.scalars(
                select(ContactHydrationCandidate).where(
                    ContactHydrationCandidate.job_id == job.id,
                    ContactHydrationCandidate.candidate_type == "contact",
                    ContactHydrationCandidate.status != "excluded",
                )
            )
        )
        contacts.sort(key=lambda item: int((item.evidence or {}).get("message_count") or 0), reverse=True)
        for candidate in contacts:
            _apply_observed_contact_aliases(candidate)
        for candidate in contacts[job.max_contacts:]:
            candidate.action = "exclude"
            candidate.status = "excluded"
            candidate.evidence = {**(candidate.evidence or {}), "reason": "max contacts limit"}
        organizations = self.session.scalars(
            select(ContactHydrationCandidate).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.candidate_type == "organization",
                ContactHydrationCandidate.status == "discovered",
            )
        ).all()
        for candidate in organizations:
            candidate.status = "review"
        job.candidates_found = self.session.scalar(
            select(func.count(ContactHydrationCandidate.id)).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.status != "excluded",
            )
        ) or 0
        job.excluded_count = self.session.scalar(
            select(func.count(ContactHydrationCandidate.id)).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.status == "excluded",
            )
        ) or 0
        job.ambiguous_count = self.session.scalar(
            select(func.count(ContactHydrationCandidate.id)).where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.action == "needs_review",
            )
        ) or 0
        has_enrichment = job.enable_enrichment and any(
            candidate.status == "discovered" for candidate in contacts[: job.max_contacts]
        )
        if has_enrichment:
            job.status = "enriching"
        else:
            for candidate in contacts:
                if candidate.status == "discovered":
                    candidate.status = "review"
            job.status = "review"

    def _enrich_next_candidate(self, job: ContactHydrationJob) -> None:
        candidate = self.session.scalar(
            select(ContactHydrationCandidate)
            .where(
                ContactHydrationCandidate.job_id == job.id,
                ContactHydrationCandidate.candidate_type == "contact",
                ContactHydrationCandidate.status == "discovered",
            )
            .order_by(ContactHydrationCandidate.confidence.desc())
            .limit(1)
        )
        if candidate is None:
            job.status = "review"
            return
        samples = list((candidate.evidence or {}).get("samples") or [])
        thread_ids = list(dict.fromkeys(str(sample.get("thread_id")) for sample in samples if sample.get("thread_id")))
        evidence_payload: list[dict[str, Any]] = []
        max_threads = int((job.config or {}).get("max_threads_per_contact") or 2)
        for thread_id in thread_ids[:max_threads]:
            output = self._execute_tool(
                job,
                "gmail.thread.get",
                {"thread_id": thread_id, "max_body_chars": 5000},
            )
            evidence_payload.append(
                {
                    "thread_id": thread_id,
                    "messages": [
                        {
                            "subject": message.get("subject"),
                            "from": message.get("from"),
                            "to": message.get("to"),
                            "date": message.get("date"),
                            "body_text": str(message.get("body_text") or "")[:5000],
                        }
                        for message in (output.get("messages") or [])
                        if isinstance(message, dict)
                    ],
                }
            )
        try:
            enriched = self._enrichment_response(
                job,
                job.local_model_profile,
                candidate,
                evidence_payload,
            )
            job.stats = {
                **(job.stats or {}),
                "local_calls": int((job.stats or {}).get("local_calls") or 0) + 1,
            }
        except Exception as local_error:
            job.stats = {
                **(job.stats or {}),
                "local_failures": int((job.stats or {}).get("local_failures") or 0) + 1,
            }
            enriched = None
            if job.enable_cloud_fallback and job.cloud_calls < job.max_cloud_calls:
                try:
                    enriched = self._enrichment_response(
                        job,
                        job.cloud_model_profile,
                        candidate,
                        evidence_payload,
                    )
                    job.cloud_calls += 1
                except Exception:
                    job.stats = {
                        **(job.stats or {}),
                        "cloud_failures": int((job.stats or {}).get("cloud_failures") or 0) + 1,
                    }
            if enriched is None:
                candidate.error_message = f"Enrichment skipped: {local_error}"
        if enriched:
            canonical_name, aliases, identity_evidence = _verified_contact_identity(
                candidate,
                enriched,
                evidence_payload,
            )
            candidate.display_name = canonical_name
            organization_value = candidate.proposed_data.get("organization")
            organization_name = str(enriched.get("organization") or "").strip()
            if organization_name:
                verified_organization = self._apply_enriched_organization(
                    job,
                    candidate,
                    organization_name,
                    [
                        str(value).strip()
                        for value in enriched.get("organization_aliases") or []
                        if str(value).strip()
                    ],
                    evidence_payload,
                )
                organization_value = verified_organization or organization_value
            candidate.proposed_data = {
                **candidate.proposed_data,
                "name": canonical_name,
                "aliases": aliases,
                "alias_evidence": identity_evidence,
                "organization": str(organization_value or "").strip() or None,
                "role": str(enriched.get("role") or "").strip() or None,
                "summary": str(enriched.get("summary") or "").strip() or None,
                "relationship_context": str(enriched.get("relationship_context") or "").strip() or None,
            }
            candidate.confidence = max(candidate.confidence, float(enriched.get("confidence") or 0.0))
        if candidate.action != "needs_review":
            candidate.status = "review"
        self.session.commit()

    def _enrichment_response(
        self,
        job: ContactHydrationJob,
        profile: str,
        candidate: ContactHydrationCandidate,
        evidence_payload: list[dict[str, Any]],
    ) -> dict[str, Any]:
        client = self.llm_factory(profile)
        identity = maestro_user_identity()
        domain = self.session.get(Domain, job.domain_id)
        global_setting = self.session.get(RuntimeSetting, "global_maestro_context")
        global_context = str(
            ((global_setting.value or {}).get("context") if global_setting else "")
            or "Maestro is Chris Aliperti's cross-domain chief-of-staff system."
        )
        return client.structured_response(
            instructions=load_prompt("contact_hydration.md"),
            input_text=str(
                {
                    "owner": {
                        "name": identity.full_name,
                        "email": identity.email,
                        "role": "Maestro system owner; never a CRM contact candidate",
                    },
                    "global_context": global_context[:4000],
                    "domain_context": str(domain.description or "")[:4000] if domain else "",
                    "candidate_identity": {
                        "name": candidate.display_name,
                        "email": candidate.identity_key,
                        "name_source": (candidate.metadata_ or {}).get("name_source"),
                        "instruction": (
                            "Header names are authoritative. An email-local name may be refined "
                            "only from direct signature or header evidence."
                        ),
                    },
                    "interaction_stats": candidate.evidence,
                    "representative_threads": evidence_payload,
                }
            ),
            schema_name="contact_hydration_enrichment",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "canonical_name": {"type": "string"},
                    "contact_aliases": {"type": "array", "items": {"type": "string"}},
                    "organization": {"type": "string"},
                    "organization_aliases": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string"},
                    "summary": {"type": "string"},
                    "relationship_context": {"type": "string"},
                    "identity_evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "canonical_name",
                    "contact_aliases",
                    "organization",
                    "organization_aliases",
                    "role",
                    "summary",
                    "relationship_context",
                    "identity_evidence",
                    "confidence",
                ],
            },
        )

    def _apply_enriched_organization(
        self,
        job: ContactHydrationJob,
        contact_candidate: ContactHydrationCandidate,
        organization_name: str,
        organization_aliases: list[str],
        evidence_payload: list[dict[str, Any]],
    ) -> str | None:
        email = str(contact_candidate.proposed_data.get("email") or "")
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if not domain or domain in PERSONAL_EMAIL_DOMAINS:
            return None
        verified_name, verified_aliases = _verified_organization_identity(
            domain,
            organization_name,
            organization_aliases,
            evidence_payload,
        )
        if not verified_name:
            return None
        organization = self._upsert_organization_candidate(
            job,
            domain=domain,
            source_ref=contact_candidate.source_refs[0] if contact_candidate.source_refs else {},
            sample=((contact_candidate.evidence or {}).get("samples") or [{}])[0],
        )
        final_name = organization.display_name if organization.existing_object_id else verified_name
        final_aliases = set(verified_aliases)
        if _normalize(verified_name) != _normalize(final_name):
            final_aliases.add(verified_name)
        organization.display_name = final_name
        organization.proposed_data = {
            **organization.proposed_data,
            "name": final_name,
            "aliases": sorted(final_aliases),
            "summary": f"Organization associated with {contact_candidate.display_name} in historical Gmail evidence.",
        }
        existing = self.session.scalar(
            select(Entity).where(Entity.normalized_name == _normalize(verified_name))
        )
        if existing:
            organization.action = "update"
            organization.existing_object_id = existing.id
            organization.display_name = existing.name
            organization.proposed_data = {
                **organization.proposed_data,
                "name": existing.name,
                "aliases": sorted(
                    {
                        *final_aliases,
                        *(
                            [verified_name]
                            if _normalize(verified_name) != _normalize(existing.name)
                            else []
                        ),
                    }
                ),
            }
            organization.confidence = 0.99
        organization.status = "review"
        return organization.display_name

    def _promote_batch(self, job: ContactHydrationJob) -> None:
        candidates = list(
            self.session.scalars(
                select(ContactHydrationCandidate)
                .where(
                    ContactHydrationCandidate.job_id == job.id,
                    ContactHydrationCandidate.status == "approved",
                )
                .order_by(ContactHydrationCandidate.candidate_type.desc())
                .limit(10)
            )
        )
        if not candidates:
            remaining_review = self.session.scalar(
                select(func.count(ContactHydrationCandidate.id)).where(
                    ContactHydrationCandidate.job_id == job.id,
                    ContactHydrationCandidate.status.in_(["review", "discovered"]),
                )
            ) or 0
            if remaining_review:
                job.status = "review"
            else:
                job.status = "complete"
                job.completed_at = datetime.now(UTC)
                task = self.session.get(Task, job.task_id) if job.task_id else None
                if task is not None:
                    task.status = "completed"
                    task.output_payload = {"hydration_job_id": str(job.id), "promoted_count": job.promoted_count}
            RoutedHygieneService(self.session).run_once(persist_report=True)
            ContactEmbeddingService(self.session).backfill(limit=job.max_contacts)
            OrganizationEmbeddingService(self.session).backfill(limit=job.max_contacts)
            return
        for candidate in candidates:
            try:
                with self.session.begin_nested():
                    self._promote_candidate(job, candidate)
            except Exception as exc:
                candidate.status = "failed"
                candidate.error_message = str(exc)
        self.session.commit()

    def _promote_candidate(
        self,
        job: ContactHydrationJob,
        candidate: ContactHydrationCandidate,
    ) -> None:
        from app.db.models import RoutedItem

        data = candidate.proposed_data or {}
        route_type = "contact" if candidate.candidate_type == "contact" else "entity"
        if route_type == "contact" and is_maestro_user_reference(
            name=str(data.get("name") or candidate.display_name),
            email=str(data.get("email") or candidate.identity_key),
        ):
            candidate.status = "rejected"
            candidate.error_message = "Suppressed Maestro owner identity from CRM hydration."
            return
        summary = str(data.get("summary") or "").strip()
        if not summary:
            summary = (
                f"Historical Gmail participant with {int((candidate.evidence or {}).get('message_count') or 0)} observed messages."
                if route_type == "contact"
                else f"Organization inferred from historical Gmail domain {data.get('email_domain') or ''}."
            )
        metadata = {
            **data,
            "hydration_job_id": str(job.id),
            "hydration_candidate_id": str(candidate.id),
            "resolution_action": candidate.action,
            "existing_object_id": str(candidate.existing_object_id) if candidate.existing_object_id else None,
            "interaction_type": "email_history",
            "channel": "gmail",
            "direction": _dominant_direction(candidate.evidence),
            "last_contact_at": _latest_sample_date(candidate.evidence),
            "historical_import": True,
        }
        item = RoutedItem(
            domain_id=job.domain_id,
            task_id=job.task_id,
            route_type=route_type,
            title=candidate.display_name,
            content=summary,
            priority="normal",
            status="open",
            source_refs=candidate.source_refs,
            metadata_=metadata,
        )
        self.session.add(item)
        self.session.flush()
        result = RoutedMemoryService(self.session, enable_llm_resolver=False).promote_item(item)
        if result is None:
            raise ContactHydrationError("Candidate could not be promoted to a canonical routed object.")
        candidate.routed_item_id = item.id
        candidate.promoted_object_id = result.object_id
        candidate.status = "promoted"
        candidate.error_message = None
        job.promoted_count += 1

    def _execute_tool(self, job: ContactHydrationJob, tool_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = self.session.get(Task, job.task_id) if job.task_id else None
        if task is None:
            raise ContactHydrationError("Hydration job has no audit task.")
        agent = self.session.get(Agent, task.assigned_agent_id) if task.assigned_agent_id else None
        if agent is None:
            raise ContactHydrationError("Hydration job has no Gmail-capable agent.")
        result = self.tool_service.execute_for_task(
            ToolExecutionRequest(agent_key=agent.key, tool_key=tool_key, payload=payload),
            task=task,
        )
        if result.status != "complete" or not isinstance(result.output, dict):
            raise ContactHydrationError(result.error_message or f"{tool_key} failed during hydration.")
        return result.output

    def _gmail_agent(self, domain_id: uuid.UUID) -> Agent:
        agents = list(
            self.session.scalars(
                select(Agent).where(Agent.domain_id == domain_id, Agent.is_active.is_(True))
            )
        )
        for agent in agents:
            permissions = agent.tool_permissions or {}
            if {"gmail.message.search", "gmail.thread.get"}.issubset(permissions):
                return agent
        raise ContactHydrationError(
            "The selected domain needs an active agent with gmail.message.search and gmail.thread.get access."
        )

    def _organization_for_domain(self, domain: str) -> Entity | None:
        normalized_domain = domain.lower().removeprefix("www.")
        organizations = self.session.scalars(select(Entity).where(Entity.status != "archived")).all()
        for organization in organizations:
            website = str(organization.website or (organization.metadata_ or {}).get("email_domain") or "").lower()
            if normalized_domain and normalized_domain in website:
                return organization
        return None

    def _job(self, job_id: uuid.UUID) -> ContactHydrationJob:
        job = self.session.get(ContactHydrationJob, job_id)
        if job is None:
            raise ContactHydrationError("Hydration job not found.")
        return job


def _llm_client_for_profile(profile: str) -> LLMClient:
    settings = get_settings()
    if profile.startswith("ollama:"):
        return OllamaLLMClient(
            model=profile.removeprefix("ollama:").strip(),
            base_url=settings.embedding_base_url,
            timeout_seconds=settings.ollama_llm_timeout_seconds,
        )
    if profile.startswith("openrouter:"):
        return OpenAILLMClient(provider="openrouter", model=profile.removeprefix("openrouter:").strip())
    if profile.startswith("openai:"):
        return OpenAILLMClient(provider="openai", model=profile.removeprefix("openai:").strip())
    return OpenAILLMClient(model=profile or None)


def _gmail_source_ref(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "gmail_message",
        "message_id": message.get("message_id") or message.get("id"),
        "thread_id": message.get("thread_id"),
        "subject": message.get("subject"),
        "from": message.get("from"),
        "to": message.get("to"),
        "date": message.get("date") or _internal_date_iso(message.get("internal_date")),
    }


def _internal_date_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError):
        return None


def _display_name(raw_name: str, email: str) -> tuple[str, str, set[str]]:
    cleaned = re.sub(r"\s+", " ", raw_name.replace('"', "")).strip()
    if cleaned and "@" not in cleaned:
        canonical, aliases = _person_name_from_header(cleaned)
        return canonical, "header", aliases
    local = email.split("@", 1)[0]
    parts = []
    for raw_part in re.split(r"[._+-]+", local):
        part = re.sub(r"\d+$", "", raw_part.lower())
        if part and part not in PERSON_NAME_STOP_TOKENS:
            parts.append(part.upper() if len(part) == 1 else part.capitalize())
    name = " ".join(parts).strip() or local.replace("@", " ").strip().title()
    return name, "email_local", set()


def _person_name_from_header(value: str) -> tuple[str, set[str]]:
    raw = re.sub(r"\([^)]*\)", " ", value)
    raw = re.sub(r"\s+", " ", raw).strip(" ,")
    rank = next(
        (token.upper() for token in re.findall(r"[A-Za-z0-9]+", raw) if token.lower() in PERSON_TITLE_TOKENS),
        None,
    )
    if "," in raw:
        surname, remainder = (part.strip() for part in raw.split(",", 1))
        given_tokens = _person_tokens_before_marker(remainder)
        canonical_tokens = [*given_tokens, *_person_tokens_before_marker(surname)]
    else:
        tokens = _person_tokens_before_marker(raw)
        canonical_tokens = tokens[1:] if tokens and tokens[0].lower() in PERSON_TITLE_TOKENS else tokens
    canonical = " ".join(canonical_tokens).strip() or raw
    aliases = {canonical}
    surname = canonical_tokens[-1] if canonical_tokens else ""
    if rank and surname:
        aliases.add(f"{rank} {surname}")
        if len(canonical_tokens) >= 2:
            aliases.add(f"{rank} {canonical_tokens[0]} {surname}")
    return canonical, {alias for alias in aliases if alias.strip()}


def _person_tokens_before_marker(value: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z'’-]*|[A-Za-z]", value):
        lowered = token.lower().rstrip(".")
        if lowered in PERSON_NAME_STOP_TOKENS or (lowered in PERSON_TITLE_TOKENS and tokens):
            break
        if lowered not in PERSON_TITLE_TOKENS:
            tokens.append(token)
    return tokens


def _automated_address(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in AUTOMATED_LOCAL_PARTS or any(
        token in local for token in ("no-reply", "noreply", "notification", "mailer-daemon")
    )


def _organization_name_from_domain(domain: str) -> str:
    parts = domain.lower().removeprefix("www.").split(".")
    stem = parts[-2] if len(parts) >= 2 else parts[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", stem) if part)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _name_quality(value: str) -> int:
    return (2 if " " in value.strip() else 0) + (1 if "@" not in value else 0) + min(len(value), 40)


def _verified_contact_identity(
    candidate: ContactHydrationCandidate,
    enriched: dict[str, Any],
    evidence_payload: list[dict[str, Any]],
) -> tuple[str, list[str], dict[str, str]]:
    corpus = _identity_corpus(evidence_payload, (candidate.evidence or {}).get("observed_names") or [])
    canonical = candidate.display_name
    proposed = str(enriched.get("canonical_name") or "").strip()
    name_source = str((candidate.metadata_ or {}).get("name_source") or "email_local")
    if (
        name_source == "email_local"
        and proposed
        and not is_maestro_user_reference(name=proposed, email=candidate.identity_key)
        and _phrase_is_observed(proposed, corpus)
    ):
        canonical = proposed

    values = {
        str(value).strip(): "email_header"
        for value in (candidate.evidence or {}).get("observed_names") or []
        if str(value).strip()
    }
    for value in enriched.get("contact_aliases") or []:
        alias = str(value).strip()
        if alias and _supported_person_alias(alias, canonical, corpus):
            values[alias] = "interaction_evidence"
    normalized_canonical = _normalize(canonical)
    aliases = sorted(
        alias
        for alias in values
        if _normalize(alias) != normalized_canonical
        and not is_maestro_user_reference(name=alias, email=candidate.identity_key)
    )
    return canonical, aliases, {alias: values[alias] for alias in aliases}


def _apply_observed_contact_aliases(candidate: ContactHydrationCandidate) -> None:
    normalized_name = _normalize(candidate.display_name)
    aliases = sorted(
        {
            str(value).strip()
            for value in (candidate.evidence or {}).get("observed_names") or []
            if str(value).strip()
            and _normalize(str(value)) != normalized_name
            and not is_maestro_user_reference(name=str(value), email=candidate.identity_key)
        }
    )
    candidate.proposed_data = {
        **(candidate.proposed_data or {}),
        "aliases": aliases,
        "alias_evidence": {alias: "email_header" for alias in aliases},
    }


def _verified_organization_identity(
    email_domain: str,
    proposed_name: str,
    proposed_aliases: list[str],
    evidence_payload: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    corpus = _identity_corpus(evidence_payload, [])
    domain_name = _organization_name_from_domain(email_domain)
    domain_tokens = set(_normalize(domain_name).split())
    normalized_proposed = _normalize(proposed_name)
    proposed_tokens = set(normalized_proposed.split())
    supported = _phrase_is_observed(proposed_name, corpus) or (
        bool(domain_tokens) and domain_tokens == proposed_tokens
    )
    if not supported:
        return None, []
    aliases = {
        alias
        for alias in proposed_aliases
        if _phrase_is_observed(alias, corpus)
        and _normalize(alias) != normalized_proposed
    }
    if _normalize(domain_name) != normalized_proposed:
        aliases.add(domain_name)
    return proposed_name, sorted(aliases)


def _identity_corpus(evidence_payload: list[dict[str, Any]], observed_names: list[str]) -> str:
    serialized = json.dumps({"threads": evidence_payload, "observed_names": observed_names})
    return _normalize(serialized.replace("\\n", " ").replace("\\r", " "))


def _phrase_is_observed(value: str, normalized_corpus: str) -> bool:
    phrase = _normalize(value)
    return bool(phrase and len(phrase) >= 3 and phrase in normalized_corpus)


def _supported_person_alias(alias: str, canonical_name: str, normalized_corpus: str) -> bool:
    if _phrase_is_observed(alias, normalized_corpus):
        return True
    alias_tokens = _normalize(alias).split()
    canonical_tokens = _normalize(canonical_name).split()
    if len(alias_tokens) < 2 or len(canonical_tokens) < 2:
        return False
    if alias_tokens[-1] != canonical_tokens[-1]:
        return False
    return all(token in normalized_corpus.split() for token in alias_tokens)


def _merge_source_refs(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in [*existing, *incoming]:
        key = (
            str(source.get("type") or ""),
            str(source.get("message_id") or source.get("id") or source.get("thread_id") or source),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(source)
        if len(merged) >= limit:
            break
    return merged


def _dominant_direction(evidence: dict[str, Any]) -> str:
    return "outbound" if int(evidence.get("outbound_count") or 0) >= int(evidence.get("inbound_count") or 0) else "inbound"


def _latest_sample_date(evidence: dict[str, Any]) -> str | None:
    values: list[datetime] = []
    for sample in evidence.get("samples") or []:
        raw = str(sample.get("date") or "").strip()
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
        values.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return max(values).isoformat() if values else None
