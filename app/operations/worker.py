"""Signal-aware standalone process for Maestro's existing background services.

This is intentionally a thin host around the current scheduler, Gmail trigger, and memory
dropbox services. Durable state remains in Postgres. A cycle opens its own SQLAlchemy session so a
failed integration cannot poison the other loops' transactions.
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Callable

from app.agents.runtime import AgentRegistryService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.seed import seed_default_domains
from app.db.session import SessionLocal
from app.issues.repositories import ensure_default_repository_portfolio, ensure_runtime_repository
from app.issues.worker import RepositoryIntelligenceWorker
from app.maestro.calendar_trigger import CalendarTriggerService, calendar_trigger_worker_settings
from app.maestro.gmail_trigger import GmailTriggerService, gmail_trigger_worker_settings
from app.maestro.identity_grounding import IdentityGroundingService
from app.maestro.mobile_notifications import MobileNotificationWorker
from app.maestro.product_issue_agent_tasks import ProductIssueAgentTaskService
from app.maestro.scheduler_worker import SchedulerWorkerService, scheduler_worker_settings
from app.maestro.todo_agent_tasks import TodoAgentTaskService
from app.memory.contact_hydration import ContactHydrationService
from app.memory.context_mailbox import ContextMailboxService
from app.memory.dropbox import MemoryDropboxProcessor
from app.memory.hygiene import DurableMemoryHygieneService
from app.memory.recurring_todos import RecurringTodoService
from app.memory.routed_hygiene import RoutedHygieneService

logger = logging.getLogger(__name__)

Cycle = Callable[[], int]


def process_scheduler_once() -> int:
    """Run one scheduler cycle and return its configured polling interval."""
    with SessionLocal() as session:
        worker_settings = scheduler_worker_settings(session)
        if worker_settings["enabled"]:
            SchedulerWorkerService(session).run_once(
                owner=_worker_owner(),
                claim_limit=int(worker_settings["claim_limit"]),
                execute_llm=bool(worker_settings["execute_llm"]),
                auto_tool_loop=bool(worker_settings["auto_tool_loop"]),
            )
        return int(worker_settings["interval_seconds"])


def process_gmail_triggers_once() -> int:
    """Run one Gmail History polling cycle and return its configured interval."""
    with SessionLocal() as session:
        worker_settings = gmail_trigger_worker_settings(session)
        if worker_settings["enabled"]:
            result = GmailTriggerService(session).poll_once(
                page_size=int(worker_settings["page_size"]),
            )
            emitted_count = int(result.get("emitted_count") or 0)
            if emitted_count:
                logger.info("Gmail trigger worker emitted %s message event(s).", emitted_count)
        return int(worker_settings["interval_seconds"])


def process_memory_dropbox_once() -> int:
    """Run one local dropbox scan when configured and return its polling interval."""
    settings = get_settings()
    if settings.memory_dropbox_autorun:
        with SessionLocal() as session:
            results = MemoryDropboxProcessor(session).process_once()
            if results:
                logger.info(
                    "Memory dropbox worker processed %s artifact(s): %s",
                    len(results),
                    ", ".join(result.status for result in results),
                )
    return int(settings.memory_dropbox_interval_seconds)


def process_calendar_triggers_once() -> int:
    with SessionLocal() as session:
        worker_settings = calendar_trigger_worker_settings(session)
        if worker_settings["enabled"]:
            result = CalendarTriggerService(session).poll_once(
                page_size=int(worker_settings["page_size"]),
            )
            emitted_count = int(result.get("emitted_count") or 0)
            if emitted_count:
                logger.info("Calendar trigger emitted %s event change(s).", emitted_count)
        return int(worker_settings["interval_seconds"])


def process_context_mailbox_once() -> int:
    settings = get_settings()
    if not (settings.context_mailbox_autorun and settings.context_mailbox_configured):
        return settings.context_mailbox_interval_seconds
    with SessionLocal() as session:
        result = ContextMailboxService(session, settings=settings).poll_once()
        counts = result["counts"]
        if counts.get("staged") or counts.get("quarantined") or counts.get("failed"):
            logger.info("Context mailbox poll completed: %s", counts)
    return settings.context_mailbox_interval_seconds


def process_contact_hydration_once() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        job = ContactHydrationService(session).process_once()
        if job is not None:
            logger.info("Contact hydration job %s advanced to %s.", job.id, job.status)
    return settings.contact_hydration_interval_seconds


def process_memory_hygiene_once() -> int:
    settings = get_settings()
    if settings.memory_hygiene_autorun:
        with SessionLocal() as session:
            DurableMemoryHygieneService(session).run()
    return settings.memory_hygiene_interval_seconds


def process_todo_agent_tasks_once() -> int:
    settings = get_settings()
    if settings.todo_agent_worker_autorun:
        with SessionLocal() as session:
            RecurringTodoService(session).materialize_all()
            TodoAgentTaskService(session).run_once(
                claim_limit=settings.todo_agent_worker_claim_limit
            )
    return settings.todo_agent_worker_interval_seconds


def process_product_issue_agent_tasks_once() -> int:
    settings = get_settings()
    if settings.product_issue_agent_worker_autorun:
        with SessionLocal() as session:
            ProductIssueAgentTaskService(session).run_once(
                claim_limit=settings.product_issue_agent_worker_claim_limit
            )
    return settings.product_issue_agent_worker_interval_seconds


def process_routed_hygiene_once() -> int:
    settings = get_settings()
    if settings.routed_hygiene_autorun:
        with SessionLocal() as session:
            RoutedHygieneService(session).run_once()
    return settings.routed_hygiene_interval_seconds


def process_repository_intelligence_once() -> int:
    settings = get_settings()
    if settings.repository_intelligence_autorun:
        with SessionLocal() as session:
            RepositoryIntelligenceWorker(session).run_once()
    return settings.repository_intelligence_interval_seconds


def process_mobile_notifications_once() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        MobileNotificationWorker(session).run_once()
    return settings.mobile_notification_worker_interval_seconds


def initialize_worker() -> None:
    """Seed the durable records required by worker services on a fresh deployment."""
    with SessionLocal() as session:
        seed_default_domains(session)
        IdentityGroundingService(session).seed_defaults()
        AgentRegistryService(session).ensure_domain_provider_connections()
        ensure_runtime_repository(session)
        ensure_default_repository_portfolio(session)
        RecurringTodoService(session).materialize_all()


async def run_cycle_loop(
    *,
    name: str,
    cycle: Cycle,
    stop_event: asyncio.Event,
    minimum_interval_seconds: int,
    initial_interval_seconds: int,
) -> None:
    """Run a blocking cycle off the event loop until shutdown is requested."""
    interval_seconds = initial_interval_seconds
    while not stop_event.is_set():
        try:
            interval_seconds = await asyncio.to_thread(cycle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s worker cycle failed.", name)

        wait_seconds = max(minimum_interval_seconds, int(interval_seconds))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
        except TimeoutError:
            pass


async def run_worker() -> None:
    """Start all standalone worker loops and stop them cleanly on SIGINT/SIGTERM."""
    configure_logging()
    settings = get_settings()
    await asyncio.to_thread(initialize_worker)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(shutdown_signal, stop_event.set)

    tasks = [
        asyncio.create_task(
            run_cycle_loop(
                name="scheduler",
                cycle=process_scheduler_once,
                stop_event=stop_event,
                minimum_interval_seconds=5,
                initial_interval_seconds=settings.scheduler_worker_interval_seconds,
            ),
            name="maestro-scheduler-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="gmail-trigger",
                cycle=process_gmail_triggers_once,
                stop_event=stop_event,
                minimum_interval_seconds=10,
                initial_interval_seconds=settings.gmail_trigger_interval_seconds,
            ),
            name="maestro-gmail-trigger-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="memory-dropbox",
                cycle=process_memory_dropbox_once,
                stop_event=stop_event,
                minimum_interval_seconds=5,
                initial_interval_seconds=settings.memory_dropbox_interval_seconds,
            ),
            name="maestro-memory-dropbox-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="calendar-trigger",
                cycle=process_calendar_triggers_once,
                stop_event=stop_event,
                minimum_interval_seconds=10,
                initial_interval_seconds=settings.calendar_trigger_interval_seconds,
            ),
            name="maestro-calendar-trigger-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="context-mailbox",
                cycle=process_context_mailbox_once,
                stop_event=stop_event,
                minimum_interval_seconds=10,
                initial_interval_seconds=settings.context_mailbox_interval_seconds,
            ),
            name="maestro-context-mailbox-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="contact-hydration",
                cycle=process_contact_hydration_once,
                stop_event=stop_event,
                minimum_interval_seconds=5,
                initial_interval_seconds=settings.contact_hydration_interval_seconds,
            ),
            name="maestro-contact-hydration-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="memory-hygiene",
                cycle=process_memory_hygiene_once,
                stop_event=stop_event,
                minimum_interval_seconds=300,
                initial_interval_seconds=settings.memory_hygiene_interval_seconds,
            ),
            name="maestro-memory-hygiene-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="todo-agent-tasks",
                cycle=process_todo_agent_tasks_once,
                stop_event=stop_event,
                minimum_interval_seconds=10,
                initial_interval_seconds=settings.todo_agent_worker_interval_seconds,
            ),
            name="maestro-todo-agent-task-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="product-issue-agent-tasks",
                cycle=process_product_issue_agent_tasks_once,
                stop_event=stop_event,
                minimum_interval_seconds=10,
                initial_interval_seconds=settings.product_issue_agent_worker_interval_seconds,
            ),
            name="maestro-product-issue-agent-task-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="routed-hygiene",
                cycle=process_routed_hygiene_once,
                stop_event=stop_event,
                minimum_interval_seconds=300,
                initial_interval_seconds=settings.routed_hygiene_interval_seconds,
            ),
            name="maestro-routed-hygiene-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="repository-intelligence",
                cycle=process_repository_intelligence_once,
                stop_event=stop_event,
                minimum_interval_seconds=60,
                initial_interval_seconds=settings.repository_intelligence_interval_seconds,
            ),
            name="maestro-repository-intelligence-worker",
        ),
        asyncio.create_task(
            run_cycle_loop(
                name="mobile-notifications",
                cycle=process_mobile_notifications_once,
                stop_event=stop_event,
                minimum_interval_seconds=5,
                initial_interval_seconds=settings.mobile_notification_worker_interval_seconds,
            ),
            name="maestro-mobile-notification-worker",
        ),
    ]
    logger.info("Maestro background worker started with owner %s.", _worker_owner())

    await stop_event.wait()
    logger.info("Maestro background worker is shutting down.")
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _worker_owner() -> str:
    explicit_owner = os.environ.get("MAESTRO_WORKER_OWNER", "").strip()
    if explicit_owner:
        return explicit_owner
    return f"maestro-worker:{socket.gethostname()}:{os.getpid()}"


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
