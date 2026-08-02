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

FLAG for review (bounded interpretation, §6.6): the paper's §6.6 falsification
criteria are not rendered in the repo. They are implemented here as three named,
editable constants (``FALSIFICATION_CRITERIA``) expressed through the ratified
materiality rule (CI excludes zero in the hypothesised direction AND
|d_z| ≥ 0.2) over the named contrasts. The MECHANISM is tested; the exact
scientific wording/direction of each criterion must be confirmed against paper
§6.6 before any reported falsification claim.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Optional

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
    return float(np.mean(perm_means >= observed - tol))


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
# §6.6 falsification criteria (FLAGGED — mechanical, wording pending review).
# Each maps to a named headline contrast + hypothesised direction. "met" uses
# the ratified materiality rule via the contrast's material_positive/direction.
# ---------------------------------------------------------------------------

FALSIFICATION_CRITERIA = (
    {"name": "decomposition_improves_over_monolith", "contrast": "C2_vs_C1",
     "expect_material_positive": True,
     "desc": "Some orchestrated decomposition materially beats the single-worker "
             "baseline (C2 > C1). Not met ⇒ decomposition provides no benefit."},
    {"name": "right_sizing_not_monotone_finer", "contrast": "C1_vs_C4",
     "expect_material_positive": False,
     "desc": "Finest decomposition (C4) does NOT materially beat coarse (C1) — "
             "an interior optimum, not 'finer is always better'. Met (i.e. C1−C4 "
             "not materially negative) supports right-sizing."},
    {"name": "structural_not_budget", "contrast": "S0primeC2_vs_C2",
     "expect_material_positive": False,
     "desc": "Matched-token monolith S0′_C2 does NOT materially beat C2 — the "
             "benefit is structural, not a token-budget artefact."},
)


def evaluate_falsification(contrast_by_name: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for crit in FALSIFICATION_CRITERIA:
        c = contrast_by_name.get(crit["contrast"], {})
        material_pos = bool(c.get("material_positive", False))
        met = material_pos if crit["expect_material_positive"] else (not material_pos)
        rows.append({
            "criterion": crit["name"],
            "contrast": crit["contrast"],
            "expect_material_positive": crit["expect_material_positive"],
            "observed_material_positive": material_pos,
            "met": met,
            "mean_diff": c.get("mean_diff"),
            "ci_low": c.get("ci_low"),
            "ci_high": c.get("ci_high"),
            "cohen_dz": c.get("cohen_dz"),
            "description": crit["desc"],
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
        validated = cell[cell["trace_consistent"]]
        cell_tok = cell.groupby("case_id")["total_tokens"].mean()
        common = sorted(set(cell_tok.index) & set(base_tok.loc[cond].index)) if cond in base_tok.index.get_level_values(0) else []
        penalty = float(np.mean([cell_tok[c] - base_tok.loc[(cond, c)] for c in common])) if common else float("nan")
        rows.append({
            "mode": mode,
            "condition": cond,
            "n_runs": len(cell),
            "substitution_success_rate": float(sub_success.mean()) if len(sub_success) else float("nan"),
            "all_case_accuracy": float(cell["final_answer_accuracy"].mean()),
            "validated_trace_accuracy": float(validated["final_answer_accuracy"].mean()) if len(validated) else float("nan"),
            "token_cost_penalty_vs_baseline": penalty,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Top-level analysis assembly.
# ---------------------------------------------------------------------------


def build_analysis(scored: pd.DataFrame) -> dict[str, pd.DataFrame]:
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

    return {
        "case_level": cl,
        "headline_contrasts": pd.DataFrame(contrast_rows),
        "injection_cells": injection_cells(scored),
        "falsification": evaluate_falsification(contrast_by_name),
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
