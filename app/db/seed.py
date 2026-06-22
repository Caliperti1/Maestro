from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain


@dataclass(frozen=True)
class DomainSeed:
    key: str
    name: str
    description: str


DEFAULT_DOMAINS = [
    DomainSeed(
        "personal",
        "Personal",
        "Personal life operations, email, calendar, reminders, and household context.",
    ),
    DomainSeed(
        "maestro-development",
        "Maestro Development",
        "Maestro introspection, architecture, backlog, GitHub, Codex, and self-improvement work.",
    ),
    DomainSeed(
        "praxis",
        "Praxis",
        "Praxis Defense business development, delivery, product, and technical operations.",
    ),
    DomainSeed(
        "ophi",
        "Ophi",
        "Ophiuchus Labs product, research, market, and technical operations.",
    ),
    DomainSeed(
        "usma",
        "USMA",
        "USMA teaching, admin, research, and academic operations.",
    ),
    DomainSeed(
        "personal-irad-projects",
        "Personal IRAD Projects",
        "Personal independent R&D projects, scaffolding, build plans, and low-priority async development.",
    ),
    DomainSeed("l3", "L3", "L3 domain operations and memory."),
]


def seed_default_domains(session: Session) -> list[Domain]:
    seeded: list[Domain] = []
    for domain_seed in DEFAULT_DOMAINS:
        domain = session.scalar(select(Domain).where(Domain.key == domain_seed.key))
        if domain is None:
            domain = Domain(
                key=domain_seed.key,
                name=domain_seed.name,
                description=domain_seed.description,
            )
            session.add(domain)
        else:
            domain.name = domain_seed.name
            domain.description = domain.description or domain_seed.description
        seeded.append(domain)
    session.commit()
    for domain in seeded:
        session.refresh(domain)
    return seeded
