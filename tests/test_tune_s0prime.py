"""test_tune_s0prime.py — the §6.3 S0′ tuning loop (scripts/tune_s0prime.py) and
the run_one loud-fail hardening (DEVLOG 2026-08-06, "third defect").

All offline: pure-function logic + the §8 scripted-client seam. No API.
"""

from __future__ import annotations

import json

import pytest

from conftest import turn
from scripts import run_one, sweep_common as sc, tune_s0prime as t
from src.harness.s0 import S0Knobs, s0_prompt_hash


# ---------------------------------------------------------------------------
# Budget targets computed from the committed scored.csv (phase 1, mode none).
# ---------------------------------------------------------------------------


class TestBudgetTargets:
    def test_computed_targets_match_ratified_values(self):
        stats = t.condition_stats()
        # The two echoed targets the operator card expects.
        assert round(stats["C2"].mean_tokens, 3) == 14590.085
        assert t.resolve_cstar(stats) == "C3"
        assert round(stats["C3"].mean_tokens, 3) == 13590.655
        assert t.budget_target(sc.S0PRIME_C2, stats) == (stats["C2"].mean_tokens, None)
        tgt, cstar = t.budget_target(sc.S0PRIME_CSTAR, stats)
        assert cstar == "C3" and tgt == stats["C3"].mean_tokens


class TestTieBreak:
    def _stat(self, cond, acc, tok):
        return t.CondStat(cond, 200, acc, tok)

    def test_tied_frame_breaks_to_lowest_tokens(self):
        # C2 and C3 tie at the max accuracy; C3 is cheaper → C* = C3.
        stats = {
            "C1": self._stat("C1", 0.72, 11316.0),
            "C2": self._stat("C2", 0.83, 14590.0),
            "C3": self._stat("C3", 0.83, 13590.0),  # same acc, fewer tokens
            "C4": self._stat("C4", 0.77, 15845.0),
        }
        assert t.resolve_cstar(stats) == "C3"

    def test_strict_accuracy_winner_ignores_tokens(self):
        # A strictly-more-accurate but pricier condition still wins (acc first).
        stats = {
            "C1": self._stat("C1", 0.90, 99999.0),
            "C2": self._stat("C2", 0.83, 100.0),
            "C3": self._stat("C3", 0.83, 90.0),
            "C4": self._stat("C4", 0.70, 50.0),
        }
        assert t.resolve_cstar(stats) == "C1"


# ---------------------------------------------------------------------------
# The deterministic knob ladder.
# ---------------------------------------------------------------------------


class TestLadder:
    def test_ladder_is_deterministic(self):
        dev = t.build_dev_and_assert()
        h1 = [s0_prompt_hash(r.knobs) for r in t.build_ladder(dev)]
        h2 = [s0_prompt_hash(r.knobs) for r in t.build_ladder(dev)]
        assert h1 == h2

    def test_rungs_monotone_in_exemplars_and_never_plain(self):
        ladder = t.build_ladder(t.build_dev_and_assert())
        counts = [len(r.knobs.exemplars) for r in ladder]
        assert counts == sorted(counts)  # non-decreasing token weight
        assert all(not r.knobs.is_plain() for r in ladder)  # every S0′ rung expands

    def test_exemplars_only_from_dev_cases(self):
        ladder = t.build_ladder(t.build_dev_and_assert())
        top = ladder[-1].knobs.exemplars
        assert top  # the top rung actually carries exemplars
        for ex in top:
            assert "dev case dev_" in ex          # rendered from a dev case
            assert "eval_" not in ex              # never an eval case


# ---------------------------------------------------------------------------
# Knob IO round-trips against the exact schema run_one._s0_knobs loads.
# ---------------------------------------------------------------------------


class TestKnobIO:
    def test_round_trip_through_run_one(self, tmp_path, monkeypatch):
        knobs = S0Knobs(
            extended_role="ROLE",
            exemplars=("EX-A", "EX-B"),
            scratchpad_instruction="SCRATCH",
        )
        t.write_knobs(sc.S0PRIME_C2, knobs, out_dir=tmp_path)
        monkeypatch.setattr(run_one, "_S0PRIME_KNOBS_DIR", tmp_path)
        loaded = run_one._s0_knobs(sc.S0PRIME_C2)
        assert loaded == knobs  # frozen dataclass equality on all three slots

    def test_written_file_has_exact_schema(self, tmp_path):
        path = t.write_knobs(sc.S0PRIME_CSTAR, S0Knobs(extended_role="r"), out_dir=tmp_path)
        d = json.loads(path.read_text(encoding="utf-8"))
        assert set(d) == {"extended_role", "exemplars", "scratchpad_instruction"}
        assert isinstance(d["exemplars"], list)


# ---------------------------------------------------------------------------
# run_one loud-fail: a S0′ condition with no committed knobs must NOT silently
# fall back to plain S0 (the third defect). Plain S0/C1-C4 stay untouched.
# ---------------------------------------------------------------------------


class TestRunOneLoudFail:
    def test_missing_knobs_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_one, "_S0PRIME_KNOBS_DIR", tmp_path)  # empty dir
        with pytest.raises(SystemExit) as exc:
            run_one._s0_knobs(sc.S0PRIME_C2)
        assert sc.S0PRIME_C2 in str(exc.value)

    def test_present_knobs_load(self, tmp_path, monkeypatch):
        t.write_knobs(sc.S0PRIME_CSTAR, S0Knobs(extended_role="r"), out_dir=tmp_path)
        monkeypatch.setattr(run_one, "_S0PRIME_KNOBS_DIR", tmp_path)
        assert run_one._s0_knobs(sc.S0PRIME_CSTAR).extended_role == "r"

    def test_non_s0prime_conditions_are_plain_without_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_one, "_S0PRIME_KNOBS_DIR", tmp_path)  # empty
        assert run_one._s0_knobs("S0").is_plain()  # no loud-fail for plain S0/C*


# ---------------------------------------------------------------------------
# The greedy bracket over the ladder, driven by scripted token counts (no API).
# ---------------------------------------------------------------------------


class TestBracket:
    def test_walks_up_from_below_into_band(self):
        # Start below the band; each step up adds tokens until one lands in band.
        target = 13590.655  # band ≈ [12231.6, 14949.7]
        counts = {0: 8000, 1: 9500, 2: 11000, 3: 12000, 4: 13600, 5: 15200, 6: 16800}
        res = t.bracket_search(7, target, lambda i: counts[i], start_index=3, max_iters=6)
        assert res["in_band"] is True
        assert res["chosen_index"] == 4 and res["chosen_tokens"] == 13600
        path = [(s["rung_index"], s["verdict"]) for s in res["iterations"]]
        assert path == [(3, "below"), (4, "in_band")]

    def test_walks_down_from_above_into_band(self):
        target = 13590.655  # band ≈ [12231.6, 14949.7]
        counts = {0: 8000, 1: 10000, 2: 13000, 3: 16000, 4: 18000, 5: 20000, 6: 22000}
        res = t.bracket_search(7, target, lambda i: counts[i], start_index=3, max_iters=6)
        assert res["in_band"] is True
        assert res["chosen_index"] == 2 and res["chosen_tokens"] == 13000
        path = [(s["rung_index"], s["verdict"]) for s in res["iterations"]]
        assert path == [(3, "above"), (2, "in_band")]

    def test_bracketed_with_no_in_band_rung_keeps_closest(self):
        # target falls BETWEEN two adjacent rungs, neither in band; the walk
        # brackets it (3 below → 4 above → would revisit 3) and keeps the closest.
        target = 13590.655
        counts = {0: 8000, 1: 10000, 2: 11000, 3: 12000, 4: 15200, 5: 17000, 6: 19000}
        res = t.bracket_search(7, target, lambda i: counts[i], start_index=3, max_iters=6)
        assert res["in_band"] is False
        # |12000-13590.655|=1590.655 < |15200-13590.655|=1609.345 → rung 3.
        assert res["chosen_index"] == 3

    def test_exhaustion_keeps_closest_and_flags_out_of_band(self):
        # Every rung far below target: walk to the top, never in band, flag it.
        target = 50000.0
        counts = {i: 1000 * (i + 1) for i in range(7)}
        res = t.bracket_search(7, target, lambda i: counts[i], start_index=3, max_iters=6)
        assert res["in_band"] is False
        assert res["chosen_index"] == 6 and res["chosen_tokens"] == 7000  # closest to target


# ---------------------------------------------------------------------------
# measure_config integration: it runs the harness execution call (run_s0) on dev
# cases and sums the same total_tokens accounting scored.csv reports (incl.
# repairs). Driven by the scripted client — no API, temp out-root only.
# ---------------------------------------------------------------------------


class TestMeasureIntegration:
    def test_measure_runs_s0_and_sums_tokens(self, tmp_path, monkeypatch):
        # A never-validating turn (no fenced json) forces initial + 3 repairs =
        # 4 model calls per case, each 150 tokens → 600 per case, mean 600.
        script = {"S0": [turn("no json here", 100, 50) for _ in range(4)]}
        script_file = tmp_path / "script.json"
        script_file.write_text(json.dumps(script), encoding="utf-8")
        monkeypatch.setenv("SWEEP_SCRIPTED_CLIENT", str(script_file))

        factory = t._make_client_factory()
        dev = t.build_dev_and_assert()[:2]  # two dev cases is enough
        knobs = t.build_ladder(t.build_dev_and_assert())[0].knobs
        mean, per_case = t.measure_config(knobs, dev, factory, tmp_path / "out")

        assert mean == 600.0
        assert [pc["case_id"] for pc in per_case] == ["dev_001", "dev_002"]
        assert all(pc["status"] == "validation_exhausted" for pc in per_case)
        assert all(pc["total_tokens"] == 600 for pc in per_case)
        # provenance written under the temp out-root, never results/raw/.
        assert (tmp_path / "out" / "dev_001.json").is_file()

    def test_factory_requires_a_client_source(self, monkeypatch):
        monkeypatch.delenv("SWEEP_SCRIPTED_CLIENT", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(SystemExit):
            t._make_client_factory()
