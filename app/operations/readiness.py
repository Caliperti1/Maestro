"""Reusable database readiness check for HTTP endpoints and deployment diagnostics."""

import json
import sys
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.session import engine


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    database: str
    detail: str | None = None


def check_readiness(database_engine: Engine = engine) -> ReadinessResult:
    try:
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return ReadinessResult(
            ready=False,
            database="unavailable",
            detail=type(exc).__name__,
        )
    return ReadinessResult(ready=True, database="available")


def main() -> None:
    result = check_readiness()
    print(json.dumps(asdict(result), sort_keys=True))
    if not result.ready:
        sys.exit(1)


if __name__ == "__main__":
    main()
