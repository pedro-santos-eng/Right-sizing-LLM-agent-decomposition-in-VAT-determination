"""test_sweep.py — Layer-4 sweep runner (grounding HARNESS_GROUNDING_4_SWEEP §8).

Matrix enumeration counts; resume-skip; quarantine; a real subprocess round-trip
with the scripted client; budget-cap abort; kill file. No API anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import bundle_payload, fence, turn
from scripts import sweep as sweep
from scripts import sweep_common as sc
from src.harness.prompts import worker_id
from src.harness.surface import PARTITIONS
from src.oracle import generator, labeler, validator


# ---------------------------------------------------------------------------
# §1 enumeration counts (exact).
# ---------------------------------------------------------------------------


class TestEnumeration:
    def test_phase_counts_exact(self):
        assert [sc.phase_run_count(p) for p in (0, 1, 2, 3, 4)] == [25, 1000, 200, 3000, 200]
        for phase, expect in ((0, 25), (1, 1000), (2, 200), (3, 3000), (4, 200)):
            assert len(sc.enumerate_runs(phase)) == expect

    def test_total_eval_runs(self):
        assert sum(sc.phase_run_count(p) for p in (1, 2, 3, 4)) == 4400

    def test_s0prime_excluded_from_injection_cells(self):
        # Phase 3 (injections) contains only the base conditions (§1).
        conds = {r.condition for r in sc.enumerate_runs(3)}
        assert conds == set(sc.BASE_CONDITIONS)
        assert sc.S0PRIME_C2 not in conds and sc.S0PRIME_CSTAR not in conds

    def test_phase3_three_modes(self):
        modes = {r.mode for r in sc.enumerate_runs(3)}
        assert modes == {"timeout", "hallucination", "outage"}


# ---------------------------------------------------------------------------
# Runner-driven behaviors, redirected off the repo tree.
# ---------------------------------------------------------------------------


@pytest.fixture
def redirect_results(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(sc, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sc, "QUARANTINE_DIR", tmp_path / "quarantine")
    monkeypatch.setattr(sc, "STOP_FILE", tmp_path / "STOP")
    return tmp_path


def _valid_record(spec, tokens=(100, 50)):
    tin, tout = tokens
    return {
        "schema_version": sc.RAW_SCHEMA_VERSION,
        "sweep": {"phase": spec.phase, "mode": spec.mode, "condition": spec.condition,
                  "case_id": spec.case_id, "repeat": spec.repeat},
        "terminal_status": "ok",
        "emitted_trace": {"case_id": spec.case_id},
        "wall_clock": {"start_utc": "t", "end_utc": "t", "duration_ms": 1.0},
        "plan_sha256": None,
        "run_record": {"accounting": {"token_counts": {"input": tin, "output": tout, "total": tin + tout},
                                      "injection": {"mode": spec.mode, "fired": False}}},
    }


class _FakeRunner:
    """Synchronous runner: writes a record on start(); poll always done."""

    def __init__(self, make):
        self.make = make
        self.started = []

    def start(self, spec):
        self.started.append(spec)
        content = self.make(spec)
        path = sc.record_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        if content == "corrupt":
            path.write_text("{ not json", encoding="utf-8")
        elif content is not None:
            path.write_text(json.dumps(content), encoding="utf-8")
        return object()

    def poll(self, handle):
        return True

    def returncode(self, handle):
        return 0


class TestResumeQuarantine:
    def test_resume_skips_complete_runs(self, redirect_results):
        specs = sc.enumerate_runs(0)
        # Pre-write valid records for the first 10 runs.
        pre = specs[:10]
        for s in pre:
            p = sc.record_path(s)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_valid_record(s)), encoding="utf-8")
        runner = _FakeRunner(lambda s: _valid_record(s))
        result = sweep.run_phase(0, sweep.SweepConfig(n_parallel=4), runner=runner)
        assert result.skipped == 10
        assert result.completed == 15  # 25 total − 10 skipped
        assert not any(s in pre for s in runner.started)  # skipped runs never started

    def test_corrupt_record_quarantined_and_reexecuted(self, redirect_results):
        specs = sc.enumerate_runs(0)
        bad = specs[0]
        p = sc.record_path(bad)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ truncated", encoding="utf-8")  # corrupt
        runner = _FakeRunner(lambda s: _valid_record(s))
        result = sweep.run_phase(0, sweep.SweepConfig(n_parallel=4), runner=runner)
        assert result.quarantined == 1
        assert sc.quarantine_path(bad).is_file()          # moved aside
        assert bad in runner.started                       # re-executed
        assert sc.record_is_complete(bad)                  # now valid


class TestBudgetAndKill:
    def test_budget_cap_aborts_and_preserves(self, redirect_results):
        runner = _FakeRunner(lambda s: _valid_record(s, tokens=(1000, 0)))
        cfg = sweep.SweepConfig(n_parallel=1, token_caps={0: 2500})
        result = sweep.run_phase(0, cfg, runner=runner)
        assert result.aborted is True
        assert "token cap" in result.abort_reason
        assert 0 < result.completed < 25                   # some preserved, not all
        assert (sc.RAW_DIR / sc.phase_dir_name(0) / "ABORTED.json").is_file()

    def test_kill_file_honored(self, redirect_results):
        sc.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        sc.STOP_FILE.write_text("stop", encoding="utf-8")
        runner = _FakeRunner(lambda s: _valid_record(s))
        result = sweep.run_phase(0, sweep.SweepConfig(n_parallel=4), runner=runner)
        assert result.aborted is True
        assert "STOP" in result.abort_reason
        assert result.completed == 0                        # nothing started


class TestSubprocessRoundTrip:
    def test_one_real_child_scripted(self, tmp_path):
        # A genuine `python -m scripts.run_one` subprocess using the scripted
        # client (env seam), writing to a temp out-root (no repo pollution).
        ds = generator.generate_dataset(seed=42)
        case = next(c for c in ds.dev_cases if c.case_id == "dev_001")
        emitted = validator.trace_to_emitted(labeler.label_case(case))
        script = {
            worker_id("C1", g): [turn(fence(bundle_payload(g, emitted)), 10, 5)]
            for g in PARTITIONS["C1"]
        }
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        out_root = tmp_path / "raw"
        env = dict(os.environ, PYTHONPATH=str(sc._REPO_ROOT),
                   SWEEP_SCRIPTED_CLIENT=str(script_file), SWEEP_OUT_ROOT=str(out_root))
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.run_one", "0", "none", "C1", "dev_001", "0"],
            cwd=str(sc._REPO_ROOT), env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        rec_path = sc.record_path(sc.RunSpec(0, "none", "C1", "dev_001", 0), root=out_root)
        assert rec_path.is_file()
        raw = json.loads(rec_path.read_text(encoding="utf-8"))
        ok, errors = sc.validate_raw_record(raw)
        assert ok, errors
        assert raw["terminal_status"] == "ok"
        assert raw["emitted_trace"]["case_id"] == "dev_001"
        assert raw["run_record"]["accounting"]["injection"]["mode"] == "none"
        assert "duration_ms" in raw["wall_clock"]


class TestInjectionMarkersInRecords:
    def test_injected_phase_record_carries_marker(self, tmp_path):
        # A phase-3 run child (hallucination) records the injection marker + sha.
        ds = generator.generate_dataset(seed=42)
        from src.harness.injection import load_plan
        plan = load_plan()
        cls_case = next(cid for cid, t in plan["tau_by_case"].items()
                        if t == "CLS" and validator.trace_to_emitted(
                            labeler.label_case(next(c for c in ds.eval_cases if c.case_id == cid))
                        )["lines"][0]["exm"]["decision"]["exempt"] is False)
        case = next(c for c in ds.eval_cases if c.case_id == cls_case)
        emitted = validator.trace_to_emitted(labeler.label_case(case))
        script = {
            worker_id("C1", g): [turn(fence(bundle_payload(g, emitted)), 3, 2)]
            for g in PARTITIONS["C1"]
        }
        script_file = tmp_path / "s.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        out_root = tmp_path / "raw"
        env = dict(os.environ, PYTHONPATH=str(sc._REPO_ROOT),
                   SWEEP_SCRIPTED_CLIENT=str(script_file), SWEEP_OUT_ROOT=str(out_root))
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.run_one", "3", "hallucination", "C1", cls_case, "0"],
            cwd=str(sc._REPO_ROOT), env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        raw = json.loads(sc.record_path(
            sc.RunSpec(3, "hallucination", "C1", cls_case, 0), root=out_root).read_text())
        marker = raw["run_record"]["accounting"]["injection"]
        assert marker["mode"] == "hallucination" and marker["fired"] is True
        assert raw["plan_sha256"] == plan["content_sha256"]
