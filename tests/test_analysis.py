"""test_analysis.py — offline analysis primitives + assembly
(grounding HARNESS_GROUNDING_4_SWEEP §6, §8).

Hand-computed bootstrap/permutation/d_z under the pinned seeds; Holm ordering on
a constructed p-set; §6.6 criteria on constructed outcomes; build_analysis on a
tiny fixture.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from scripts import analyze
from scripts import sweep_common as sc


class TestPrimitives:
    def test_permutation_exact_small_fixture(self):
        # diffs all +0.5, n=3 → only (+++)=0.5 and (---)=-0.5 reach |mean|≥0.5
        # → 2 / 2^3 = 0.25, EXACTLY (exact enumeration branch).
        assert analyze.permutation_p(np.array([0.5, 0.5, 0.5])) == pytest.approx(0.25)

    def test_permutation_all_positive_distinct(self):
        # diffs [0.1,0.2,0.3]: |mean| maximised only by all-same-sign → 2/8 = 0.25
        assert analyze.permutation_p(np.array([0.1, 0.2, 0.3])) == pytest.approx(0.25)

    def test_cohen_dz_hand(self):
        # mean 0.4, sd(ddof=1)=0.2 → 2.0
        assert analyze.cohen_dz(np.array([0.2, 0.4, 0.6])) == pytest.approx(2.0)

    def test_bootstrap_ci_degenerate_is_exact(self):
        lo, hi = analyze.bootstrap_ci(np.array([0.5, 0.5, 0.5]))
        assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)

    def test_bootstrap_ci_deterministic_under_seed(self):
        d = np.array([0.1, -0.2, 0.3, 0.4, -0.1])
        assert analyze.bootstrap_ci(d) == analyze.bootstrap_ci(d)  # pinned seed → identical

    def test_sampled_permutation_addone_never_zero(self):
        # k=16 forces the sampled branch (2^16 > 2^14). Strongly one-sided
        # diffs: p must be >= 1/(1+B) and deterministic under the pinned seed.
        diffs = np.arange(1.0, 17.0)
        p1 = analyze.permutation_p(diffs)
        p2 = analyze.permutation_p(diffs)
        assert p1 == p2
        assert p1 >= 1.0 / (1 + analyze.N_PERMUTATION)
        # zeros: every permutation ties the observed mean -> p == 1 exactly.
        assert analyze.permutation_p(np.zeros(16)) == pytest.approx(1.0)

    def test_holm_ordering_on_constructed_pset(self):
        h = analyze.holm_bonferroni({"a": 0.01, "b": 0.04, "c": 0.02, "d": 0.5})
        # sorted 0.01,0.02,0.04,0.5 → ×4,×3,×2,×1 = 0.04,0.06,0.08,0.5 (monotone)
        assert h["a"]["holm_adjusted"] == pytest.approx(0.04) and h["a"]["reject"] is True
        assert h["c"]["holm_adjusted"] == pytest.approx(0.06) and h["c"]["reject"] is False
        assert h["b"]["holm_adjusted"] == pytest.approx(0.08) and h["b"]["reject"] is False
        assert h["d"]["holm_adjusted"] == pytest.approx(0.5) and h["d"]["reject"] is False


class TestInjectionDeltaBootstrap:
    """§8 injection-delta bootstrap primitive — exact reference algorithm, no API."""

    def test_degenerate_ci_is_the_value(self):
        lo, hi = analyze.delta_bootstrap_ci(np.array([0.16, 0.16, 0.16]))
        assert lo == pytest.approx(0.16) and hi == pytest.approx(0.16)

    def test_reproduces_reference_algorithm(self):
        d = np.array([-1.0, 0.0, 1.0, 0.0, 0.5, -0.5])
        # Hand-mirror the pinned algorithm: default_rng(42), 1000 draws of
        # rng.choice(d, len(d), replace=True).mean(), percentile [2.5, 97.5].
        rng = np.random.default_rng(analyze.INJECTION_DELTA_SEED)
        means = np.array([rng.choice(d, size=len(d), replace=True).mean()
                          for _ in range(analyze.N_DELTA_BOOTSTRAP)])
        exp_lo, exp_hi = np.percentile(means, [2.5, 97.5])
        lo, hi = analyze.delta_bootstrap_ci(d)
        assert lo == pytest.approx(exp_lo) and hi == pytest.approx(exp_hi)

    def test_deterministic_under_seed(self):
        d = np.array([0.1, -0.2, 0.3, 0.4, -0.1, 0.0, 0.2])
        assert analyze.delta_bootstrap_ci(d) == analyze.delta_bootstrap_ci(d)

    def test_empty_is_nan(self):
        lo, hi = analyze.delta_bootstrap_ci(np.array([]))
        assert np.isnan(lo) and np.isnan(hi)


def _cl_fixture(cond_acc_tok: dict[str, tuple[list[float], list[float]]],
                s0prime_c2: tuple[list[float], list[float]] | None = None) -> pd.DataFrame:
    """Build a case_level frame directly: 4 cases per condition, phase per
    analyze._CONDITION_PHASE, mode 'none'."""
    rows = []
    def add(cond, phase, accs, toks):
        for i, (a, t) in enumerate(zip(accs, toks), 1):
            rows.append(dict(phase=phase, mode="none", condition=cond,
                             case_id=f"eval_{i:03d}", acc=a, tokens=t, latency_ms=1.0))
    for cond, (accs, toks) in cond_acc_tok.items():
        add(cond, 1, accs, toks)
    if s0prime_c2 is not None:
        add(sc.S0PRIME_C2, 2, *s0prime_c2)
    return pd.DataFrame(rows)


def _c(material: bool, n: int = 4, ci=(0.1, 0.5), holm_reject=None, mean_diff=0.3):
    d = {"material_positive": material, "n": n, "ci_low": ci[0], "ci_high": ci[1],
         "mean_diff": mean_diff, "cohen_dz": 0.9 if material else 0.05}
    if holm_reject is not None:
        d["holm_reject"] = holm_reject
    return d


class TestFalsificationMechanism:
    """§6.6, faithfully: the three criteria on constructed outcomes."""

    def test_rq1_supported_via_c3_nonmonotonic(self):
        cl = _cl_fixture({"S0": ([0.2]*4, [100]*4), "C1": ([0.2]*4, [100]*4),
                          "C2": ([0.4]*4, [100]*4), "C3": ([0.9]*4, [100]*4),
                          "C4": ([0.3]*4, [100]*4)})
        cbn = {"C2_vs_C1": _c(False), "C2_vs_C4": _c(False),
               "C3_vs_C1": _c(True), "C3_vs_C4": _c(True)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert t.loc["intermediate_optimum_RQ1", "triggered"] == False

    def test_rq1_triggered_when_no_intermediate_beats_both(self):
        cl = _cl_fixture({"S0": ([0.2]*4, [100]*4), "C1": ([0.2]*4, [100]*4),
                          "C2": ([0.4]*4, [100]*4), "C3": ([0.9]*4, [100]*4),
                          "C4": ([0.3]*4, [100]*4)})
        cbn = {"C2_vs_C1": _c(True), "C2_vs_C4": _c(False),
               "C3_vs_C1": _c(True), "C3_vs_C4": _c(False)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert t.loc["intermediate_optimum_RQ1", "triggered"] == True

    def test_rq1_auto_triggered_on_monotonic_sequence(self):
        # C3 materially beats both, but C1..C4 accuracy is monotone increasing
        # -> §6.6(1) "automatically unmet".
        cl = _cl_fixture({"S0": ([0.1]*4, [100]*4), "C1": ([0.2]*4, [100]*4),
                          "C2": ([0.4]*4, [100]*4), "C3": ([0.6]*4, [100]*4),
                          "C4": ([0.8]*4, [100]*4)})
        cbn = {"C2_vs_C1": _c(False), "C2_vs_C4": _c(False),
               "C3_vs_C1": _c(True), "C3_vs_C4": _c(True)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert analyze.accuracy_sequence_monotonic(cl) is True
        assert t.loc["intermediate_optimum_RQ1", "triggered"] == True

    def test_rq2_pareto_dominance_with_tie_triggers(self):
        # S0 ties C1 on both axes and strictly beats the rest -> weak dominance.
        cl = _cl_fixture({"S0": ([0.8]*4, [100]*4), "C1": ([0.8]*4, [100]*4),
                          "C2": ([0.7]*4, [120]*4), "C3": ([0.6]*4, [130]*4),
                          "C4": ([0.5]*4, [140]*4)})
        t = analyze.evaluate_falsification(cl, {}).set_index("criterion")
        assert analyze.s0_weakly_pareto_dominates_all(cl) is True
        assert t.loc["orchestration_benefit_RQ2", "triggered"] == True

    def test_rq2_not_triggered_when_any_axis_worse(self):
        # S0 more accurate everywhere but costlier than C2 -> no dominance.
        cl = _cl_fixture({"S0": ([0.9]*4, [110]*4), "C1": ([0.8]*4, [120]*4),
                          "C2": ([0.7]*4, [90]*4), "C3": ([0.6]*4, [130]*4),
                          "C4": ([0.5]*4, [140]*4)})
        t = analyze.evaluate_falsification(cl, {}).set_index("criterion")
        assert t.loc["orchestration_benefit_RQ2", "triggered"] == False

    def test_rq3_conjunction(self):
        cl = _cl_fixture({"S0": ([0.5]*4, [100]*4), "C1": ([0.5]*4, [100]*4),
                          "C2": ([0.5]*4, [100]*4), "C3": ([0.5]*4, [100]*4),
                          "C4": ([0.5]*4, [100]*4)})
        # not significant + CI spans zero -> triggered
        cbn = {"S0primeC2_vs_C2": _c(False, ci=(-0.1, 0.1), holm_reject=False, mean_diff=0.0)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert t.loc["prompt_budget_confound_RQ3", "triggered"] == True
        # significant -> not triggered even if CI spans zero is impossible; use excl.
        cbn = {"S0primeC2_vs_C2": _c(True, ci=(0.05, 0.3), holm_reject=True)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert t.loc["prompt_budget_confound_RQ3", "triggered"] == False
        # not significant but CI excludes zero -> not triggered (conjunction).
        cbn = {"S0primeC2_vs_C2": _c(False, ci=(0.02, 0.3), holm_reject=False)}
        t = analyze.evaluate_falsification(cl, cbn).set_index("criterion")
        assert t.loc["prompt_budget_confound_RQ3", "triggered"] == False
        # phase 2 absent -> undeterminable (None).
        t = analyze.evaluate_falsification(cl, {}).set_index("criterion")
        assert t.loc["prompt_budget_confound_RQ3", "triggered"] is None


def _tiny_scored():
    rows = []
    cases = [f"eval_{i:03d}" for i in range(1, 5)]
    acc = {"S0": [0, 0, 0, 1], "C1": [1, 1, 0, 1], "C2": [1, 1, 1, 1], "C4": [1, 0, 0, 1]}

    def _run(**kw):
        a = kw["final_answer_accuracy"]
        # main_table/error_types read terminal_status + earliest_failing_subtask:
        # a failed run is validation_exhausted and fails earliest at JUR here.
        kw.setdefault("terminal", True)
        kw.setdefault("terminal_status", "ok" if a else "validation_exhausted")
        kw.setdefault("earliest_failing_subtask", None if a else "JUR")
        rows.append(kw)

    for cond, a in acc.items():
        for ci, case in enumerate(cases):
            for rep in range(5):
                _run(phase=1, mode="none", condition=cond, case_id=case, repeat=rep,
                     final_answer_accuracy=a[ci], total_tokens=100 + ci, latency_ms=10.0,
                     trace_consistent=bool(a[ci]), substitution_success=None)
    for ci, case in enumerate(cases):
        for rep in range(5):
            _run(phase=2, mode="none", condition=sc.S0PRIME_C2, case_id=case, repeat=rep,
                 final_answer_accuracy=[1, 1, 0, 1][ci], total_tokens=200, latency_ms=20.0,
                 trace_consistent=True, substitution_success=None)
    for ci, case in enumerate(cases):
        for rep in range(5):
            _run(phase=3, mode="hallucination", condition="C1", case_id=case, repeat=rep,
                 final_answer_accuracy=0, total_tokens=150, latency_ms=15.0,
                 trace_consistent=False, substitution_success=(ci % 2 == 0))
    return pd.DataFrame(rows)


class TestBuildAnalysis:
    def test_tables_and_hand_values(self, tmp_path):
        # raw_dir → empty tmp so the s0_family prompt-hash lookup stays hermetic.
        tables = analyze.build_analysis(_tiny_scored(), raw_dir=tmp_path)
        assert set(tables) == {"case_level", "main_table", "error_types", "s0_family",
                               "headline_contrasts", "supplementary_contrasts",
                               "injection_cells", "injection_deltas", "falsification"}
        fal = tables["falsification"].set_index("criterion")
        assert list(fal.index) == ["intermediate_optimum_RQ1", "orchestration_benefit_RQ2",
                                   "prompt_budget_confound_RQ3"]
        # tiny fixture has no C3 -> RQ1 undeterminable (missing contrasts);
        # RQ2 is definitively False: C1 already defeats S0 dominance on accuracy,
        # so the missing C3 cannot change the verdict (early definitive exit).
        assert fal.loc["intermediate_optimum_RQ1", "triggered"] is None
        assert fal.loc["orchestration_benefit_RQ2", "triggered"] == False
        supp = tables["supplementary_contrasts"].set_index("name")
        assert list(supp.index) == ["C2_vs_C4", "C3_vs_C1", "C3_vs_C4"]
        # C2 [1,1,1,1] − C4 [1,0,0,1] -> diffs [0,1,1,0], mean 0.5
        assert supp.loc["C2_vs_C4", "mean_diff"] == pytest.approx(0.5)

        hc = tables["headline_contrasts"].set_index("name")
        # S0 case accs [0,0,0,1]; C1 [1,1,0,1] → diffs [-1,-1,0,0], mean −0.5
        assert hc.loc["S0_vs_C1", "mean_diff"] == pytest.approx(-0.5)
        # C2 [1,1,1,1] − C1 [1,1,0,1] → diffs [0,0,1,0], mean 0.25
        assert hc.loc["C2_vs_C1", "mean_diff"] == pytest.approx(0.25)
        assert hc.loc["S0_vs_C1", "n"] == 4

        # injection cell: substitution rate over [T,F,T,F] means = 0.5; token
        # penalty = 150 − mean(C1 baseline tokens 100,101,102,103 = 101.5) = 48.5
        ic = tables["injection_cells"].iloc[0]
        assert ic["substitution_success_rate"] == pytest.approx(0.5)
        assert ic["token_cost_penalty_vs_baseline"] == pytest.approx(48.5)
        assert ic["all_case_accuracy"] == pytest.approx(0.0)
        # record_substituted absent from the fixture → NaN (hallucination-only
        # column, not applicable / None elsewhere).
        assert np.isnan(ic["record_substituted_rate"])

        # injection delta: hallucination-C1 injected acc all 0 minus phase-1 C1
        # baseline case means [1,1,0,1] → deltas [-1,-1,0,-1], mean −0.75, n=4.
        idl = tables["injection_deltas"].set_index(["mode", "condition"])
        assert idl.loc[("hallucination", "C1"), "mean_delta"] == pytest.approx(-0.75)
        assert idl.loc[("hallucination", "C1"), "n_cases"] == 4

        # every headline test carries a Holm-adjusted p and reject flag.
        assert tables["headline_contrasts"]["holm_adjusted"].notna().all()

        # main_table: S0 case accs [0,0,0,1] → 0.25; C2 all 1 → 1.0; C3 absent →
        # n_cases 0. S0 fails 3/4 cases (×5 reps) → terminal_failure_rate 0.75,
        # trace_consistent_mean 0.25; latency 10ms → 0.01s.
        mt = tables["main_table"].set_index("condition")
        assert mt.loc["S0", "acc_mean"] == pytest.approx(0.25)
        assert mt.loc["C2", "acc_mean"] == pytest.approx(1.0)
        assert mt.loc["C3", "n_cases"] == 0
        assert mt.loc["S0", "terminal_failure_rate"] == pytest.approx(0.75)
        assert mt.loc["S0", "trace_consistent_mean"] == pytest.approx(0.25)
        assert mt.loc["S0", "latency_s_median"] == pytest.approx(0.01)

        # error_types: S0 has 3 failing cases ×5 reps = 15 runs, all earliest JUR;
        # zeros filled for CLS/RAT/EXM/RCH; total = 15.
        et = tables["error_types"].set_index("condition")
        assert list(et.columns) == ["CLS", "JUR", "RAT", "EXM", "RCH", "total"]
        assert et.loc["S0", "JUR"] == 15 and et.loc["S0", "CLS"] == 0
        assert et.loc["S0", "total"] == 15
        assert et.loc["C2", "total"] == 0  # C2 never fails

        # s0_family: S0 is the untuned reference (target/in_band n/a); S0prime_C2
        # tokens 200 vs target 14590.085 → far below band → in_band False; empty
        # raw_dir → prompt_hash "".
        sf = tables["s0_family"].set_index("arm")
        assert list(sf.index) == ["S0", "S0prime_C2", "S0prime_Cstar"]
        assert sf.loc["S0", "in_band"] is None and np.isnan(sf.loc["S0", "deviation_pct"])
        assert sf.loc["S0prime_C2", "acc_mean"] == pytest.approx(0.75)
        assert sf.loc["S0prime_C2", "deviation_pct"] < 0 and sf.loc["S0prime_C2", "in_band"] is False
        assert (sf["prompt_hash"] == "").all()

    def test_no_tail_percentile_latency(self, tmp_path):
        # §6.1: latency is medians only — no p95/p99 anywhere in the outputs.
        tables = analyze.build_analysis(_tiny_scored(), raw_dir=tmp_path)
        for df in tables.values():
            cols = " ".join(df.columns).lower()
            assert "p95" not in cols and "p99" not in cols


class TestPaperTables:
    """§7 paper tables — the bootstrap generaliser and per-table structure."""

    def test_bootstrap_stat_ci_matches_delta_engine_for_mean(self):
        # stat=np.mean must reproduce delta_bootstrap_ci byte-for-byte (same
        # default_rng(42), same rng.choice stream).
        d = np.array([-1.0, 0.0, 1.0, 0.0, 0.5, -0.5])
        assert analyze._bootstrap_stat_ci(d, np.mean) == analyze.delta_bootstrap_ci(d)

    def test_bootstrap_stat_ci_degenerate_and_deterministic(self):
        lo, hi = analyze._bootstrap_stat_ci(np.array([7.0, 7.0, 7.0]), np.median)
        assert lo == pytest.approx(7.0) and hi == pytest.approx(7.0)
        d = np.array([1.0, 4.0, 9.0, 2.0, 5.0])
        assert analyze._bootstrap_stat_ci(d, np.median) == analyze._bootstrap_stat_ci(d, np.median)

    def test_bootstrap_stat_ci_empty_is_nan(self):
        lo, hi = analyze._bootstrap_stat_ci(np.array([]), np.mean)
        assert np.isnan(lo) and np.isnan(hi)

    def test_main_table_structure_and_values(self):
        mt = analyze.main_table(_tiny_scored()).set_index("condition")
        assert list(mt.index) == list(analyze.MAIN_TABLE_CONDITIONS)
        # C4 case accs [1,0,0,1] → mean 0.5; tokens all 103 → mean 103; latency
        # 10ms → 0.01s; every run ok+consistent for its passing repeats.
        assert mt.loc["C4", "acc_mean"] == pytest.approx(0.5)
        # per-case tokens 100,101,102,103 → mean 101.5; CI brackets the mean.
        assert mt.loc["C4", "total_tokens_mean"] == pytest.approx(101.5)
        assert (mt.loc["C4", "total_tokens_ci_low"] <= 101.5
                <= mt.loc["C4", "total_tokens_ci_high"])
        # constant latency 10ms → median 0.01s and a degenerate (collapsed) CI.
        assert mt.loc["C4", "latency_s_median"] == pytest.approx(0.01)
        assert mt.loc["C4", "latency_s_ci_low"] == pytest.approx(0.01)
        assert mt.loc["C4", "latency_s_ci_high"] == pytest.approx(0.01)

    def test_error_types_zeros_filled_and_totals(self):
        et = analyze.error_types(_tiny_scored()).set_index("condition")
        # C1 fails one case (eval_003) ×5 reps, all earliest JUR.
        assert et.loc["C1", "JUR"] == 5 and et.loc["C1", "total"] == 5
        # per-row total equals the row's subtask-count sum (zeros filled).
        for cond in analyze.MAIN_TABLE_CONDITIONS:
            assert et.loc[cond, "total"] == int(
                sum(et.loc[cond, s] for s in analyze.ERROR_SUBTASK_ORDER))

    def test_s0_family_deviation_band_and_prompt_hash(self, tmp_path):
        # A record placed where s0_family looks for S0prime_C2's prompt hash.
        rec = (tmp_path / "phase2" / "none" / sc.S0PRIME_C2 / "eval_001")
        rec.mkdir(parents=True)
        (rec / "r0.json").write_text(json.dumps(
            {"run_record": {"accounting": {"prompt_hashes": {"S0": "cafef00d"}}}}),
            encoding="utf-8")
        sf = analyze.s0_family(_tiny_scored(), raw_dir=tmp_path).set_index("arm")
        # S0 reference row: no target → deviation NaN, in_band None.
        assert np.isnan(sf.loc["S0", "target_tokens"]) and sf.loc["S0", "in_band"] is None
        # S0prime_C2 tokens 200 vs target 14590.085 → deviation ≈ -98.6%, out of band.
        assert sf.loc["S0prime_C2", "deviation_pct"] == pytest.approx(
            (200 - 14590.085) / 14590.085 * 100.0)
        assert sf.loc["S0prime_C2", "in_band"] is False
        assert sf.loc["S0prime_C2", "prompt_hash"] == "cafef00d"
        # S0prime_Cstar has no phase-4 rows and no raw record → empty hash, 0 cases.
        assert sf.loc["S0prime_Cstar", "n_cases"] == 0
        assert sf.loc["S0prime_Cstar", "prompt_hash"] == ""

    def test_s0_family_in_band_true_at_boundary(self):
        # Feed S0prime_C2 exactly its target token cost → deviation 0 → in band.
        rows = []
        for ci in range(4):
            for rep in range(5):
                rows.append(dict(phase=2, mode="none", condition=sc.S0PRIME_C2,
                                 case_id=f"eval_{ci+1:03d}", repeat=rep,
                                 final_answer_accuracy=1, total_tokens=14590.085,
                                 latency_ms=1.0, trace_consistent=True))
        sf = analyze.s0_family(pd.DataFrame(rows), raw_dir="does_not_exist").set_index("arm")
        assert sf.loc["S0prime_C2", "deviation_pct"] == pytest.approx(0.0)
        assert sf.loc["S0prime_C2", "in_band"] is True
