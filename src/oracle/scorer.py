"""scorer.py — compare an emitted trace against the oracle labels (grounding §5).

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md.

Produces, for an emitted trace + the oracle CaseTrace labels:
  - final_answer_accuracy : emitted final determination matches the oracle label
    across all output fields, for ALL line items.
  - step_accuracy         : per-subtask {CLS, JUR, RAT, EXM, RCH} match flags.
  - trace_consistent      : all V checks pass (delegates to validator.py).
  - earliest_error_subtask: first subtask in fixed order [CLS, JUR, RAT, EXM, RCH]
    whose label is wrong; None if fully correct.

Pure comparison: never mutates the trace, never calls an LLM (grounding §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import validator
from .labeler import CaseTrace
from .rules import LineDetermination, StepRecord

# Fixed subtask order (grounding §2, §5). Earliest-error is reported in this order.
SUBTASK_ORDER: tuple[str, ...] = ("CLS", "JUR", "RAT", "EXM", "RCH")

# Decision fields that define a correct record for each subtask.
_DECISION_FIELDS = {
    "CLS": ("category",),
    "JUR": ("jurisdiction", "jur_path"),
    "RAT": ("rate_band", "rate"),
    "EXM": ("exempt",),
    "RCH": ("outcome", "reverse_charge", "liable_party", "vat_amount", "non_charging_reason"),
}

# Output fields that define the final answer (per grounding §5): values only, no
# citations. jurisdiction comes from JUR; the rest are per line.
_FINAL_LINE_FIELDS = (
    ("RAT", "rate_band"),
    ("RAT", "rate"),
    ("EXM", "exempt"),
    ("RCH", "outcome"),
    ("RCH", "reverse_charge"),
    ("RCH", "liable_party"),
    ("RCH", "vat_amount"),
    ("RCH", "non_charging_reason"),
)


@dataclass(frozen=True)
class Score:
    case_id: str
    final_answer_accuracy: bool
    step_accuracy: dict[str, bool]
    trace_consistent: bool
    earliest_error_subtask: Optional[str]


def score(emitted: Any, labels: CaseTrace) -> Score:
    """Score an emitted trace against oracle ``labels`` (a CaseTrace)."""
    trace_consistent = validator.validate_trace(emitted).ok

    emitted_lines = _emitted_lines_by_id(emitted)
    label_lines = {ld.line_id: ld for ld in labels.lines}

    step_accuracy = {
        "CLS": _per_line_step_ok("CLS", "cls", emitted_lines, label_lines),
        "JUR": _step_record_ok("JUR", _get(emitted, "jur"), labels.jur),
        "RAT": _per_line_step_ok("RAT", "rat", emitted_lines, label_lines),
        "EXM": _per_line_step_ok("EXM", "exm", emitted_lines, label_lines),
        "RCH": _per_line_step_ok("RCH", "rch", emitted_lines, label_lines),
    }

    final_answer_accuracy = _final_answer_ok(emitted, emitted_lines, labels, label_lines)

    earliest = next((st for st in SUBTASK_ORDER if not step_accuracy[st]), None)

    return Score(
        case_id=labels.case_id,
        final_answer_accuracy=final_answer_accuracy,
        step_accuracy=step_accuracy,
        trace_consistent=trace_consistent,
        earliest_error_subtask=earliest,
    )


def score_terminal_failure(case_id: str) -> Score:
    """Score a terminal failure (no trace / incomplete trace): all wrong."""
    return Score(
        case_id=case_id,
        final_answer_accuracy=False,
        step_accuracy={st: False for st in SUBTASK_ORDER},
        trace_consistent=False,
        earliest_error_subtask=SUBTASK_ORDER[0],
    )


# ---------------------------------------------------------------------------
# Comparison helpers. emitted records are plain dicts; labels are StepRecords.
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _emitted_lines_by_id(emitted: Any) -> dict[str, dict]:
    lines = _get(emitted, "lines") or []
    result: dict[str, dict] = {}
    for ln in lines:
        if isinstance(ln, dict):
            result[ln.get("line_id")] = ln
    return result


def _decision_matches(emitted_step: Any, label: StepRecord, fields: tuple[str, ...]) -> bool:
    if not isinstance(emitted_step, dict):
        return False
    decision = emitted_step.get("decision")
    if not isinstance(decision, dict):
        return False
    return all(decision.get(f) == label.decision.get(f) for f in fields)


def _step_record_ok(subtask: str, emitted_step: Any, label: StepRecord) -> bool:
    """A subtask record matches the label on its decision fields AND citation."""
    if not _decision_matches(emitted_step, label, _DECISION_FIELDS[subtask]):
        return False
    return emitted_step.get("rule_reference") == label.rule_reference


def _per_line_step_ok(
    subtask: str,
    key: str,
    emitted_lines: dict[str, dict],
    label_lines: dict[str, LineDetermination],
) -> bool:
    """True iff every labeled line's emitted record for this subtask matches."""
    for line_id, ld in label_lines.items():
        emitted_line = emitted_lines.get(line_id)
        if not isinstance(emitted_line, dict):
            return False
        label_rec: StepRecord = getattr(ld, key)
        if not _step_record_ok(subtask, emitted_line.get(key), label_rec):
            return False
    return True


def _final_answer_ok(
    emitted: Any,
    emitted_lines: dict[str, dict],
    labels: CaseTrace,
    label_lines: dict[str, LineDetermination],
) -> bool:
    """Final answer = jurisdiction + all enumerated per-line output fields match."""
    emitted_jur = _get(emitted, "jur")
    if not isinstance(emitted_jur, dict):
        return False
    if emitted_jur.get("decision", {}).get("jurisdiction") != labels.jur.decision["jurisdiction"]:
        return False

    for line_id, ld in label_lines.items():
        emitted_line = emitted_lines.get(line_id)
        if not isinstance(emitted_line, dict):
            return False
        for key, field in _FINAL_LINE_FIELDS:
            short = key.lower()
            emitted_rec = emitted_line.get(short)
            label_rec: StepRecord = getattr(ld, short)
            emitted_val = emitted_rec.get("decision", {}).get(field) if isinstance(emitted_rec, dict) else None
            if emitted_val != label_rec.decision.get(field):
                return False
    return True
