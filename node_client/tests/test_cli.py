from __future__ import annotations

import json
import stat

import httpx

from maestro_node import cli


def test_enroll_writes_identity_and_status(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/nodes/enroll"
        payload = json.loads(request.content)
        assert payload["enrollment_code"] == "one-time-code"
        assert payload["capabilities"][0]["key"] == "diagnostic.echo"
        return httpx.Response(200, json={"node_id": "node-1", "refresh_token": "secret"})

    original_api = cli.NodeAPI

    def make_api(base_url: str):
        return original_api(base_url, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(cli, "NodeAPI", make_api)

    assert (
        cli.main(
            [
                "--data-dir",
                str(tmp_path),
                "enroll",
                "--url",
                "https://example.test",
                "--code",
                "one-time-code",
                "--name",
                "Personal Mac",
            ]
        )
        == 0
    )
    assert cli.main(["--data-dir", str(tmp_path), "status", "--json"]) == 0

    output = capsys.readouterr().out
    assert "node-1" in output
    assert stat.S_IMODE((tmp_path / "config.json").stat().st_mode) == 0o600

