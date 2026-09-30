"""Local configuration and credential storage for a Maestro node."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(RuntimeError):
    """Raised when local node configuration is missing or malformed."""


def default_data_dir() -> Path:
    """Return an OS-appropriate per-user application data directory."""

    override = os.environ.get("MAESTRO_NODE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Maestro Node"
    xdg_home = os.environ.get("XDG_DATA_HOME")
    if xdg_home:
        return Path(xdg_home).expanduser() / "maestro-node"
    return Path.home() / ".local" / "share" / "maestro-node"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write_private(path: Path, content: str) -> None:
    """Atomically write a UTF-8 file readable only by the current user."""

    ensure_private_dir(path.parent)
    file_descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class NodeConfig:
    api_base_url: str
    node_id: str
    display_name: str
    credential: str
    private_key_path: str
    capabilities: tuple[str, ...] = ("diagnostic.echo",)
    protocol_version: str = "1"

    def to_json(self) -> str:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NodeConfig:
        try:
            return cls(
                api_base_url=str(value["api_base_url"]).rstrip("/"),
                node_id=str(value["node_id"]),
                display_name=str(value["display_name"]),
                credential=str(value["credential"]),
                private_key_path=str(value["private_key_path"]),
                capabilities=tuple(str(item) for item in value.get("capabilities", [])),
                protocol_version=str(value.get("protocol_version", "1")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError("Node configuration is malformed.") from exc


class NodePaths:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()

    @property
    def config(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def private_key(self) -> Path:
        return self.data_dir / "device.key"

    @property
    def journal(self) -> Path:
        return self.data_dir / "journal.sqlite3"

    @property
    def runtime_state(self) -> Path:
        return self.data_dir / "runtime.json"

    def load_config(self) -> NodeConfig:
        try:
            value = json.loads(self.config.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"No node configuration found at {self.config}. Run `maestro-node enroll` first."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid JSON in {self.config}.") from exc
        if not isinstance(value, dict):
            raise ConfigurationError("Node configuration must be a JSON object.")
        config = NodeConfig.from_dict(value)
        self.secure_existing_files()
        return config

    def save_config(self, config: NodeConfig) -> None:
        atomic_write_private(self.config, config.to_json())

    def write_runtime_state(self, value: dict[str, Any]) -> None:
        atomic_write_private(
            self.runtime_state, json.dumps(value, indent=2, sort_keys=True) + "\n"
        )

    def read_runtime_state(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.runtime_state.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def secure_existing_files(self) -> None:
        ensure_private_dir(self.data_dir)
        for path in (self.config, self.private_key, self.journal, self.runtime_state):
            if path.exists():
                os.chmod(path, 0o600)

