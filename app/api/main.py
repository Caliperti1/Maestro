import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agents import router as agents_router
from app.api.maestro import router as maestro_router
from app.api.memory import router as memory_router
from app.api.scheduler import router as scheduler_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.maestro.scheduler_worker import SchedulerWorkerService

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(title=settings.app_name)
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

    worker_task: asyncio.Task | None = None

    @app.on_event("startup")
    async def start_scheduler_worker() -> None:
        nonlocal worker_task
        if not settings.scheduler_worker_autorun:
            return
        worker_task = asyncio.create_task(_scheduler_worker_loop())

    @app.on_event("shutdown")
    async def stop_scheduler_worker() -> None:
        if worker_task is None:
            return
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    return app


async def _scheduler_worker_loop() -> None:
    while True:
        settings = get_settings()
        try:
            with SessionLocal() as session:
                SchedulerWorkerService(session).run_once(
                    owner="maestro-background-worker",
                    claim_limit=settings.scheduler_worker_claim_limit,
                    execute_llm=settings.scheduler_worker_execute_llm,
                    auto_tool_loop=settings.scheduler_worker_auto_tool_loop,
                )
        except Exception:
            logger.exception("Scheduler worker heartbeat failed.")
        await asyncio.sleep(max(5, settings.scheduler_worker_interval_seconds))


app = create_app()
