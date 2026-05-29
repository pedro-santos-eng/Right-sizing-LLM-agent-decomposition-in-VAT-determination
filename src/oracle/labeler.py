"""labeler.py — run the bounded rules over a whole case (grounding §2.2).

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md.

Composes the pure resolvers in ``rules.py`` into a complete oracle trace: one
case-level JUR record plus, per line item, the CLS/RAT/EXM/RCH determinations,
and a case-level final aggregation.

Pure: no I/O, no RNG, no clock, no LLM. The label is ground-truth-by-construction
(the generator assigned the true categories; the oracle just reads them).
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import (
    Case,
    LineDetermination,
    StepRecord,
    resolve_jur,
    resolve_line,
)


@dataclass(frozen=True)
class CaseTrace:
    """The full oracle label for one case."""
    case_id: str
    case: Case
    jur: StepRecord
    lines: tuple[LineDetermination, ...]
    final: dict


def label_case(case: Case) -> CaseTrace:
    """Produce the complete oracle trace (final + all intermediate labels)."""
    if not case.line_items:
        raise ValueError(f"case {case.case_id!r} has no line items")

    # JUR is per-case. In the bounded set the goods/service kind does not change
    # the country selection (grounding §2.2 / rules.resolve_jur), so one JUR
    # record computed from the first line's category applies to every line.
    jur = resolve_jur(case, case.line_items[0].true_category)

    lines = tuple(resolve_line(case, jur, li) for li in case.line_items)
    final = _aggregate(jur, lines)
    return CaseTrace(case_id=case.case_id, case=case, jur=jur, lines=lines, final=final)


def _aggregate(jur: StepRecord, lines: tuple[LineDetermination, ...]) -> dict:
    """Case-level final determination: the structured aggregation of all lines."""
    total = round(sum(ld.rch.decision["vat_amount"] for ld in lines), 2)
    return {
        "currency": "EUR",
        "jurisdiction": jur.decision["jurisdiction"],
        "total_vat_amount": total,
        "lines": [
            {
                "line_id": ld.line_id,
                "category": ld.cls.decision["category"],
                "rate_band": ld.rat.decision["rate_band"],
                "rate": ld.rat.decision["rate"],
                "exempt": ld.exm.decision["exempt"],
                "outcome": ld.rch.decision["outcome"],
                "reverse_charge": ld.rch.decision["reverse_charge"],
                "liable_party": ld.rch.decision["liable_party"],
                "vat_amount": ld.rch.decision["vat_amount"],
                "non_charging_reason": ld.rch.decision["non_charging_reason"],
            }
            for ld in lines
        ],
    }
