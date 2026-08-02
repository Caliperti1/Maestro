from pathlib import Path

from scripts.restart_runtime_services import _is_within


def test_runtime_restart_path_guard() -> None:
    runtime = Path("/Users/example/Maestro-runtime")

    assert _is_within(runtime, runtime)
    assert _is_within(runtime / "frontend", runtime)
    assert not _is_within(Path("/Users/example/OtherApp"), runtime)
