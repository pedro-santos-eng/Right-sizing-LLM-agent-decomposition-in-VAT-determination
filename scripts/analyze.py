"""analyze.py — OFFLINE analysis pass (grounding HARNESS_GROUNDING_4_SWEEP §6;
paper §6.4–§6.6).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0. Reads
``results/scored.csv`` and emits every reported number to ``results/analysis/``
as CSV tables. All seeds, resample counts, and the test family are constants at
the top of this file; changing any is a DEVLOG-recorded event (§6).

Pins (DECISION 4): bootstrap_seed 20260805, permutation_seed 20260806,
1,000/1,000 draws, percentile CIs, Cohen's d_z, Holm–Bonferroni over exactly the
four named headline tests, Mann–Whitney descriptive only, §6.4 cell metrics,
mechanical §6.6 criteria, NO p95/p99 latency.

REVIEWED 2026-08-02 (flag resolved): the three §6.6 falsification criteria are
rendered faithfully in ``evaluate_falsification``: (1) intermediate-optimum
granularity — triggered iff neither C2 nor C3 materially outperforms BOTH
endpoints C1 and C4 on accuracy, or the C1–C4 accuracy sequence is monotonic
in either direction (automatic); (2) orchestration benefit — triggered iff S0
weakly Pareto-dominates all of C1–C4 on the (token cost, accuracy) point
estimates; (3) prompt-budget confound — triggered iff the S0'_C2 vs C2
permutation test is not significant under Holm AND its bootstrap CI includes
zero. The three supplementary accuracy contrasts this requires (C2–C4, C3–C1,
C3–C4) are descriptive, outside the Holm family (§6.5). Sampled permutation p
uses the (1+hits)/(1+B) estimator (Phipson & Smyth 2010), so a reported p is
never zero; the exact-enumeration branch already contains the identity
permutation.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from scripts import sweep_common as sc

# ---------------------------------------------------------------------------
# Pinned analysis constants (§6 / DECISION 4). Changing any is a DEVLOG event.
# ---------------------------------------------------------------------------

BOOTSTRAP_SEED = 20260805
PERMUTATION_SEED = 20260806
N_BOOTSTRAP = 1000
N_PERMUTATION = 1000
CI_PCT = 95.0
D_Z_MATERIAL = 0.2
# Exact sign-flip enumeration when feasible (2^n ≤ this); else N_PERMUTATION
# random flips under PERMUTATION_SEED. For the 40-case sweep this is always
# random; small test fixtures enumerate exactly (hand-computable). (§6, flagged
# refinement of "1,000 permutations", which governs the real-data path.)
EXACT_PERMUTATION_MAX_N = 14

# The four headline tests (Holm family, §6). (A, B): paired diff is A − B.
HEADLINE_FAMILY: tuple[tuple[str, str, str], ...] = (
    ("S0_vs_C1", "S0", "C1"),
    ("C1_vs_C4", "C1", "C4"),
    ("C2_vs_C1", "C2", "C1"),
    ("S0primeC2_vs_C2", sc.S0PRIME_C2, "C2"),
)

# Supplementary accuracy contrasts required by §6.6(1); descriptive, OUTSIDE the
# Holm family (§6.5: "All other comparisons ... reported descriptively outside
# the corrected family").
SUPPLEMENTARY_CONTRASTS: tuple[tuple[str, str, str], ...] = (
    ("C2_vs_C4", "C2", "C4"),
    ("C3_vs_C1", "C3", "C1"),
    ("C3_vs_C4", "C3", "C4"),
)

GRANULARITY_ORDER: tuple[str, ...] = ("C1", "C2", "C3", "C4")

# Which phase supplies each condition's un-injected (mode none) case-level data.
_CONDITION_PHASE = {
    "S0": 1, "C1": 1, "C2": 1, "C3": 1, "C4": 1,
    sc.S0PRIME_C2: 2, sc.S0PRIME_CSTAR: 4,
}


# ---------------------------------------------------------------------------
# Statistical primitives (numpy only; no scipy). Directly unit-tested.
# ---------------------------------------------------------------------------


def bootstrap_ci(diffs: np.ndarray, seed: int = BOOTSTRAP_SEED,
                 n: int = N_BOOTSTRAP, pct: float = CI_PCT) -> tuple[float, float]:
    """Case-clustered paired bootstrap: resample cases with replacement, take the
    mean paired diff each time, return the percentile CI (§6)."""
    diffs = np.asarray(diffs, dtype=float)
    k = len(diffs)
    if k == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, k, k)
        means[i] = diffs[idx].mean()
    lo = (100.0 - pct) / 2.0
    return (float(np.percentile(means, lo)), float(np.percentile(means, 100.0 - lo)))


def permutation_p(diffs: np.ndarray, seed: int = PERMUTATION_SEED,
                  n: int = N_PERMUTATION) -> float:
    """Two-sided paired permutation (sign-flip) test on the mean paired diff.
    Exact enumeration when 2^k ≤ 2^EXACT_PERMUTATION_MAX_N, else n random flips."""
    diffs = np.asarray(diffs, dtype=float)
    k = len(diffs)
    if k == 0:
        return float("nan")
    observed = abs(diffs.mean())
    tol = 1e-12
    if k <= EXACT_PERMUTATION_MAX_N:
        hits = total = 0
        for signs in itertools.product((-1.0, 1.0), repeat=k):
            total += 1
            if abs(np.dot(signs, diffs) / k) >= observed - tol:
                hits += 1
        return hits / total
    rng = np.random.default_rng(seed)
    signs = rng.choice((-1.0, 1.0), size=(n, k))
    perm_means = np.abs((signs * diffs).mean(axis=1))
    # (1 + hits) / (1 + B): Phipson & Smyth (2010); never returns exactly 0.
    return float((1 + int(np.sum(perm_means >= observed - tol))) / (1 + n))


def cohen_dz(diffs: np.ndarray) -> float:
    """Cohen's d_z = mean(diffs) / sd(diffs, ddof=1) (§6)."""
    diffs = np.asarray(diffs, dtype=float)
    if len(diffs) < 2:
        return float("nan")
    sd = diffs.std(ddof=1)
    if sd == 0:
        return float("inf") if diffs.mean() != 0 else 0.0
    return float(diffs.mean() / sd)


def holm_bonferroni(pvalues: dict[str, float], alpha: float = 0.05) -> dict[str, dict]:
    """Holm–Bonferroni step-down over the named family (§6). Returns per-name
    {p, holm_adjusted, reject}."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, dict] = {}
    running = 0.0
    for i, (name, p) in enumerate(items):
        adj = min(1.0, (m - i) * p)
        running = max(running, adj)  # enforce monotone non-decreasing
        out[name] = {"p": p, "holm_adjusted": running, "reject": running <= alpha}
    return out


def mann_whitney_p(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided Mann–Whitney U with a normal approximation (descriptive only,
    §6). Tie-corrected variance."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return float("nan")
    allv = np.concatenate([a, b])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ranks for ties
    _, inv, counts = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    avg = sums / counts
    ranks = avg[inv]
    r1 = ranks[:n1].sum()
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u = min(u1, n1 * n2 - u1)
    mu = n1 * n2 / 2.0
    n = n1 + n2
    tie = np.sum(counts ** 3 - counts)
    sigma2 = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1))) if n > 1 else 0.0
    if sigma2 <= 0:
        return 1.0
    z = (u - mu + 0.5) / np.sqrt(sigma2)
    # two-sided normal tail
    return float(2.0 * 0.5 * (1.0 + _erf(-abs(z) / np.sqrt(2.0))))


def _erf(x: float) -> float:
    # Abramowitz-Stegun 7.1.26 (adequate for a descriptive column).
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-x * x)
    return float(np.sign(x) * y)


# ---------------------------------------------------------------------------
# Case-level aggregation (§6): R=5 → per (condition, case) summaries.
# ---------------------------------------------------------------------------


def case_level(scored: pd.DataFrame) -> pd.DataFrame:
    """Aggregate repeats to case level per (phase, mode, condition, case):
    mean accuracy, mean token cost, median latency."""
    g = scored.groupby(["phase", "mode", "condition", "case_id"], as_index=False)
    return g.agg(
        acc=("final_answer_accuracy", "mean"),
        tokens=("total_tokens", "mean"),
        latency_ms=("latency_ms", "median"),
    )


def _condition_case_series(cl: pd.DataFrame, condition: str, metric: str) -> pd.Series:
    phase = _CONDITION_PHASE.get(condition)
    sub = cl[(cl["condition"] == condition) & (cl["phase"] == phase) & (cl["mode"] == "none")]
    return sub.set_index("case_id")[metric]


def contrast(cl: pd.DataFrame, a: str, b: str, metric: str = "acc") -> dict:
    """Paired A−B over common cases (§6): mean diff, bootstrap CI, permutation p,
    d_z, Mann–Whitney (descriptive), materiality."""
    sa = _condition_case_series(cl, a, metric)
    sb = _condition_case_series(cl, b, metric)
    common = sorted(set(sa.index) & set(sb.index))
    diffs = np.array([sa[c] - sb[c] for c in common], dtype=float)
    if len(diffs) == 0:
        return {"a": a, "b": b, "metric": metric, "n": 0}
    ci_lo, ci_hi = bootstrap_ci(diffs)
    dz = cohen_dz(diffs)
    p = permutation_p(diffs)
    mw = mann_whitney_p(sa.loc[common].values, sb.loc[common].values)
    material = bool(ci_lo > 0 and dz >= D_Z_MATERIAL)  # CI excludes zero positively AND d_z ≥ 0.2
    return {
        "a": a, "b": b, "metric": metric, "n": len(diffs),
        "mean_diff": float(diffs.mean()),
        "ci_low": ci_lo, "ci_high": ci_hi,
        "cohen_dz": dz, "perm_p": p, "mannwhitney_p_descriptive": mw,
        "ci_excludes_zero": bool(ci_lo > 0 or ci_hi < 0),
        "material_positive": material,
    }


# ---------------------------------------------------------------------------
# §6.6 falsification criteria — faithful rendering (reviewed 2026-08-02).
# Each row reports whether the paper's falsifying condition is TRIGGERED, the
# verdict sentence §6.6 attaches to it, and the supporting evidence.
# ---------------------------------------------------------------------------


def _condition_point(cl: pd.DataFrame, condition: str, metric: str) -> float:
    """Point estimate: mean of the case-level summaries (§6.6 comparisons on
    main-sweep conditions are descriptive point estimates)."""
    s = _condition_case_series(cl, condition, metric)
    return float(s.mean()) if len(s) else float("nan")


def accuracy_sequence_monotonic(cl: pd.DataFrame, tol: float = 1e-12):
    """§6.6(1): "If the C1–C4 accuracy sequence is monotonic in either
    direction, this criterion is automatically unmet." Weak monotonicity over
    the point estimates; None if any condition is absent."""
    vals = [_condition_point(cl, c, "acc") for c in GRANULARITY_ORDER]
    if any(np.isnan(v) for v in vals):
        return None
    nondec = all(b >= a - tol for a, b in zip(vals, vals[1:]))
    noninc = all(b <= a + tol for a, b in zip(vals, vals[1:]))
    return bool(nondec or noninc)


def s0_weakly_pareto_dominates_all(cl: pd.DataFrame, tol: float = 1e-12):
    """§6.6(2): S0 weakly Pareto-dominates a configuration iff it is no worse
    on BOTH axes — accuracy no lower AND token cost no higher — on the
    main-sweep point estimates. Triggered iff this holds against every one of
    C1–C4. None if any condition is absent."""
    acc0 = _condition_point(cl, "S0", "acc")
    tok0 = _condition_point(cl, "S0", "tokens")
    if np.isnan(acc0) or np.isnan(tok0):
        return None
    any_missing = False
    for ci in GRANULARITY_ORDER:
        a = _condition_point(cl, ci, "acc")
        t = _condition_point(cl, ci, "tokens")
        if np.isnan(a) or np.isnan(t):
            any_missing = True          # undeterminable UNLESS a definitive
            continue                    # failure appears elsewhere
        if not (acc0 >= a - tol and tok0 <= t + tol):
            return False                # definitive, order-independent
    return None if any_missing else True


def _material(contrast_by_name: dict[str, dict], name: str) -> bool:
    return bool(contrast_by_name.get(name, {}).get("material_positive", False))


def evaluate_falsification(cl: pd.DataFrame,
                           contrast_by_name: dict[str, dict]) -> pd.DataFrame:
    """The three pre-stated §6.6 criteria, evaluated mechanically. "Materially
    outperforms" is the ratified rule carried by ``contrast``: bootstrap CI
    excludes zero in the positive direction AND d_z ≥ 0.2."""
    rows = []

    # (1) Intermediate-optimum granularity (RQ1).
    m21 = _material(contrast_by_name, "C2_vs_C1")
    m24 = _material(contrast_by_name, "C2_vs_C4")
    m31 = _material(contrast_by_name, "C3_vs_C1")
    m34 = _material(contrast_by_name, "C3_vs_C4")
    intermediate_beats_both = (m21 and m24) or (m31 and m34)
    monotonic = accuracy_sequence_monotonic(cl)
    have_all = all(contrast_by_name.get(k, {}).get("n", 0) > 0
                   for k in ("C2_vs_C1", "C2_vs_C4", "C3_vs_C1", "C3_vs_C4"))
    trig1 = (None if (not have_all or monotonic is None)
             else bool((not intermediate_beats_both) or monotonic))
    rows.append({
        "criterion": "intermediate_optimum_RQ1",
        "triggered": trig1,
        "verdict": ("intermediate-optimum hypothesis unsupported for this workload"
                    if trig1 else
                    "an intermediate configuration materially outperforms both endpoints"
                    if trig1 is False else "undeterminable (missing conditions)"),
        "evidence": (f"C2>C1={m21}, C2>C4={m24}, C3>C1={m31}, C3>C4={m34}, "
                     f"monotonic_C1..C4={monotonic}"),
        "paper_rule": ("§6.6(1): triggered iff neither C2 nor C3 materially "
                       "outperforms both C1 and C4 on final-answer accuracy, or "
                       "the C1–C4 accuracy sequence is monotonic (automatic)."),
    })

    # (2) Orchestration benefit (RQ2).
    dom = s0_weakly_pareto_dominates_all(cl)
    pts = {c: (round(_condition_point(cl, c, "acc"), 6),
               round(_condition_point(cl, c, "tokens"), 3))
           for c in ("S0",) + GRANULARITY_ORDER}
    rows.append({
        "criterion": "orchestration_benefit_RQ2",
        "triggered": dom,
        "verdict": ("orchestrated multi-worker hypothesis unsupported for this workload"
                    if dom else
                    "S0 does not weakly Pareto-dominate all of C1–C4"
                    if dom is False else "undeterminable (missing conditions)"),
        "evidence": f"(acc, tokens) points: {pts}",
        "paper_rule": ("§6.6(2): triggered iff S0 weakly Pareto-dominates all of "
                       "C1–C4 on the token-cost/final-answer-accuracy plane "
                       "(main-sweep point estimates)."),
    })

    # (3) Prompt-budget confound (RQ3).
    c = contrast_by_name.get("S0primeC2_vs_C2", {})
    if c.get("n", 0):
        ci_spans_zero = bool(c["ci_low"] <= 0.0 <= c["ci_high"])
        not_significant = not bool(c.get("holm_reject", False))
        trig3 = bool(not_significant and ci_spans_zero)
        ev = (f"holm_reject={c.get('holm_reject')}, "
              f"ci=[{c['ci_low']:.6g}, {c['ci_high']:.6g}], "
              f"mean_diff={c.get('mean_diff'):.6g}")
    else:
        trig3, ev = None, "S0primeC2_vs_C2 contrast unavailable (n=0)"
    rows.append({
        "criterion": "prompt_budget_confound_RQ3",
        "triggered": trig3,
        "verdict": ("any C2 main-sweep advantage is reported as consistent with a "
                    "prompt-budget explanation" if trig3 else
                    "the matched-token comparison distinguishes C2 from S0'_C2"
                    if trig3 is False else "undeterminable (phase 2 absent)"),
        "evidence": ev,
        "paper_rule": ("§6.6(3): triggered iff the S0'_C2 vs C2 accuracy "
                       "difference is not significant under the paired "
                       "permutation test with Holm–Bonferroni correction AND its "
                       "paired bootstrap CI includes zero."),
    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# §6.4 cell metrics per (injection mode, condition), from Phase 3, with the
# paired un-injected baseline from Phase 1.
# ---------------------------------------------------------------------------


def injection_cells(scored: pd.DataFrame) -> pd.DataFrame:
    inj = scored[(scored["phase"] == 3)]
    base = scored[(scored["phase"] == 1) & (scored["mode"] == "none")]
    base_tok = base.groupby(["condition", "case_id"])["total_tokens"].mean()
    rows = []
    for (mode, cond), cell in inj.groupby(["mode", "condition"]):
        sub_success = cell["substitution_success"].dropna()
        rec_sub = (cell["record_substituted"].dropna()
                   if "record_substituted" in cell.columns else pd.Series(dtype=float))
        validated = cell[cell["trace_consistent"]]
        cell_tok = cell.groupby("case_id")["total_tokens"].mean()
        common = sorted(set(cell_tok.index) & set(base_tok.loc[cond].index)) if cond in base_tok.index.get_level_values(0) else []
        penalty = float(np.mean([cell_tok[c] - base_tok.loc[(cond, c)] for c in common])) if common else float("nan")
        rows.append({
            "mode": mode,
            "condition": cond,
            "n_runs": len(cell),
            "substitution_success_rate": float(sub_success.mean()) if len(sub_success) else float("nan"),
            "record_substituted_rate": float(rec_sub.mean()) if len(rec_sub) else float("nan"),
            "all_case_accuracy": float(cell["final_answer_accuracy"].mean()),
            "validated_trace_accuracy": float(validated["final_answer_accuracy"].mean()) if len(validated) else float("nan"),
            "token_cost_penalty_vs_baseline": penalty,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# §8 injection-delta table: paired per-case accuracy delta of each injection
# cell vs the phase-1 mode-none baseline for that condition, with a
# case-clustered percentile bootstrap CI (its own pinned seed, DECISION-noted).
# ---------------------------------------------------------------------------

INJECTION_DELTA_SEED = 42
N_DELTA_BOOTSTRAP = 1000


def delta_bootstrap_ci(d: np.ndarray, seed: int = INJECTION_DELTA_SEED,
                       n: int = N_DELTA_BOOTSTRAP) -> tuple[float, float]:
    """Case-clustered percentile bootstrap for the injection-delta table.
    EXACT reference algorithm: ``numpy.default_rng(seed)`` per cell; ``n``
    iterations each drawing ``rng.choice(d, size=len(d), replace=True).mean()``;
    CI = ``numpy.percentile`` at [2.5, 97.5] of the ``n`` resample means."""
    d = np.asarray(d, dtype=float)
    if len(d) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        means[i] = rng.choice(d, size=len(d), replace=True).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def injection_deltas(scored: pd.DataFrame) -> pd.DataFrame:
    """§8 paired per-case accuracy deltas. For each (injection mode, condition)
    cell: the case-level mean final-answer accuracy (over the 5 repeats) under
    injection MINUS the same-condition phase-1 mode-none baseline case-level
    mean, paired over common cases; reporting the mean delta, a 95%
    case-clustered percentile bootstrap CI (seed 42), and n cases."""
    inj = scored[scored["phase"] == 3]
    base = scored[(scored["phase"] == 1) & (scored["mode"] == "none")]
    base_acc = base.groupby(["condition", "case_id"])["final_answer_accuracy"].mean()
    have_cond = set(base_acc.index.get_level_values(0))
    rows = []
    for (mode, cond), cell in inj.groupby(["mode", "condition"]):
        if cond not in have_cond:
            continue
        cell_acc = cell.groupby("case_id")["final_answer_accuracy"].mean()
        base_cond = base_acc.loc[cond]
        common = sorted(set(cell_acc.index) & set(base_cond.index))
        d = np.array([cell_acc[c] - base_cond[c] for c in common], dtype=float)
        if len(d) == 0:
            continue
        ci_lo, ci_hi = delta_bootstrap_ci(d)
        rows.append({
            "mode": mode,
            "condition": cond,
            "n_cases": len(d),
            "mean_delta": float(d.mean()),
            "ci_low": ci_lo,
            "ci_high": ci_hi,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# §7 paper tables — main_table, error_types, s0_family. All CIs use the SAME
# case-clustered percentile bootstrap engine as the §8 injection-delta table
# (``numpy.default_rng(42)`` per series, 1,000 resamples, percentile
# [2.5, 97.5]); see ``delta_bootstrap_ci``. ``_bootstrap_stat_ci`` generalises
# it to an arbitrary summary statistic so the latency column can bootstrap the
# median while accuracy/tokens bootstrap the mean, all off the identical stream.
# ---------------------------------------------------------------------------

# The §7 main-sweep conditions, in report order (all phase 1, mode none).
MAIN_TABLE_CONDITIONS: tuple[str, ...] = ("S0",) + GRANULARITY_ORDER

# earliest_failing_subtask categories, in pipeline order (matches the step_*
# columns CLS→JUR→RAT→EXM→RCH). Fixed so error_types is zeros-filled and stable.
ERROR_SUBTASK_ORDER: tuple[str, ...] = ("CLS", "JUR", "RAT", "EXM", "RCH")

# S0-family matched-token specification (§6.7). Each tuned arm targets the
# phase-1 mean total-token cost of its reference condition; target_tokens are the
# phase-1 main-sweep points (C2 = 14590.085, C* resolved to C3 = 13590.655). The
# ±10% band is the preregistered matched-token tolerance. S0 is the untuned
# reference (no target / band).
S0_FAMILY_SPEC: tuple[tuple[str, int, str, Optional[float]], ...] = (
    ("S0", 1, "S0", None),
    ("S0prime_C2", 2, sc.S0PRIME_C2, 14590.085),
    ("S0prime_Cstar", 4, sc.S0PRIME_CSTAR, 13590.655),
)
S0_FAMILY_BAND_PCT = 10.0


def _bootstrap_stat_ci(values: np.ndarray, stat: Callable[[np.ndarray], float],
                       seed: int = INJECTION_DELTA_SEED,
                       n: int = N_DELTA_BOOTSTRAP) -> tuple[float, float]:
    """Case-clustered percentile bootstrap of a summary ``stat`` over a 1-D
    array, mirroring ``delta_bootstrap_ci`` exactly: one ``default_rng(seed)``
    per call; ``n`` resamples, each ``rng.choice(values, size=len(values),
    replace=True)``; CI = ``numpy.percentile`` at [2.5, 97.5] of the ``n``
    statistics. With ``stat=numpy.mean`` this is byte-identical to
    ``delta_bootstrap_ci``; ``numpy.median`` gives the latency-median CI."""
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    stats = np.empty(n)
    for i in range(n):
        stats[i] = stat(rng.choice(values, size=len(values), replace=True))
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return (float(lo), float(hi))


def _cond_case_frame(cl: pd.DataFrame, condition: str, phase: int) -> pd.DataFrame:
    return cl[(cl["condition"] == condition) & (cl["phase"] == phase)
              & (cl["mode"] == "none")]


def main_table(scored: pd.DataFrame) -> pd.DataFrame:
    """§7 headline table, one row per main-sweep condition (S0, C1–C4; phase 1,
    mode none). Accuracy and total_tokens report the case-level mean with a 95%
    case-clustered bootstrap CI; latency reports the median of the per-case
    median latencies in SECONDS with a bootstrap-of-the-median CI. trace_consistent
    and terminal_failure are run-level rates."""
    cl = case_level(scored)
    rows = []
    for cond in MAIN_TABLE_CONDITIONS:
        phase = _CONDITION_PHASE[cond]
        cc = _cond_case_frame(cl, cond, phase)
        acc = cc["acc"].to_numpy(dtype=float)
        tok = cc["tokens"].to_numpy(dtype=float)
        lat_s = cc["latency_ms"].to_numpy(dtype=float) / 1000.0
        runs = scored[(scored["condition"] == cond) & (scored["phase"] == phase)
                      & (scored["mode"] == "none")]
        acc_lo, acc_hi = _bootstrap_stat_ci(acc, np.mean)
        tok_lo, tok_hi = _bootstrap_stat_ci(tok, np.mean)
        lat_lo, lat_hi = _bootstrap_stat_ci(lat_s, np.median)
        rows.append({
            "condition": cond,
            "n_cases": int(len(acc)),
            "n_runs": int(len(runs)),
            "acc_mean": float(acc.mean()) if len(acc) else float("nan"),
            "acc_ci_low": acc_lo, "acc_ci_high": acc_hi,
            "total_tokens_mean": float(tok.mean()) if len(tok) else float("nan"),
            "total_tokens_ci_low": tok_lo, "total_tokens_ci_high": tok_hi,
            "latency_s_median": float(np.median(lat_s)) if len(lat_s) else float("nan"),
            "latency_s_ci_low": lat_lo, "latency_s_ci_high": lat_hi,
            "trace_consistent_mean": (float(runs["trace_consistent"].mean())
                                      if len(runs) else float("nan")),
            "terminal_failure_rate": (float((runs["terminal_status"] != "ok").mean())
                                      if len(runs) else float("nan")),
        })
    return pd.DataFrame(rows)


def error_types(scored: pd.DataFrame) -> pd.DataFrame:
    """§7 earliest-failure breakdown: counts of ``earliest_failing_subtask`` over
    the failed runs (final_answer_accuracy == 0) of each main-sweep condition
    (phase 1, mode none). Every subtask category is a column (zeros filled) and
    each row carries its own total."""
    base = scored[(scored["phase"] == 1) & (scored["mode"] == "none")
                  & (scored["condition"].isin(MAIN_TABLE_CONDITIONS))]
    fails = base[base["final_answer_accuracy"] == 0]
    rows = []
    for cond in MAIN_TABLE_CONDITIONS:
        col = fails[fails["condition"] == cond]["earliest_failing_subtask"]
        counts = {s: int((col == s).sum()) for s in ERROR_SUBTASK_ORDER}
        rows.append({"condition": cond, **counts,
                     "total": int(sum(counts.values()))})
    return pd.DataFrame(rows, columns=["condition", *ERROR_SUBTASK_ORDER, "total"])


def _one_prompt_hash(condition: str, phase: int, raw_dir: Path) -> str:
    """The tuned/system prompt SHA-256 for a condition, read from a single raw
    record's ``run_record.accounting.prompt_hashes`` (§6.7 disclosure). Returns
    the sole role hash (preferring the ``S0`` role key); "" if unavailable."""
    base = Path(raw_dir) / f"phase{phase}" / "none" / condition
    if not base.exists():
        return ""
    for rec in sorted(base.glob("*/r*.json")):
        try:
            d = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ph = (d.get("run_record", {}).get("accounting", {}) or {}).get("prompt_hashes")
        if isinstance(ph, dict) and ph:
            return str(ph.get("S0") or next(iter(ph.values())))
    return ""


def s0_family(scored: pd.DataFrame, raw_dir: Optional[Path] = None) -> pd.DataFrame:
    """§7 S0-family matched-token table: S0 (phase 1), S0prime_C2 (phase 2), and
    S0prime_Cstar (phase 4). Reports accuracy mean + 95% bootstrap CI, the mean
    total-token cost against its matched-token target, the signed deviation %,
    the in-band verdict (|deviation| ≤ 10%), and the disclosed prompt hash. S0 is
    the untuned reference (target / deviation / in_band = n/a)."""
    raw_dir = raw_dir if raw_dir is not None else (sc.RESULTS_DIR / "raw")
    cl = case_level(scored)
    rows = []
    for label, phase, cond, target in S0_FAMILY_SPEC:
        cc = _cond_case_frame(cl, cond, phase)
        acc = cc["acc"].to_numpy(dtype=float)
        tok = cc["tokens"].to_numpy(dtype=float)
        acc_lo, acc_hi = _bootstrap_stat_ci(acc, np.mean)
        mean_tok = float(tok.mean()) if len(tok) else float("nan")
        if target is None:
            dev, in_band = float("nan"), None
        else:
            dev = (mean_tok - target) / target * 100.0
            in_band = bool(abs(dev) <= S0_FAMILY_BAND_PCT)
        rows.append({
            "arm": label,
            "phase": phase,
            "condition": cond,
            "n_cases": int(len(acc)),
            "acc_mean": float(acc.mean()) if len(acc) else float("nan"),
            "acc_ci_low": acc_lo, "acc_ci_high": acc_hi,
            "mean_tokens": mean_tok,
            "target_tokens": (float(target) if target is not None else float("nan")),
            "deviation_pct": dev,
            "in_band": in_band,
            "prompt_hash": _one_prompt_hash(cond, phase, raw_dir),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Top-level analysis assembly.
# ---------------------------------------------------------------------------


def build_analysis(scored: pd.DataFrame,
                   raw_dir: Optional[Path] = None) -> dict[str, pd.DataFrame]:
    cl = case_level(scored)

    contrast_rows = []
    contrast_by_name: dict[str, dict] = {}
    pvals: dict[str, float] = {}
    for name, a, b in HEADLINE_FAMILY:
        c = contrast(cl, a, b, metric="acc")
        c["name"] = name
        contrast_by_name[name] = c
        contrast_rows.append(c)
        if c.get("n", 0) > 0:
            pvals[name] = c["perm_p"]

    holm = holm_bonferroni(pvals) if pvals else {}
    for row in contrast_rows:
        h = holm.get(row.get("name"), {})
        row["holm_adjusted"] = h.get("holm_adjusted")
        row["holm_reject"] = h.get("reject")

    # §6.6(1) supplementary accuracy contrasts — descriptive, no Holm columns.
    supplementary_rows = []
    for name, a, b in SUPPLEMENTARY_CONTRASTS:
        c = contrast(cl, a, b, metric="acc")
        c["name"] = name
        contrast_by_name[name] = c
        supplementary_rows.append(c)

    return {
        "case_level": cl,
        "main_table": main_table(scored),
        "error_types": error_types(scored),
        "s0_family": s0_family(scored, raw_dir),
        "headline_contrasts": pd.DataFrame(contrast_rows),
        "supplementary_contrasts": pd.DataFrame(supplementary_rows),
        "injection_cells": injection_cells(scored),
        "injection_deltas": injection_deltas(scored),
        "falsification": evaluate_falsification(cl, contrast_by_name),
    }


def main(scored_path: Optional[Path] = None) -> int:
    scored_path = scored_path or (sc.RESULTS_DIR / "scored.csv")
    scored = pd.read_csv(scored_path)
    tables = build_analysis(scored)
    out_dir = sc.RESULTS_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)
    print(f"wrote {len(tables)} tables to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
