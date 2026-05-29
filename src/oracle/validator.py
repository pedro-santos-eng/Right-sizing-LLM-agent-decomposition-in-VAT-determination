"""validator.py — the validation-check set V (grounding §4).

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md.

Enforces, in order, over any emitted trace:
  1. Schema conformance (against src/schemas/final_trace.schema.json).
  2. Required-field presence (covered by the schema's `required`).
  3. Rule-citation presence (rule_reference drawn from the closed RULE_KEYS set;
     an exemption assertion must cite the exemption-table rule).
  4. Citation-decision consistency (the cited rule matches the decision).

Validation-passing is NECESSARY, NOT SUFFICIENT for correctness: a schema-valid,
citation-consistent trace can still be oracle-wrong. scorer.py — not this module
— decides oracle correctness (grounding §4, §5).

Pure: no I/O beyond reading the local schema file, no RNG, no clock, no LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .labeler import CaseTrace
from .rules import RATE_TABLE, RULE_KEYS, Category, Jurisdiction, Rule

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "final_trace.schema.json"

# Which rule_reference each rate band must carry, and vice versa.
_BAND_RULE = {"standard": Rule.RATE_STANDARD.value, "reduced": Rule.RATE_REDUCED.value}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    failed_checks: tuple[str, ...]


@lru_cache(maxsize=1)
def _schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# trace_to_emitted: render an oracle CaseTrace as the emitted-trace dict shape
# that an agent would produce (and that validate_trace / scorer consume).
# ---------------------------------------------------------------------------


def _step_to_dict(record) -> dict:
    return {
        "subtask": record.subtask,
        "decision": dict(record.decision),
        "support": dict(record.support),
        "rule_reference": record.rule_reference,
    }


def trace_to_emitted(trace: CaseTrace) -> dict:
    return {
        "case_id": trace.case_id,
        "jur": _step_to_dict(trace.jur),
        "lines": [
            {
                "line_id": ld.line_id,
                "cls": _step_to_dict(ld.cls),
                "rat": _step_to_dict(ld.rat),
                "exm": _step_to_dict(ld.exm),
                "rch": _step_to_dict(ld.rch),
            }
            for ld in trace.lines
        ],
        "final": {
            "currency": trace.final["currency"],
            "jurisdiction": trace.final["jurisdiction"],
            "total_vat_amount": trace.final["total_vat_amount"],
            "lines": [dict(line) for line in trace.final["lines"]],
        },
    }


# ---------------------------------------------------------------------------
# validate_trace: run V checks 1-4, accumulating failures.
# ---------------------------------------------------------------------------


def validate_trace(emitted: Any) -> ValidationResult:
    failed: list[str] = []

    # --- V1/V2: schema conformance + required-field presence ----------------
    schema_ok = True
    if not isinstance(emitted, dict):
        failed.append("schema: trace is not an object")
        schema_ok = False
    else:
        errors = sorted(
            _schema_validator().iter_errors(emitted), key=lambda e: list(e.path)
        )
        for err in errors:
            location = "/".join(str(p) for p in err.path) or "<root>"
            failed.append(f"schema: {location}: {err.message}")
        schema_ok = not errors

    # If the structure is broken, the semantic checks below cannot run safely.
    if not schema_ok:
        return ValidationResult(ok=False, failed_checks=tuple(failed))

    # --- V3/V4: citation presence + citation-decision consistency -----------
    jur = emitted["jur"]
    jurisdiction = jur["decision"]["jurisdiction"]
    customer_registered = jur.get("support", {}).get("customer_vat_registered")
    jur_path = jur["decision"]["jur_path"]

    _check_citation_present("JUR", jur, failed)

    for idx, line in enumerate(emitted["lines"]):
        cls = line["cls"]
        rat = line["rat"]
        exm = line["exm"]
        rch = line["rch"]

        for name, rec in (("CLS", cls), ("RAT", rat), ("EXM", exm), ("RCH", rch)):
            _check_citation_present(name, rec, failed, where=f"lines[{idx}]")

        _check_rat_consistency(idx, jurisdiction, rat, failed)
        _check_exm_consistency(idx, cls, exm, failed)
        _check_rch_consistency(idx, jur_path, customer_registered, exm, rch, failed)

    return ValidationResult(ok=not failed, failed_checks=tuple(failed))


# ---------------------------------------------------------------------------
# V3: citation presence (closed set + exemption must cite the exemption rule).
# ---------------------------------------------------------------------------


def _check_citation_present(name: str, record: dict, failed: list[str], where: str = "") -> None:
    prefix = f"{where}." if where else ""
    ref = record.get("rule_reference")
    if not ref or not isinstance(ref, str):
        failed.append(f"citation-presence: {prefix}{name} missing rule_reference")
        return
    if ref not in RULE_KEYS:
        failed.append(f"citation-presence: {prefix}{name} rule_reference {ref!r} not in closed set")
        return
    # An exemption assertion must cite the exemption-table rule.
    if name == "EXM" and record.get("decision", {}).get("exempt") is True:
        if ref != Rule.EXM_EXEMPT_SUPPLY.value:
            failed.append(
                f"citation-presence: {prefix}EXM exempt=true must cite {Rule.EXM_EXEMPT_SUPPLY.value}"
            )


# ---------------------------------------------------------------------------
# V4: citation-decision consistency.
# ---------------------------------------------------------------------------


def _check_rat_consistency(idx: int, jurisdiction: str, rat: dict, failed: list[str]) -> None:
    decision = rat.get("decision", {})
    band = decision.get("rate_band")
    rate = decision.get("rate")
    ref = rat.get("rule_reference")

    if band is None:
        # Exempt line: rate not applicable, must cite RATE.NA_EXEMPT.
        if rate is not None:
            failed.append(f"consistency: lines[{idx}].RAT band=null but rate is not null")
        if ref != Rule.RATE_NA_EXEMPT.value:
            failed.append(f"consistency: lines[{idx}].RAT band=null must cite {Rule.RATE_NA_EXEMPT.value}")
        return

    # Banded line: rate must match the bounded table for the stated jurisdiction.
    try:
        expected = RATE_TABLE[Jurisdiction(jurisdiction)][band]
    except (KeyError, ValueError):
        failed.append(f"consistency: lines[{idx}].RAT no table entry for ({jurisdiction}, {band})")
        return
    if rate != expected:
        failed.append(
            f"consistency: lines[{idx}].RAT rate {rate} != table {expected} for ({jurisdiction}, {band})"
        )
    if ref != _BAND_RULE.get(band):
        failed.append(f"consistency: lines[{idx}].RAT band {band!r} must cite {_BAND_RULE.get(band)}")


def _check_exm_consistency(idx: int, cls: dict, exm: dict, failed: list[str]) -> None:
    is_exempt = exm.get("decision", {}).get("exempt")
    category = cls.get("decision", {}).get("category")
    if is_exempt is True and category != Category.EXEMPT_SUPPLY.value:
        failed.append(
            f"consistency: lines[{idx}].EXM exempt=true but category is {category!r}, not EXEMPT_SUPPLY"
        )


def _check_rch_consistency(
    idx: int,
    jur_path: str,
    customer_registered: Any,
    exm: dict,
    rch: dict,
    failed: list[str],
) -> None:
    decision = rch.get("decision", {})
    reverse_charge = decision.get("reverse_charge")
    outcome = decision.get("outcome")
    ref = rch.get("rule_reference")

    # reverse_charge=true must be consistent with intra-community B2B + registered.
    if reverse_charge is True:
        if jur_path != "intra_community_b2b":
            failed.append(
                f"consistency: lines[{idx}].RCH reverse_charge=true but jur_path is {jur_path!r}"
            )
        if customer_registered is not True:
            failed.append(
                f"consistency: lines[{idx}].RCH reverse_charge=true but customer_vat_registered is not true"
            )
        if ref != Rule.RCH_B2B_INTRA.value:
            failed.append(f"consistency: lines[{idx}].RCH reverse_charge=true must cite {Rule.RCH_B2B_INTRA.value}")

    # An RCH exempt outcome must be consistent with EXM exempt=true.
    if outcome == "exempt":
        if exm.get("decision", {}).get("exempt") is not True:
            failed.append(f"consistency: lines[{idx}].RCH outcome=exempt but EXM exempt is not true")
        if ref != Rule.RCH_EXEMPT.value:
            failed.append(f"consistency: lines[{idx}].RCH outcome=exempt must cite {Rule.RCH_EXEMPT.value}")
