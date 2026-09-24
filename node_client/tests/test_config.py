from __future__ import annotations

import json
import stat

from maestro_node.config import NodeConfig, NodePaths
from maestro_node.security import create_device_key


def permissions(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_config_and_key_are_private(tmp_path) -> None:
    paths = NodePaths(tmp_path / "node")
    create_device_key(paths.private_key)
    paths.save_config(
        NodeConfig(
            api_base_url="https://maestro.example.test/",
            node_id="node-1",
            display_name="Test Mac",
            credential="secret-value",
            private_key_path=str(paths.private_key),
        )
    )

    loaded = paths.load_config()

    assert loaded.api_base_url == "https://maestro.example.test"
    assert permissions(paths.data_dir) == 0o700
    assert permissions(paths.config) == 0o600
    assert permissions(paths.private_key) == 0o600
    assert json.loads(paths.config.read_text())["credential"] == "secret-value"


def test_runtime_state_is_private(tmp_path) -> None:
    paths = NodePaths(tmp_path / "node")
    paths.write_runtime_state({"state": "connected"})

    assert paths.read_runtime_state() == {"state": "connected"}
    assert permissions(paths.runtime_state) == 0o600

