"""Explicit local capability allowlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class CapabilityError(RuntimeError):
    """A safe, reportable capability execution error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Capability:
    key: str
    version: str
    mode: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]


def diagnostic_echo(input_envelope: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded JSON value without touching local machine resources."""

    value = input_envelope.get("value")
    if len(repr(value)) > 64_000:
        raise CapabilityError("input_too_large", "Echo input exceeds the 64 KB local limit.")
    return {"value": value}


CAPABILITIES: dict[str, Capability] = {
    "diagnostic.echo": Capability(
        key="diagnostic.echo", version="1.0.0", mode="read", handler=diagnostic_echo
    )
}


def manifest(keys: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {"key": capability.key, "version": capability.version, "mode": capability.mode}
        for key in keys
        if (capability := CAPABILITIES.get(key)) is not None
    ]


def execute(key: str, input_envelope: dict[str, Any]) -> dict[str, Any]:
    capability = CAPABILITIES.get(key)
    if capability is None:
        raise CapabilityError("capability_not_allowed", f"Capability {key!r} is not installed.")
    return capability.handler(input_envelope)

