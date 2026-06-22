from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRegistryService,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.core.config import get_settings
from app.db.models import Artifact, MemoryItem, ToolConnection
from app.db.repositories import DomainRepository
from app.db.seed import seed_default_domains


def _seed_memory(session: Session) -> None:
    seed_default_domains(session)
    repo = DomainRepository(session)
    praxis = repo.get_by_key("praxis")
    ophi = repo.get_by_key("ophi")
    assert praxis is not None
    assert ophi is not None
    session.add_all(
        [
            MemoryItem(
                scope="global",
                memory_type="preference",
                title="Chris likes concise context",
                content="Chris prefers concise, decision-oriented context with next steps.",
                impact_level="medium",
                importance=0.8,
                metadata_={},
            ),
            MemoryItem(
                scope="domain",
                domain_id=praxis.id,
                memory_type="fact",
                title="Praxis partner follow-up",
                content="Praxis partner follow-ups should connect training and transition needs.",
                impact_level="medium",
                importance=0.9,
                metadata_={},
            ),
            MemoryItem(
                scope="domain",
                domain_id=ophi.id,
                memory_type="fact",
                title="Ophi private roadmap",
                content="This Ophi context must not appear in a Praxis prompt package.",
                impact_level="medium",
                importance=1.0,
                metadata_={},
            ),
        ]
    )
    session.commit()


def test_seed_agent_registry_returns_domain_scoped_specs(session: Session) -> None:
    specs = AgentRegistryService(session).list_specs()

    praxis = next(spec for spec in specs if spec.key == "praxis-planning-agent")
    assert praxis.domain_key == "praxis"
    assert praxis.memory_profile == "agent_prompt"
    assert [tool.key for tool in praxis.allowed_tools] == [
        "artifact.stage_interaction",
        "llm.gateway",
        "memory.context_bundle",
    ]


def test_prompt_aggregation_includes_scoped_memory_and_tools(session: Session) -> None:
    _seed_memory(session)
    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare context for a Praxis partner follow-up call.",
            query_text="partner follow-up",
            use_semantic=False,
        )
    )

    assert package.agent.domain_key == "praxis"
    assert "Praxis Defense" in package.domain_context
    assert "Praxis partner follow-up" in package.assembled_prompt
    assert "Ophi private roadmap" not in package.assembled_prompt
    assert "memory.context_bundle" in package.assembled_prompt
    assert package.memory_context.included_count >= 1


def test_updated_domain_context_flows_into_prompt_package(session: Session) -> None:
    _seed_memory(session)
    registry = AgentRegistryService(session)
    registry.update_domain_context("praxis", "Praxis edited UI context for partner work.")

    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Prepare a partner call brief.",
            use_semantic=False,
        )
    )

    assert package.domain_context == "Praxis edited UI context for partner work."
    assert "Praxis edited UI context for partner work." in package.assembled_prompt


def test_tool_manifest_can_attach_domain_connections(session: Session) -> None:
    seed_default_domains(session)
    praxis = DomainRepository(session).get_by_key("praxis")
    assert praxis is not None
    session.add(
        ToolConnection(
            domain_id=praxis.id,
            tool_key="memory.context_bundle",
            display_name="Praxis memory retrieval",
            auth_type="service",
            config={},
            is_active=True,
        )
    )
    session.commit()

    spec = AgentRegistryService(session).get_spec("praxis-planning-agent")

    memory_tool = next(tool for tool in spec.allowed_tools if tool.key == "memory.context_bundle")
    assert memory_tool.connection_id is not None
    assert memory_tool.auth_type == "service"


def test_interaction_artifact_packager_stages_package_for_curation(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    packager = InteractionArtifactPackager(session)
    package = packager.build_package(
        domain_key="praxis",
        agent_key="praxis-planning-agent",
        user_input="What should I do before the partner call?",
        maestro_tasking="Prepare a concise partner-call brief.",
        agent_output="Focus on training needs and transition risks.",
        tool_calls=[{"tool_name": "memory.context_bundle", "status": "complete"}],
        generated_artifacts=[{"name": "partner-call-brief.md", "uri": "reports/brief.md"}],
        open_questions=["Who owns the next follow-up?"],
        next_steps=["Draft agenda."],
    )

    staged = packager.stage_package(package)

    assert staged.path is not None
    staged_path = Path(staged.path)
    assert staged_path.is_file()
    assert staged_path.parent == tmp_path / "praxis" / "inbox"
    assert package.schema_version == "maestro.interaction_artifact.v1"
    artifact = session.query(Artifact).one()
    assert artifact.artifact_type == "interaction_package"
    assert artifact.metadata_["staged_for_curation"] is True
