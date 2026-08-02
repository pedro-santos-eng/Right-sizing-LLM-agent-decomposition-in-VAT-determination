"""test_analysis.py — offline analysis primitives + assembly
(grounding HARNESS_GROUNDING_4_SWEEP §6, §8).

Hand-computed bootstrap/permutation/d_z under the pinned seeds; Holm ordering on
a constructed p-set; §6.6 criteria on constructed outcomes; build_analysis on a
tiny fixture.
"""

from __future__ import annotations

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

    def test_holm_ordering_on_constructed_pset(self):
        h = analyze.holm_bonferroni({"a": 0.01, "b": 0.04, "c": 0.02, "d": 0.5})
        # sorted 0.01,0.02,0.04,0.5 → ×4,×3,×2,×1 = 0.04,0.06,0.08,0.5 (monotone)
        assert h["a"]["holm_adjusted"] == pytest.approx(0.04) and h["a"]["reject"] is True
        assert h["c"]["holm_adjusted"] == pytest.approx(0.06) and h["c"]["reject"] is False
        assert h["b"]["holm_adjusted"] == pytest.approx(0.08) and h["b"]["reject"] is False
        assert h["d"]["holm_adjusted"] == pytest.approx(0.5) and h["d"]["reject"] is False


class TestFalsificationMechanism:
    def test_criteria_evaluate_from_constructed_contrasts(self):
        # Construct contrast outcomes; the criteria evaluate per expect_material.
        contrasts = {
            "C2_vs_C1": {"material_positive": True, "mean_diff": 0.3, "ci_low": 0.1,
                         "ci_high": 0.5, "cohen_dz": 0.9},
            "C1_vs_C4": {"material_positive": False, "mean_diff": 0.0, "ci_low": -0.1,
                         "ci_high": 0.1, "cohen_dz": 0.05},
            "S0primeC2_vs_C2": {"material_positive": False, "mean_diff": -0.2, "ci_low": -0.4,
                                "ci_high": 0.0, "cohen_dz": -0.6},
        }
        table = analyze.evaluate_falsification(contrasts)
        met = dict(zip(table["criterion"], table["met"]))
        # decomposition_improves (expects material+ True) → met; the other two
        # (expect material+ False) → met because observed material+ is False.
        assert met["decomposition_improves_over_monolith"] is True
        assert met["right_sizing_not_monotone_finer"] is True
        assert met["structural_not_budget"] is True

    def test_criterion_flips_when_material(self):
        contrasts = {"C2_vs_C1": {"material_positive": False},
                     "C1_vs_C4": {"material_positive": True},
                     "S0primeC2_vs_C2": {"material_positive": True}}
        table = analyze.evaluate_falsification(contrasts)
        met = dict(zip(table["criterion"], table["met"]))
        assert met["decomposition_improves_over_monolith"] is False   # no material gain
        assert met["right_sizing_not_monotone_finer"] is False        # C4 materially beats C1
        assert met["structural_not_budget"] is False                  # budget artefact


def _tiny_scored():
    rows = []
    cases = [f"eval_{i:03d}" for i in range(1, 5)]
    acc = {"S0": [0, 0, 0, 1], "C1": [1, 1, 0, 1], "C2": [1, 1, 1, 1], "C4": [1, 0, 0, 1]}
    for cond, a in acc.items():
        for ci, case in enumerate(cases):
            for rep in range(5):
                rows.append(dict(phase=1, mode="none", condition=cond, case_id=case, repeat=rep,
                                 final_answer_accuracy=a[ci], total_tokens=100 + ci, latency_ms=10.0,
                                 trace_consistent=bool(a[ci]), substitution_success=None))
    for ci, case in enumerate(cases):
        for rep in range(5):
            rows.append(dict(phase=2, mode="none", condition=sc.S0PRIME_C2, case_id=case, repeat=rep,
                             final_answer_accuracy=[1, 1, 0, 1][ci], total_tokens=200, latency_ms=20.0,
                             trace_consistent=True, substitution_success=None))
    for ci, case in enumerate(cases):
        for rep in range(5):
            rows.append(dict(phase=3, mode="hallucination", condition="C1", case_id=case, repeat=rep,
                             final_answer_accuracy=0, total_tokens=150, latency_ms=15.0,
                             trace_consistent=False, substitution_success=(ci % 2 == 0)))
    return pd.DataFrame(rows)


class TestBuildAnalysis:
    def test_tables_and_hand_values(self):
        tables = analyze.build_analysis(_tiny_scored())
        assert set(tables) == {"case_level", "headline_contrasts", "injection_cells", "falsification"}

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

        # every headline test carries a Holm-adjusted p and reject flag.
        assert tables["headline_contrasts"]["holm_adjusted"].notna().all()

    def test_no_tail_percentile_latency(self):
        # §6.1: latency is medians only — no p95/p99 anywhere in the outputs.
        tables = analyze.build_analysis(_tiny_scored())
        for df in tables.values():
            cols = " ".join(df.columns).lower()
            assert "p95" not in cols and "p99" not in cols
