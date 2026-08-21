from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import PromptAggregationService, PromptPackageRequest
from app.db.models import Entity, IdentityNode, IdentityRelationship
from app.db.seed import seed_default_domains
from app.maestro.context_assembler import MaestroContextAssembler
from app.maestro.identity_grounding import IdentityGroundingService


def test_identity_graph_seeds_authoritative_user_and_organization_relationships(
    session: Session,
) -> None:
    seed_default_domains(session)

    packet = IdentityGroundingService(session).build_packet(force_refresh=True)

    keys = {node["key"] for node in packet.nodes}
    relationships = {relationship["key"] for relationship in packet.relationships}
    assert "person:chris-aliperti" in keys
    assert "organization:praxis-defense" in keys
    assert "organization:perti-laboratories" in keys
    assert "chris-owns-praxis" in relationships
    assert "chris-founded-perti" in relationships
    assert "Praxis Defense is Chris Aliperti's company" in packet.rendered_text
    assert '"I", "me", and "my" refer to Chris Aliperti' in packet.rendered_text


def test_domain_grounding_packet_is_compact_and_domain_scoped(session: Session) -> None:
    seed_default_domains(session)

    packet = IdentityGroundingService(session).build_packet(
        domain_key="praxis",
        force_refresh=True,
    )

    keys = {node["key"] for node in packet.nodes}
    assert "organization:praxis-defense" in keys
    assert "organization:perti-laboratories" not in keys
    assert "domain:l3" not in keys
    assert "Praxis Defense is Chris Aliperti's company" in packet.rendered_text
    assert "Current agent domain: praxis" in packet.rendered_text
    assert len(packet.rendered_text) < 1800


def test_seed_preserves_edits_and_links_existing_canonical_organization(session: Session) -> None:
    seed_default_domains(session)
    service = IdentityGroundingService(session)
    service.seed_defaults()
    praxis_node = session.scalar(
        select(IdentityNode).where(IdentityNode.key == "organization:praxis-defense")
    )
    assert praxis_node is not None
    praxis_node.description = "User-edited authoritative Praxis description."
    session.add(
        Entity(
            name="Praxis Defense",
            normalized_name="praxis defense",
            summary="Canonical organization record.",
            source_refs=[],
            provenance={},
            metadata_={},
        )
    )
    session.commit()

    service.seed_defaults()
    session.refresh(praxis_node)

    assert praxis_node.description == "User-edited authoritative Praxis description."
    assert praxis_node.entity_id is not None


def test_agent_prompt_and_maestro_context_receive_authoritative_grounding(
    session: Session,
) -> None:
    seed_default_domains(session)

    package = PromptAggregationService(session).build_prompt_package(
        PromptPackageRequest(
            agent_key="praxis-planning-agent",
            task_instruction="Explain how this request affects Praxis.",
            use_semantic=False,
        )
    )
    maestro_bundle = MaestroContextAssembler(session).build_bundle(
        query_text="What is Praxis?",
        domain_key="praxis",
        max_chars=5000,
    )

    assert "## Authoritative Identity" in package.assembled_prompt
    assert "Praxis Defense is Chris Aliperti's company" in package.identity_grounding
    assert "Praxis Defense is Chris Aliperti's company" in maestro_bundle.rendered_text
    assert maestro_bundle.sections["identity"]["relationships"]
    assert session.query(IdentityRelationship).count() >= 1


def test_maestro_context_render_keeps_routed_objects_with_federated_results(
    session: Session,
) -> None:
    rendered = MaestroContextAssembler(session)._render(
        {
            "identity": {"rendered_text": "Chris Aliperti owns Praxis."},
            "federated": {"rendered_text": "Praxis is Chris's company."},
            "memory": {"rendered_text": ""},
            "routed_objects": {
                "rendered_text": "Events:\n- Collaborative Autonomy Standup (2026-08-24T11:00:00-04:00)"
            },
            "reports": {"items": []},
            "run_log": {"items": []},
            "artifacts": {"items": []},
        },
        max_chars=3000,
    )

    assert "## Retrieved Context" in rendered
    assert "## Routed Objects" in rendered
    assert "Collaborative Autonomy Standup" in rendered
