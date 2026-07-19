"""FastAPI application factory and process-local background services.

The backend exposes Maestro's HTTP/WebSocket API and starts a lightweight scheduler heartbeat in
the same process. The worker reads runtime settings from the database on every loop so the UI can
turn autonomous execution on/off without editing `.env` or restarting the app.
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agents import router as agents_router
from app.api.maestro import router as maestro_router
from app.api.memory import router as memory_router
from app.api.scheduler import router as scheduler_router
from app.api.workflow_outputs import router as workflow_outputs_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.maestro.scheduler_worker import SchedulerWorkerService, scheduler_worker_settings
from app.memory.dropbox import MemoryDropboxProcessor

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    worker_tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker_tasks.extend(
            [
                asyncio.create_task(_scheduler_worker_loop()),
                asyncio.create_task(_memory_dropbox_worker_loop()),
            ]
        )
        try:
            yield
        finally:
            for worker_task in worker_tasks:
                worker_task.cancel()
            for worker_task in worker_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.app_name}

    app.include_router(memory_router)
    app.include_router(agents_router)
    app.include_router(maestro_router)
    app.include_router(scheduler_router)
    app.include_router(workflow_outputs_router)

    return app


async def _scheduler_worker_loop() -> None:
    while True:
        interval_seconds = get_settings().scheduler_worker_interval_seconds
        try:
            interval_seconds = await asyncio.to_thread(_process_scheduler_work_once)
        except Exception:
            logger.exception("Scheduler worker heartbeat failed.")
        await asyncio.sleep(max(5, interval_seconds))


def _process_scheduler_work_once() -> int:
    """Run one blocking scheduler heartbeat without occupying FastAPI's event loop."""
    with SessionLocal() as session:
        worker_settings = scheduler_worker_settings(session)
        if worker_settings["enabled"]:
            SchedulerWorkerService(session).run_once(
                owner="maestro-background-worker",
                claim_limit=int(worker_settings["claim_limit"]),
                execute_llm=bool(worker_settings["execute_llm"]),
                auto_tool_loop=bool(worker_settings["auto_tool_loop"]),
            )
        return int(worker_settings["interval_seconds"])


async def _memory_dropbox_worker_loop() -> None:
    while True:
        settings = get_settings()
        try:
            if settings.memory_dropbox_autorun:
                await asyncio.to_thread(_process_memory_dropbox_once)
        except Exception:
            logger.exception("Memory dropbox worker heartbeat failed.")
        await asyncio.sleep(max(5, settings.memory_dropbox_interval_seconds))


def _process_memory_dropbox_once() -> None:
    with SessionLocal() as session:
        results = MemoryDropboxProcessor(session).process_once()
        if results:
            logger.info(
                "Memory dropbox worker processed %s artifact(s): %s",
                len(results),
                ", ".join(result.status for result in results),
            )


app = create_app()
