"""test_runlog.py — run-record schema round-trip (grounding §7.2, §10).

Gate box: run-record schema round-trips (write -> validate -> read) on synthetic
examples; injection events present as no-op markers; wall-clock only in the
envelope.
"""

from __future__ import annotations

import pytest

from src.harness import runlog
from src.harness.runlog import (
    SEAM_RATE_TABLE_OUTAGE,
    injection_event,
    new_run_record,
    read_run_record,
    validate_run_record,
    write_run_record,
)


def _fully_populated_record() -> dict:
    rec = new_run_record(
        condition="C4",
        case_id="eval_011",
        repeat=0,
        oracle_commit="e2d2bdd22b85ea2915e3d719d7c12c6f18eac577",
        dataset_sha256="3dc683ec418666fa2e8823a2ea622bfd90f638254377d83fe95d5247563e599e",
    )
    rec["workers"].append(
        {
            "worker_id": "w_RAT",
            "assigned": ["RAT"],
            "dispatches": 1,
            "retries": 0,
            "retry_verdicts": [],
            "terminal_status": "ok",
        }
    )
    rec["tool_invocations"].append(
        {"tool": "rate_table_lookup", "arguments": {"jurisdiction": "ES", "band": "reduced"}, "result": {"rate": 0.1}}
    )
    rec["validation"]["record_verdicts"].append(
        {"subtask": "RAT", "accepted": True, "failed_checks": [], "deferred_checks": []}
    )
    rec["validation"]["final"] = {"ok": True, "failed_checks": []}
    rec["injection_events"].append(
        injection_event(SEAM_RATE_TABLE_OUTAGE, case_id="eval_011", subtask="RAT", fired=False, note="no-op")
    )
    return rec


class TestRunRecordSchema:
    def test_new_record_validates(self):
        rec = new_run_record("S0", "dev_001", 2, "commit", "sha")
        assert validate_run_record(rec).ok

    def test_full_record_validates(self):
        assert validate_run_record(_fully_populated_record()).ok

    def test_round_trip(self, tmp_path):
        rec = _fully_populated_record()
        # pre-stamp the envelope so the round-trip is byte-deterministic
        rec["envelope"]["written_at_utc"] = "2026-07-31T00:00:00Z"
        path = tmp_path / "run.json"
        write_run_record(rec, path)
        back = read_run_record(path)
        assert back == rec

    def test_writer_stamps_envelope_when_unset(self, tmp_path):
        rec = _fully_populated_record()
        assert rec["envelope"]["written_at_utc"] is None
        path = tmp_path / "run.json"
        write_run_record(rec, path)
        back = read_run_record(path)
        assert back["envelope"]["written_at_utc"] is not None
        assert back["envelope"]["written_at_utc"].endswith("Z")

    def test_malformed_record_rejected(self):
        bad = new_run_record("C1", "eval_001", 0, "commit", "sha")
        bad["identity"]["condition"] = "C9"  # not a real condition
        assert validate_run_record(bad).ok is False

    def test_extra_top_level_key_rejected(self):
        bad = new_run_record("C1", "eval_001", 0, "commit", "sha")
        bad["surprise"] = 1
        assert validate_run_record(bad).ok is False

    def test_write_rejects_invalid(self, tmp_path):
        bad = new_run_record("C1", "eval_001", 0, "commit", "sha")
        bad["workers"].append({"worker_id": "w", "assigned": ["ZZZ"], "terminal_status": "ok"})
        with pytest.raises(ValueError):
            write_run_record(bad, tmp_path / "bad.json")


class TestInjectionEvent:
    def test_builder_defaults_to_noop_marker(self):
        ev = injection_event(SEAM_RATE_TABLE_OUTAGE)
        assert ev["fired"] is False
        assert ev["seam"] == SEAM_RATE_TABLE_OUTAGE

    def test_unknown_seam_rejected(self):
        with pytest.raises(ValueError):
            injection_event("not_a_seam")
