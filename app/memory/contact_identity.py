"""Shared identity rules for deciding whether an address represents a person."""

import re


def is_non_person_mailbox(name: str, email: str) -> bool:
    """Return true only for clear role, automation, or distribution-list addresses."""

    normalized_email = email.strip().lower()
    if "@" not in normalized_email:
        return False
    local, domain = normalized_email.split("@", 1)
    normalized_name = _normalize(name)
    identity_like_name = "@" in name or normalized_name == _normalize(local)
    if not identity_like_name:
        return False

    exact_role_names = {
        "admin",
        "alerts",
        "billing",
        "calendar",
        "contact",
        "events",
        "hello",
        "info",
        "marketing",
        "newsletter",
        "notifications",
        "office",
        "sales",
        "support",
        "team",
    }
    if local in exact_role_names:
        return True
    compact_local = re.sub(r"[^a-z0-9]+", "", local)
    if any(
        token in compact_local
        for token in ("donotreply", "mailerdaemon", "newsletter", "notification", "noreply")
    ) or any(compact_local.endswith(token) for token in ("group", "network", "office", "team")):
        return True

    # Military distribution lists commonly use dotted `.all` mailbox names.
    is_military_domain = domain in {"army.mil", "westpoint.edu"} or domain.endswith(
        (".army.mil", ".westpoint.edu")
    )
    return is_military_domain and bool(re.search(r"(?:[._-])all$", local))


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
