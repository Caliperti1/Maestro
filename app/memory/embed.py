import argparse
import json
from dataclasses import asdict

from sqlalchemy import func, select

from app.db.models import MemoryEmbedding, MemoryItem
from app.db.session import SessionLocal
from app.memory.embeddings import MemoryEmbeddingService


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Maestro memory embeddings.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill = subparsers.add_parser("backfill", help="Generate embeddings for canonical memory.")
    backfill.add_argument("--limit", type=int, default=None)
    subparsers.add_parser("status", help="Print embedding coverage.")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.command == "backfill":
            results = MemoryEmbeddingService(session).backfill(limit=args.limit)
            print(json.dumps([asdict(result) for result in results], default=str, indent=2))
            return

        total_memories = session.scalar(select(func.count(MemoryItem.id))) or 0
        total_embeddings = session.scalar(select(func.count(MemoryEmbedding.id))) or 0
        by_model = session.execute(
            select(
                MemoryEmbedding.provider,
                MemoryEmbedding.model,
                func.count(MemoryEmbedding.id),
            ).group_by(MemoryEmbedding.provider, MemoryEmbedding.model)
        ).all()
        print(
            json.dumps(
                {
                    "memory_count": total_memories,
                    "embedding_count": total_embeddings,
                    "models": [
                        {"provider": provider, "model": model, "count": count}
                        for provider, model, count in by_model
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
