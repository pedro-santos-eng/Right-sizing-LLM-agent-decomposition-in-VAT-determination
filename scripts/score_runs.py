"""score_runs.py — OFFLINE scoring pass (grounding HARNESS_GROUNDING_4_SWEEP §4).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0; ORACLE_GROUNDING §5;
paper §6.1. Offline and re-runnable: it reads the raw records (the artifact of
record, §6.7), scores each against the oracle, and writes ONE tidy table
``results/scored.csv`` — a row per run implementing every §6.1 rule.

This is NOT an agent-context module and runs AFTER the raw records exist (L4 §3):
it imports ``scorer``/``labeler`` freely and may use pandas. Determinism: same
raw records + same price sheet ⇒ same CSV.

Every §6.1 rule implemented here:
  - final-answer accuracy (all fields, all lines);
  - the six §3.1 field-level indicators (jurisdiction / rate / exemption /
    reverse-charge / liable-party / vat-or-reason);
  - per-τ step accuracy with missing→recorded-missing, counted incorrect;
  - trace consistency;
  - earliest-failing-subtask (single label by fixed order) + same-layer ties
    in an auxiliary column;
  - terminal ⇒ final incorrect + trace-inconsistent, cost/latency counted in full;
  - prompt/completion tokens, tool calls, retries, DERIVED dollars (§5);
  - wall-clock latency;
  - substitution-success indicator (§6.4-literal) for every injected mode:
    the fraction reaching a validated trace within budget — identical semantics
    for hallucination, timeout, and outage (a case that still terminated ``ok``
    despite the fault);
  - record-substituted indicator (hallucination only): whether the injected
    record survived into the emitted trace's τ slot — the pre-9f7c298
    hallucination metric, retained as its own column now that
    substitution-success is §6.4-literal for all modes.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from scripts import sweep_common as sc
from src.harness.surface import LAYERS, SUBTASKS
from src.oracle import generator, labeler, scorer

_FIELD_INDICATORS = ("jurisdiction", "rate", "exemption", "reverse_charge",
                     "liable_party", "vat_or_reason")


@lru_cache(maxsize=1)
def _labels_by_case() -> dict:
    ds = generator.generate_dataset(seed=42)
    out = {}
    for case in list(ds.eval_cases) + list(ds.dev_cases):
        out[case.case_id] = labeler.label_case(case)
    return out


@lru_cache(maxsize=1)
def _plan() -> Optional[dict]:
    from src.harness.injection import DEFAULT_PLAN_PATH
    if not DEFAULT_PLAN_PATH.is_file():
        return None
    return json.loads(DEFAULT_PLAN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Field-level indicators (the six of §3.1). Values only, no citations.
# ---------------------------------------------------------------------------


def _emitted_line(emitted, line_id):
    for ln in (emitted.get("lines") or []):
        if isinstance(ln, dict) and ln.get("line_id") == line_id:
            return ln
    return None


def _field_indicators(emitted, labels) -> dict:
    label_lines = {ld.line_id: ld for ld in labels.lines}
    jur = emitted.get("jur") if isinstance(emitted, dict) else None
    jurisdiction_ok = (
        isinstance(jur, dict)
        and jur.get("decision", {}).get("jurisdiction") == labels.jur.decision["jurisdiction"]
    )

    def _all_lines(check) -> bool:
        for lid, ld in label_lines.items():
            el = _emitted_line(emitted, lid)
            if not isinstance(el, dict) or not check(el, ld):
                return False
        return True

    def _dec(el, key):
        rec = el.get(key)
        return rec.get("decision", {}) if isinstance(rec, dict) else {}

    rate_ok = _all_lines(lambda el, ld: _dec(el, "rat").get("rate_band") == ld.rat.decision["rate_band"]
                         and _dec(el, "rat").get("rate") == ld.rat.decision["rate"])
    exemption_ok = _all_lines(lambda el, ld: _dec(el, "exm").get("exempt") == ld.exm.decision["exempt"])
    reverse_charge_ok = _all_lines(lambda el, ld: _dec(el, "rch").get("reverse_charge") == ld.rch.decision["reverse_charge"])
    liable_party_ok = _all_lines(lambda el, ld: _dec(el, "rch").get("liable_party") == ld.rch.decision["liable_party"])
    vat_or_reason_ok = _all_lines(
        lambda el, ld: _dec(el, "rch").get("vat_amount") == ld.rch.decision["vat_amount"]
        and _dec(el, "rch").get("non_charging_reason") == ld.rch.decision["non_charging_reason"]
    )
    return {
        "jurisdiction_ok": bool(jurisdiction_ok),
        "rate_ok": bool(rate_ok),
        "exemption_ok": bool(exemption_ok),
        "reverse_charge_ok": bool(reverse_charge_ok),
        "liable_party_ok": bool(liable_party_ok),
        "vat_or_reason_ok": bool(vat_or_reason_ok),
    }


# ---------------------------------------------------------------------------
# Step status (present/correct/missing) + earliest-error with same-layer ties.
# ---------------------------------------------------------------------------


def _step_present(emitted, labels) -> dict:
    """True iff every labeled line has the emitted record for τ (JUR case-level)."""
    label_lines = {ld.line_id: ld for ld in labels.lines}
    present = {}
    jur = emitted.get("jur") if isinstance(emitted, dict) else None
    present["JUR"] = isinstance(jur, dict) and isinstance(jur.get("decision"), dict)
    for tau, key in (("CLS", "cls"), ("RAT", "rat"), ("EXM", "exm"), ("RCH", "rch")):
        ok = True
        for lid in label_lines:
            el = _emitted_line(emitted, lid)
            if not isinstance(el, dict) or not isinstance(el.get(key), dict):
                ok = False
                break
        present[tau] = ok
    return present


def _layer_of(subtask: str) -> frozenset:
    for layer in LAYERS:
        if subtask in layer:
            return layer
    return frozenset({subtask})


def score_record(raw: dict, price_sheet: dict) -> dict:
    sweep = raw["sweep"]
    case_id = sweep["case_id"]
    labels = _labels_by_case()[case_id]
    emitted = raw.get("emitted_trace")
    run_record = raw.get("run_record", {})
    terminal_status = raw.get("terminal_status", "no_trace")
    terminal = terminal_status != "ok"

    structural = isinstance(emitted, dict) and isinstance(emitted.get("lines"), list)
    if structural:
        s = scorer.score(emitted, labels)
        present = _step_present(emitted, labels)
        fields = _field_indicators(emitted, labels)
    else:
        s = scorer.score_terminal_failure(case_id)
        present = {t: False for t in SUBTASKS}
        fields = {f"{f}_ok": False for f in _FIELD_INDICATORS}

    step_ok = dict(s.step_accuracy)
    # §6.1: terminal ⇒ final-answer incorrect + trace-inconsistent (forced).
    final_answer_accuracy = (not terminal) and s.final_answer_accuracy
    trace_consistent = (not terminal) and s.trace_consistent

    # earliest failing subtask (single label, fixed order) + same-layer ties.
    earliest = s.earliest_error_subtask
    if earliest is None:
        ties = []
    else:
        layer = _layer_of(earliest)
        ties = [t for t in SUBTASKS if t in layer and not step_ok.get(t, False)]

    acc = run_record.get("accounting", {})
    tc = acc.get("token_counts", {})
    prompt_tokens = int(tc.get("input", 0))
    completion_tokens = int(tc.get("output", 0))
    workers = run_record.get("workers", [])
    retries = sum(int(w.get("retries", 0)) for w in workers)
    tool_calls = len(run_record.get("tool_invocations", []))
    injection = acc.get("injection", {})

    row = {
        "phase": sweep["phase"],
        "mode": sweep["mode"],
        "condition": sweep["condition"],
        "case_id": case_id,
        "repeat": sweep["repeat"],
        "terminal": terminal,
        "terminal_status": terminal_status,
        "final_answer_accuracy": bool(final_answer_accuracy),
        **fields,
        "trace_consistent": bool(trace_consistent),
        "earliest_failing_subtask": earliest or "",
        "earliest_error_ties": ",".join(ties),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "tool_calls": tool_calls,
        "retries": retries,
        "dollars": sc.dollars(prompt_tokens, completion_tokens, price_sheet),
        "latency_ms": raw.get("wall_clock", {}).get("duration_ms"),
        "injection_mode": injection.get("mode", sweep["mode"]),
        "injection_tau": injection.get("tau"),
        "injection_fired": bool(injection.get("fired", False)),
        "substitution_success": _substitution_success(
            sweep, injection, terminal_status
        ),
        "record_substituted": _record_substituted(
            sweep, injection, emitted, case_id
        ),
    }
    for tau in SUBTASKS:
        ok = bool(step_ok.get(tau, False))
        row[f"step_{tau}_ok"] = ok
        row[f"step_{tau}_status"] = "missing" if not present.get(tau, False) else ("correct" if ok else "incorrect")
    return row


_INJECTED_MODES = frozenset({"hallucination", "timeout", "outage"})


def _substitution_success(sweep, injection, terminal_status):
    """§6.4-literal substitution-success indicator, identical for every injected
    mode: "the fraction of injected cases reaching a validated trace within
    budget" — i.e. the case still terminated ``ok`` (a validated trace) despite
    the fault. Ratified for hallucination too (previously record-survival; that
    metric moves to ``record_substituted``).

    The metric is defined over INJECTED cases (§6.4: "fraction of injected
    cases..."), so the denominator is the fired cells: ``None`` (not applicable,
    dropped by ``analyze``) for un-injected modes AND for injected modes whose
    seam did not fire on this case; a bool only when the injection fired."""
    mode = sweep["mode"]
    if mode not in _INJECTED_MODES:
        return None
    if not injection.get("fired"):
        return None
    return terminal_status == "ok"


def _record_substituted(sweep, injection, emitted, case_id):
    """Record-substitution indicator (hallucination ONLY): did the injected
    record SURVIVE into the emitted trace's τ slot? The pre-9f7c298
    hallucination substitution metric, retained as a distinct column now that
    ``substitution_success`` is §6.4-literal for all modes. ``None`` (not
    applicable) for every other mode and for non-fired hallucination cells.

    Hallucination fires on every case (one τ per case, deterministic), so the
    not-fired→None branch never affects its column."""
    if sweep["mode"] != "hallucination":
        return None
    if not injection.get("fired"):
        return None
    return _hallucination_survived(injection, emitted, case_id)


def _hallucination_survived(injection, emitted, case_id):
    """Did the hallucinated record survive into the emitted trace's τ slot?"""
    plan = _plan()
    if not plan or not isinstance(emitted, dict):
        return False
    injected = plan.get("hallucinated_record_by_case", {}).get(case_id)
    tau = injection.get("tau")
    if injected is None or tau is None:
        return False
    if tau == "JUR":
        got = emitted.get("jur")
    else:
        # first line = lowest line_id (targeting is first-line, L3 §2)
        lines = emitted.get("lines") or []
        got = None
        if lines:
            first = min(lines, key=lambda ln: ln.get("line_id", ""))
            got = first.get(tau.lower())
    return bool(isinstance(got, dict) and got.get("decision") == injected.get("decision"))


# ---------------------------------------------------------------------------
# Directory walk → scored.csv.
# ---------------------------------------------------------------------------


def iter_raw_records(raw_dir: Path = sc.RAW_DIR):
    for path in sorted(raw_dir.rglob("r*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ok, _ = sc.validate_raw_record(obj)
        if ok:
            yield obj


def build_scored_frame(raw_dir: Path = sc.RAW_DIR, price_sheet: Optional[dict] = None) -> "pd.DataFrame":
    price_sheet = price_sheet or sc.load_price_sheet()
    rows = [score_record(obj, price_sheet) for obj in iter_raw_records(raw_dir)]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["phase", "mode", "condition", "case_id", "repeat"]).reset_index(drop=True)
    return df


def main() -> int:
    df = build_scored_frame()
    sc.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = sc.RESULTS_DIR / "scored.csv"
    df.to_csv(out, index=False)
    print(f"wrote {out} ({len(df)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
