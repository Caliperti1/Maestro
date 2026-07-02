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
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.maestro.scheduler_worker import SchedulerWorkerService, scheduler_worker_settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    worker_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal worker_task
        worker_task = asyncio.create_task(_scheduler_worker_loop())
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
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

    return app


async def _scheduler_worker_loop() -> None:
    while True:
        interval_seconds = get_settings().scheduler_worker_interval_seconds
        try:
            with SessionLocal() as session:
                worker_settings = scheduler_worker_settings(session)
                interval_seconds = int(worker_settings["interval_seconds"])
                if worker_settings["enabled"]:
                    SchedulerWorkerService(session).run_once(
                        owner="maestro-background-worker",
                        claim_limit=int(worker_settings["claim_limit"]),
                        execute_llm=bool(worker_settings["execute_llm"]),
                        auto_tool_loop=bool(worker_settings["auto_tool_loop"]),
                    )
        except Exception:
            logger.exception("Scheduler worker heartbeat failed.")
        await asyncio.sleep(max(5, interval_seconds))


app = create_app()
