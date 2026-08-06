"""tune_s0prime.py — the §6.3 S0′ matched-token TUNING LOOP (Layer-4's missing
piece; grounding HARNESS_GROUNDING_4_SWEEP §6.3, HARNESS_GROUNDING_2_ORCHESTRATION §6).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0 §6.3 (the tuning loop
Layer 4 owns) over the S0Knobs measurement Layer 2 provides (s0.py §6).

Layer 2 delivered the S0′ knobs (three sanctioned slots) and per-case token
measurement; the LOOP that turns a matched budget into a committed knobs artifact
is Layer 4's, and it was never shipped — phases 2/4 ran with plain-S0 knobs
(DEVLOG 2026-08-06, "third defect"). This script is that loop.

What it does, per condition:
  1. Compute the matched TOKEN BUDGET from results/scored.csv (phase 1, mode
     ``none``): ``S0prime_C2`` → mean total_tokens of C2; ``S0prime_Cstar`` →
     resolve C* (argmax mean final-answer accuracy over C1–C4; ties broken by
     LOWEST mean token cost — ratified 2026-08-06, DEVLOG) then C*'s mean.
  2. Walk a DETERMINISTIC knob ladder (fixed text/rendering here; only the
     measurements are stochastic). Exemplar rungs are rendered ONLY from dev
     cases (dev_001..dev_008) with their oracle labels — this is an
     ANALYSIS-side script, so it may import ``labeler``/``validator`` (like the
     scorer/conftest), unlike the isolated execution path. No eval case is ever
     touched (asserted).
  3. Measure each rung by running S0′ with those knobs on the 8 dev cases (R=1)
     through the same harness execution call the child uses (``run_s0``), with a
     TEMP out-root — never results/raw/. The per-case ``total_tokens`` is the
     same accounting scored.csv reports (prompt+completion incl. repairs).
  4. Greedy bracket: start at an interior rung; step UP when below the ±10 %
     band, DOWN when above; stop at the first in-band config. If the ladder
     exhausts (or brackets the target with no in-band rung), keep the closest
     config and emit a loud WARN (the §6.3 deviation-contingency covers it).
  5. Write the tuned knobs (exact S0Knobs schema ``run_one._s0_knobs`` loads) +
     a tuning log to results/s0prime_knobs/.

Env: ANTHROPIC_API_KEY for the live measurement; SWEEP_SCRIPTED_CLIENT selects
the offline scripted client (the §8 seam) so the loop is testable without API.

Dependency policy: this is a Layer-4 ANALYSIS tool, not the execution child. It
reuses sweep_common (paths/conditions) and the harness execution call, and — as
an analysis-side module — legitimately imports the oracle labeler/validator to
render dev-derived exemplars. It never reads or writes results/raw/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from scripts import sweep_common as sc
from src.harness.model_client import make_model_client, make_scripted_client
from src.harness.s0 import S0Knobs, run_s0_blocking
from src.harness.surface import agent_case_view
from src.oracle import generator, labeler, validator

# The matched-budget band (paper §6.3): S0′ within ±10 % of the target budget.
BAND_FRAC = 0.10
# Interior starting rung for the greedy bracket (0-based into LADDER_LEVELS).
START_INDEX = 3
# The dev split the exemplars are rendered from and the measurement runs on.
DEV_CASES: tuple[str, ...] = tuple(f"dev_{i:03d}" for i in range(1, 9))

_KNOBS_DIR = sc.RESULTS_DIR / "s0prime_knobs"
_SCORED_CSV = sc.RESULTS_DIR / "scored.csv"

# The ratified C* resolution rule (DEVLOG 2026-08-06), disclosed in §7.
CSTAR_RULE = (
    "argmax mean final-answer accuracy over C1-C4; ties broken by LOWEST mean "
    "total token cost (Pareto-consistent). Fixed 2026-08-06 after observing the "
    "exact C2==C3 accuracy tie at 0.830."
)


# ---------------------------------------------------------------------------
# §6.3 budget targets — computed from scored.csv (phase 1, mode none).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CondStat:
    condition: str
    n: int
    mean_accuracy: float
    mean_tokens: float


def condition_stats(scored_csv: Path = _SCORED_CSV) -> dict[str, CondStat]:
    """Mean final-answer accuracy and mean total_tokens per condition over the
    §6.3 basis rows: phase 1, mode ``none`` (the clean matched-budget frame)."""
    acc: dict[str, list[float]] = {}
    tok: dict[str, list[float]] = {}
    with Path(scored_csv).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["phase"] != "1" or row["mode"] != "none":
                continue
            cond = row["condition"]
            acc.setdefault(cond, []).append(1.0 if row["final_answer_accuracy"] == "True" else 0.0)
            tok.setdefault(cond, []).append(float(row["total_tokens"]))
    return {
        cond: CondStat(cond, len(tok[cond]), sum(acc[cond]) / len(acc[cond]),
                       sum(tok[cond]) / len(tok[cond]))
        for cond in tok
    }


def resolve_cstar(stats: dict[str, CondStat]) -> str:
    """Ratified C* rule: argmax mean accuracy over C1-C4; ties → LOWEST mean
    tokens (Pareto-consistent). Deterministic; independent of dict order."""
    cands = ["C1", "C2", "C3", "C4"]
    return min(cands, key=lambda c: (-stats[c].mean_accuracy, stats[c].mean_tokens))


def budget_target(condition: str, stats: dict[str, CondStat]) -> tuple[float, Optional[str]]:
    """Return (target_tokens, resolved_cstar_or_None) for a S0′ control."""
    if condition == sc.S0PRIME_C2:
        return stats["C2"].mean_tokens, None
    if condition == sc.S0PRIME_CSTAR:
        cstar = resolve_cstar(stats)
        return stats[cstar].mean_tokens, cstar
    raise SystemExit(f"not a S0′ control condition: {condition!r}")


def write_cstar_artifact(cstar: str, stats: dict[str, CondStat], out_dir: Path = _KNOBS_DIR) -> Path:
    """Version the C* resolution (which condition, the rule, the basis) so the
    tie-break is auditable and never re-decided silently."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "Cstar.json"
    payload = {
        "cstar": cstar,
        "rule": CSTAR_RULE,
        "basis": {
            "source": "results/scored.csv",
            "frame": "phase 1, mode none",
            "candidates": {
                c: {"mean_accuracy": stats[c].mean_accuracy,
                    "mean_tokens": stats[c].mean_tokens, "n": stats[c].n}
                for c in ("C1", "C2", "C3", "C4")
            },
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The deterministic knob ladder. Fixed text/rendering (reproducible); exemplars
# rendered from dev cases + oracle labels. Rungs are ordered by increasing token
# weight so the greedy bracket walk is monotone.
# ---------------------------------------------------------------------------

_ROLE_SHORT = (
    "You are a senior VAT determination specialist. Work carefully and apply the "
    "authoritative exemption table exactly."
)
_ROLE_LONG = (
    "You are a senior cross-border VAT determination specialist with deep training "
    "in EU place-of-supply, reverse-charge, and exemption rules. For every line item "
    "you first fix jurisdiction, then classification, then the applicable rate, then "
    "any exemption, then the reverse-charge liable party, and finally the amount. You "
    "never guess a rate or an exemption: you read it off the authoritative exemption "
    "table (reference R) and the stated rules, and you keep the final aggregation "
    "consistent with the per-line determinations."
)
_SCRATCHPAD = (
    "Before emitting the final_trace, reason step by step in an intermediate "
    "scratchpad: for each line, state the jurisdiction, classification, rate, "
    "exemption, and reverse-charge conclusion and the rule that justifies it. The "
    "scratchpad is for your own working only; the final message must still end with "
    "exactly one fenced json final_trace block."
)


def _render_exemplar(case) -> str:
    """One worked exemplar: the label-free input view then the correct oracle
    trace, rendered deterministically. DEV cases only (asserted)."""
    assert case.case_id.startswith("dev_"), f"exemplar must be a dev case, got {case.case_id!r}"
    view = agent_case_view(case)
    emitted = validator.trace_to_emitted(labeler.label_case(case))
    return (
        f"Example (dev case {case.case_id}) — the input case view, then the "
        "correct complete final_trace:\n"
        "INPUT: " + json.dumps(view, sort_keys=True, ensure_ascii=True) + "\n"
        "CORRECT_TRACE: " + json.dumps(emitted, sort_keys=True, ensure_ascii=True)
    )


# Fixed ladder specification (name, role text, scratchpad on/off, #exemplars).
_LADDER_LEVELS: tuple[dict, ...] = (
    {"name": "L0_role_short", "role": _ROLE_SHORT, "scratch": False, "n_exemplars": 0},
    {"name": "L1_role_long", "role": _ROLE_LONG, "scratch": False, "n_exemplars": 0},
    {"name": "L2_role_long_scratch", "role": _ROLE_LONG, "scratch": True, "n_exemplars": 0},
    {"name": "L3_scratch_1ex", "role": _ROLE_LONG, "scratch": True, "n_exemplars": 1},
    {"name": "L4_scratch_2ex", "role": _ROLE_LONG, "scratch": True, "n_exemplars": 2},
    {"name": "L5_scratch_3ex", "role": _ROLE_LONG, "scratch": True, "n_exemplars": 3},
    {"name": "L6_scratch_4ex", "role": _ROLE_LONG, "scratch": True, "n_exemplars": 4},
)


@dataclass(frozen=True)
class Rung:
    name: str
    knobs: S0Knobs


def build_ladder(dev_cases: list) -> tuple[Rung, ...]:
    """Materialise the fixed ladder into concrete S0Knobs. Deterministic given
    the seed-42 dataset: the same dev cases render the same exemplars in order."""
    ordered = sorted(dev_cases, key=lambda c: c.case_id)
    exemplars = [_render_exemplar(c) for c in ordered]  # dev-only (asserted inside)
    rungs: list[Rung] = []
    for level in _LADDER_LEVELS:
        n = level["n_exemplars"]
        assert n <= len(exemplars), f"ladder needs {n} exemplars, have {len(exemplars)}"
        rungs.append(
            Rung(
                name=level["name"],
                knobs=S0Knobs(
                    extended_role=level["role"],
                    exemplars=tuple(exemplars[:n]),
                    scratchpad_instruction=_SCRATCHPAD if level["scratch"] else "",
                ),
            )
        )
    return tuple(rungs)


# ---------------------------------------------------------------------------
# Live measurement — run S0′ on the 8 dev cases (R=1) via the harness execution
# call, temp out-root only. Mean per-case total_tokens (same accounting scored).
# ---------------------------------------------------------------------------


def _make_client_factory() -> Callable[[], object]:
    """A fresh client per case. Scripted (offline seam) if SWEEP_SCRIPTED_CLIENT
    is set; otherwise the real client, which requires ANTHROPIC_API_KEY."""
    scripted = os.environ.get("SWEEP_SCRIPTED_CLIENT")
    if scripted:
        script = json.loads(Path(scripted).read_text(encoding="utf-8"))
        # Deep-copy per call: the scripted client consumes its queues in place.
        return lambda: make_scripted_client(json.loads(json.dumps(script)))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "FATAL: live S0′ tuning requires ANTHROPIC_API_KEY (or set "
            "SWEEP_SCRIPTED_CLIENT to the §8 scripted-client JSON for offline runs)."
        )
    return lambda: make_model_client()


def measure_config(
    knobs: S0Knobs, dev_cases: list, client_factory: Callable[[], object], out_root: Path
) -> tuple[float, list[dict]]:
    """Mean per-case total_tokens for ``knobs`` across the dev cases (R=1). Writes
    a small per-case provenance record under ``out_root`` (a TEMP dir — never
    results/raw/, asserted by the caller)."""
    out_root.mkdir(parents=True, exist_ok=True)
    per_case: list[dict] = []
    for case in dev_cases:
        assert case.case_id.startswith("dev_"), f"tuning must not touch {case.case_id!r}"
        result = run_s0_blocking(case, client_factory(), knobs=knobs)
        rec = {"case_id": case.case_id, "total_tokens": result.total_tokens,
               "status": result.status, "s0_knobs_plain": knobs.is_plain()}
        per_case.append(rec)
        (out_root / f"{case.case_id}.json").write_text(
            json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    mean = sum(r["total_tokens"] for r in per_case) / len(per_case)
    return mean, per_case


# ---------------------------------------------------------------------------
# The greedy bracket over the ladder. Measurement is injected so the walk logic
# is testable with scripted token counts (no API).
# ---------------------------------------------------------------------------


def bracket_search(
    n_rungs: int,
    target: float,
    measure: Callable[[int], float],
    *,
    band_frac: float = BAND_FRAC,
    start_index: int = START_INDEX,
    max_iters: int = 6,
) -> dict:
    """Greedy 1-D bracket: below band → step up a rung, above → step down; stop
    at the first in-band rung. Stops early if the walk would leave the ladder or
    revisit a rung (target bracketed between rungs with none in band). Returns
    the chosen rung + a per-iteration log; ``in_band`` False triggers the caller's
    loud WARN (§6.3 deviation contingency)."""
    lo, hi = target * (1.0 - band_frac), target * (1.0 + band_frac)
    visited: dict[int, float] = {}
    iterations: list[dict] = []
    i = min(max(start_index, 0), n_rungs - 1)
    for it in range(max_iters):
        if i not in visited:
            visited[i] = measure(i)
        tok = visited[i]
        verdict = "in_band" if lo <= tok <= hi else ("below" if tok < lo else "above")
        iterations.append({"iter": it, "rung_index": i, "mean_tokens": tok, "verdict": verdict})
        if verdict == "in_band":
            break
        nxt = i + 1 if verdict == "below" else i - 1
        if nxt < 0 or nxt >= n_rungs or nxt in visited:
            break  # ladder exhausted, or bracketed with no in-band rung
        i = nxt

    in_band_idxs = [k for k, v in visited.items() if lo <= v <= hi]
    if in_band_idxs:
        chosen = min(in_band_idxs, key=lambda k: abs(visited[k] - target))
        in_band = True
    else:
        chosen = min(visited, key=lambda k: abs(visited[k] - target))
        in_band = False
    return {
        "chosen_index": chosen,
        "chosen_tokens": visited[chosen],
        "in_band": in_band,
        "target": target,
        "band": [lo, hi],
        "iterations": iterations,
        "visited": {str(k): v for k, v in sorted(visited.items())},
    }


# ---------------------------------------------------------------------------
# Knob + tuning-log IO. The knobs file schema is EXACTLY what run_one._s0_knobs
# loads: {extended_role, exemplars(list), scratchpad_instruction}.
# ---------------------------------------------------------------------------


def knobs_to_dict(knobs: S0Knobs) -> dict:
    return {
        "extended_role": knobs.extended_role,
        "exemplars": list(knobs.exemplars),
        "scratchpad_instruction": knobs.scratchpad_instruction,
    }


def write_knobs(condition: str, knobs: S0Knobs, out_dir: Path = _KNOBS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{condition}.json"
    path.write_text(json.dumps(knobs_to_dict(knobs), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def _knob_summary(knobs: S0Knobs) -> dict:
    return {
        "extended_role_chars": len(knobs.extended_role),
        "scratchpad": bool(knobs.scratchpad_instruction),
        "n_exemplars": len(knobs.exemplars),
        "is_plain": knobs.is_plain(),
    }


def write_tuning_log(
    condition: str, target: float, cstar: Optional[str], ladder: tuple[Rung, ...],
    bracket: dict, per_rung: dict[int, list[dict]], out_dir: Path = _KNOBS_DIR
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"tuning_log_{condition}.json"
    iterations = []
    for step in bracket["iterations"]:
        idx = step["rung_index"]
        iterations.append({
            **step,
            "rung_name": ladder[idx].name,
            "knobs": _knob_summary(ladder[idx].knobs),
            "dev_usage": per_rung.get(idx, []),
        })
    payload = {
        "condition": condition,
        "cstar": cstar,
        "target_tokens": target,
        "band_frac": BAND_FRAC,
        "band": bracket["band"],
        "start_index": START_INDEX,
        "dev_cases": [c for c in DEV_CASES],
        "ladder": [{"index": n, "name": r.name, "knobs": _knob_summary(r.knobs)}
                   for n, r in enumerate(ladder)],
        "iterations": iterations,
        "chosen": {
            "index": bracket["chosen_index"],
            "name": ladder[bracket["chosen_index"]].name,
            "mean_tokens": bracket["chosen_tokens"],
            "in_band": bracket["in_band"],
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------


def _load_dev_cases() -> list:
    ds = generator.generate_dataset(seed=42)
    by_id = {c.case_id: c for c in ds.dev_cases}
    missing = [cid for cid in DEV_CASES if cid not in by_id]
    if missing:
        raise SystemExit(f"dev cases missing from dataset: {missing}")
    return [by_id[cid] for cid in DEV_CASES]


def tune(condition: str, max_iters: int) -> dict:
    stats = condition_stats()
    target, cstar = budget_target(condition, stats)
    if cstar is not None:
        art = write_cstar_artifact(cstar, stats)
        print(f"[C*] resolved {cstar} (rule: lowest mean tokens on the accuracy tie) "
              f"→ {art}", file=sys.stderr)
        print(f"computed target: {condition} → C* {cstar} B_C*={target:.3f}")
    else:
        print(f"computed target: {condition} → B_C2={target:.3f}")

    dev_cases = build_dev_and_assert()
    ladder = build_ladder(dev_cases)

    client_factory = _make_client_factory()
    tmp_root = Path(tempfile.mkdtemp(prefix=f"s0prime_tune_{condition}_"))
    assert "results" + os.sep + "raw" not in str(tmp_root) and "results/raw" not in str(tmp_root), \
        f"measurement out-root must be a temp dir, got {tmp_root}"
    per_rung: dict[int, list[dict]] = {}

    def measure(index: int) -> float:
        mean, per_case = measure_config(
            ladder[index].knobs, dev_cases, client_factory, tmp_root / ladder[index].name
        )
        per_rung[index] = per_case
        print(f"  rung {index} {ladder[index].name}: mean_total_tokens={mean:.1f}",
              file=sys.stderr)
        return mean

    bracket = bracket_search(len(ladder), target, measure,
                             start_index=START_INDEX, max_iters=max_iters)
    chosen = ladder[bracket["chosen_index"]]
    knobs_path = write_knobs(condition, chosen.knobs)
    log_path = write_tuning_log(condition, target, cstar, ladder, bracket, per_rung)

    if not bracket["in_band"]:
        print(f"WARN: {condition} did not land within ±{int(BAND_FRAC*100)}% of "
              f"target {target:.1f}. Kept closest rung {chosen.name} "
              f"(mean={bracket['chosen_tokens']:.1f}); §6.3 deviation contingency "
              f"covers reporting.", file=sys.stderr)

    print(f"tuned {condition}: rung {chosen.name} "
          f"mean_tokens={bracket['chosen_tokens']:.1f} target={target:.1f} "
          f"in_band={bracket['in_band']} → {knobs_path.name}, {log_path.name}")
    return {"condition": condition, "target": target, "cstar": cstar,
            "chosen": chosen.name, "in_band": bracket["in_band"],
            "knobs_path": str(knobs_path), "log_path": str(log_path)}


def build_dev_and_assert() -> list:
    """Load the dev split and assert the tuning path never touches an eval case."""
    dev_cases = _load_dev_cases()
    for case in dev_cases:
        assert case.case_id.startswith("dev_"), f"non-dev case in tuning path: {case.case_id!r}"
    return dev_cases


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="S0′ §6.3 matched-token tuning loop")
    ap.add_argument("--condition", required=True, choices=[sc.S0PRIME_C2, sc.S0PRIME_CSTAR])
    ap.add_argument("--max-iters", type=int, default=6)
    ns = ap.parse_args(argv)
    tune(ns.condition, ns.max_iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
