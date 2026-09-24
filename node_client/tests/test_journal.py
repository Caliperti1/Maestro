from __future__ import annotations

import stat

from maestro_node.journal import ExecutionJournal


def test_journal_returns_completed_result_for_replayed_idempotency_key(tmp_path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = ExecutionJournal(path)
    try:
        assert (
            journal.start(
                job_id="job-1",
                idempotency_key="idem-1",
                capability="diagnostic.echo",
                lease_generation=1,
            )
            is None
        )
        completed = journal.complete("idem-1", {"value": "hello"})
        replayed = journal.start(
            job_id="job-1",
            idempotency_key="idem-1",
            capability="diagnostic.echo",
            lease_generation=2,
        )
    finally:
        journal.close()

    assert completed.status == "completed"
    assert completed.result_digest
    assert replayed == completed
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_journal_does_not_restart_uncertain_execution(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.sqlite3")
    try:
        journal.start(
            job_id="job-1",
            idempotency_key="idem-1",
            capability="diagnostic.echo",
            lease_generation=1,
        )
        replayed = journal.start(
            job_id="job-1",
            idempotency_key="idem-1",
            capability="diagnostic.echo",
            lease_generation=2,
        )
    finally:
        journal.close()

    assert replayed is not None
    assert replayed.status == "running"

