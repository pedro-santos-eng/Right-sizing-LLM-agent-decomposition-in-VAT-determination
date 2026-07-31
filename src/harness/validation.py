"""validation.py — incremental validation adapter (grounding §7.1).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_1_SURFACE.md (v1.1).

``validator.validate_trace`` (the frozen oracle) operates on a COMPLETE trace,
which is correct for S0's whole-trace repair unit. C1-C4 retry at the SUBTASK
level, so the orchestrator needs per-record verdicts as records arrive. This
module provides ``validate_record`` implementing the SAME four V check families
scoped to one record, plus:

  - ``verdicts_in_dependency_order`` — drive the per-record checks over an
    assembled trace in dependency order (the incremental adapter's reference
    driver), returning (all_accept, verdicts).
  - ``assembly_gate`` — the AUTHORITATIVE gate, unchanged in every condition: a
    thin wrapper around ``validator.validate_trace``. Incremental verdicts route
    retries; they NEVER replace this final full-trace check (grounding §7.1).

Equivalence invariant (grounding §7.1, amended): for a fully assembled trace,
evaluating validate_record over SUBTASKS in dependency order (each record given
the previously accepted ones) yields all-accept IFF validate_trace passes.

This module is NOT an agent-context module (it is condition-invariant validation
machinery, §1.2), so it may import ``validator``. It never imports
``labeler``/``scorer`` directly, and constructs no agent context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from src.oracle import validator
from src.oracle.rules import RATE_TABLE, RULE_KEYS, Category, Jurisdiction, Rule

# Reuse the oracle's authoritative outputs so there is one gate, not two.
from src.oracle.validator import ValidationResult

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "final_trace.schema.json"

# Which rule_reference each rate band must carry (mirrors validator._BAND_RULE).
_BAND_RULE = {"standard": Rule.RATE_STANDARD.value, "reduced": Rule.RATE_REDUCED.value}

_SUBTASK_DEF = {"CLS": "cls", "JUR": "jur", "RAT": "rat", "EXM": "exm", "RCH": "rch"}


@dataclass(frozen=True)
class RecordVerdict:
    """The per-record incremental verdict. ``accepted`` is True iff no check
    FAILED; ``deferred_checks`` are checks whose upstream inputs were not yet
    available (deferred, NOT passed — they run when the dependent record arrives
    or at final assembly, grounding §7.1)."""

    subtask: str
    accepted: bool
    failed_checks: tuple[str, ...]
    deferred_checks: tuple[str, ...]


@lru_cache(maxsize=1)
def _defs() -> dict:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["$defs"]


@lru_cache(maxsize=None)
def _record_validator(subtask: str) -> Draft202012Validator:
    """A validator for one subtask's $defs entry. The cls/jur/rat/exm/rch defs
    are self-contained (no internal $ref), so the sub-schema validates alone."""
    return Draft202012Validator(_defs()[_SUBTASK_DEF[subtask]])


# ---------------------------------------------------------------------------
# validate_record — the four V check families, scoped to one record.
# ---------------------------------------------------------------------------


def validate_record(subtask: str, record: dict, accepted: dict[str, dict]) -> RecordVerdict:
    """Validate one emitted ``record`` for ``subtask`` given ``accepted`` upstream
    records (keyed by subtask). Mirrors validator.validate_trace's checks:
      V1/V2 schema + required-field presence (against the subtask's $defs entry),
      V3 citation presence (closed key set; EXM exempt must cite EXM_EXEMPT_SUPPLY),
      V4 citation-decision consistency (run only when the needed upstream records
         are in ``accepted``; otherwise DEFERRED)."""
    if subtask not in _SUBTASK_DEF:
        raise ValueError(f"unknown subtask {subtask!r}")

    failed: list[str] = []
    deferred: list[str] = []

    # --- V1/V2: schema conformance + required-field presence ----------------
    if not isinstance(record, dict):
        failed.append("schema: record is not an object")
        return RecordVerdict(subtask, False, tuple(failed), tuple(deferred))

    schema_errors = sorted(
        _record_validator(subtask).iter_errors(record), key=lambda e: list(e.path)
    )
    if schema_errors:
        for err in schema_errors:
            location = "/".join(str(p) for p in err.path) or "<record>"
            failed.append(f"schema: {location}: {err.message}")
        # A structurally-broken record cannot be checked semantically (mirrors
        # validate_trace's early return on schema failure).
        return RecordVerdict(subtask, False, tuple(failed), tuple(deferred))

    # --- V3: citation presence (closed set + exemption must cite exemption) ---
    _check_citation_present(subtask, record, failed)

    # --- V4: citation-decision consistency (deferred when upstream missing) ---
    if subtask == "RAT":
        _rat_consistency(record, accepted, failed, deferred)
    elif subtask == "EXM":
        _exm_consistency(record, accepted, failed, deferred)
    elif subtask == "RCH":
        _rch_consistency(record, accepted, failed, deferred)
    # CLS and JUR carry no standalone V4 consistency check (mirrors validator).

    return RecordVerdict(subtask, not failed, tuple(failed), tuple(deferred))


def _check_citation_present(subtask: str, record: dict, failed: list[str]) -> None:
    ref = record.get("rule_reference")
    if not ref or not isinstance(ref, str):
        failed.append(f"citation-presence: {subtask} missing rule_reference")
        return
    if ref not in RULE_KEYS:
        failed.append(
            f"citation-presence: {subtask} rule_reference {ref!r} not in closed set"
        )
        return
    if subtask == "EXM" and record.get("decision", {}).get("exempt") is True:
        if ref != Rule.EXM_EXEMPT_SUPPLY.value:
            failed.append(
                f"citation-presence: EXM exempt=true must cite {Rule.EXM_EXEMPT_SUPPLY.value}"
            )


def _rat_consistency(
    record: dict, accepted: dict[str, dict], failed: list[str], deferred: list[str]
) -> None:
    decision = record.get("decision", {})
    band = decision.get("rate_band")
    rate = decision.get("rate")
    ref = record.get("rule_reference")

    if band is None:
        # Exempt line: rate n/a, must cite RATE.NA_EXEMPT. Self-contained.
        if rate is not None:
            failed.append("consistency: RAT band=null but rate is not null")
        if ref != Rule.RATE_NA_EXEMPT.value:
            failed.append(f"consistency: RAT band=null must cite {Rule.RATE_NA_EXEMPT.value}")
        return

    # Banded line: needs the accepted JUR record for the jurisdiction.
    jur = accepted.get("JUR")
    if jur is None:
        deferred.append("consistency: RAT rate vs jurisdiction table (awaiting JUR)")
        return
    jurisdiction = jur.get("decision", {}).get("jurisdiction")
    try:
        expected = RATE_TABLE[Jurisdiction(jurisdiction)][band]
    except (KeyError, ValueError):
        failed.append(f"consistency: RAT no table entry for ({jurisdiction}, {band})")
        return
    if rate != expected:
        failed.append(
            f"consistency: RAT rate {rate} != table {expected} for ({jurisdiction}, {band})"
        )
    if ref != _BAND_RULE.get(band):
        failed.append(f"consistency: RAT band {band!r} must cite {_BAND_RULE.get(band)}")


def _exm_consistency(
    record: dict, accepted: dict[str, dict], failed: list[str], deferred: list[str]
) -> None:
    is_exempt = record.get("decision", {}).get("exempt")
    if is_exempt is not True:
        return  # non-exempt lines need no category cross-check (mirrors validator)
    cls = accepted.get("CLS")
    if cls is None:
        deferred.append("consistency: EXM exempt=true vs category (awaiting CLS)")
        return
    category = cls.get("decision", {}).get("category")
    if category != Category.EXEMPT_SUPPLY.value:
        failed.append(
            f"consistency: EXM exempt=true but category is {category!r}, not EXEMPT_SUPPLY"
        )


def _rch_consistency(
    record: dict, accepted: dict[str, dict], failed: list[str], deferred: list[str]
) -> None:
    decision = record.get("decision", {})
    reverse_charge = decision.get("reverse_charge")
    outcome = decision.get("outcome")
    ref = record.get("rule_reference")

    if reverse_charge is True:
        jur = accepted.get("JUR")
        if jur is None:
            deferred.append("consistency: RCH reverse_charge vs JUR path (awaiting JUR)")
        else:
            jur_path = jur.get("decision", {}).get("jur_path")
            customer_registered = jur.get("support", {}).get("customer_vat_registered")
            if jur_path != "intra_community_b2b":
                failed.append(
                    f"consistency: RCH reverse_charge=true but jur_path is {jur_path!r}"
                )
            if customer_registered is not True:
                failed.append(
                    "consistency: RCH reverse_charge=true but customer_vat_registered is not true"
                )
            if ref != Rule.RCH_B2B_INTRA.value:
                failed.append(
                    f"consistency: RCH reverse_charge=true must cite {Rule.RCH_B2B_INTRA.value}"
                )

    if outcome == "exempt":
        exm = accepted.get("EXM")
        if exm is None:
            deferred.append("consistency: RCH outcome=exempt vs EXM (awaiting EXM)")
        else:
            if exm.get("decision", {}).get("exempt") is not True:
                failed.append("consistency: RCH outcome=exempt but EXM exempt is not true")
            if ref != Rule.RCH_EXEMPT.value:
                failed.append(
                    f"consistency: RCH outcome=exempt must cite {Rule.RCH_EXEMPT.value}"
                )


# ---------------------------------------------------------------------------
# Dependency-ordered driver + authoritative assembly gate.
# ---------------------------------------------------------------------------


def verdicts_in_dependency_order(emitted: Any) -> tuple[bool, list[RecordVerdict]]:
    """Evaluate validate_record over SUBTASKS in dependency order on an assembled
    trace. Each per-line record is validated with its own upstream context (the
    single case-level JUR record plus that line's already-accepted CLS/RAT/EXM).
    An upstream record enters ``accepted`` only if its own verdict accepted, so a
    structurally-broken upstream cannot crash a downstream check.

    Returns (all_accept, verdicts). This is the reference realization of the
    §7.1 equivalence invariant's left-hand side."""
    verdicts: list[RecordVerdict] = []
    all_accept = True

    if not isinstance(emitted, dict) or not isinstance(emitted.get("lines"), list):
        # Nothing to iterate in record order; the assembly gate is authoritative.
        return False, verdicts

    lines = emitted["lines"]
    jur = emitted.get("jur")

    # CLS (per line)
    cls_ok: list[bool] = []
    for line in lines:
        v = validate_record("CLS", _rec(line, "cls"), {})
        verdicts.append(v)
        cls_ok.append(v.accepted)
        all_accept &= v.accepted

    # JUR (case-level)
    vj = validate_record("JUR", jur if isinstance(jur, dict) else {}, {})
    verdicts.append(vj)
    all_accept &= vj.accepted
    jur_ctx: dict[str, dict] = {"JUR": jur} if (vj.accepted and isinstance(jur, dict)) else {}

    # RAT / EXM (per line, share layer)
    rat_ok: list[bool] = []
    exm_ok: list[bool] = []
    for i, line in enumerate(lines):
        acc = dict(jur_ctx)
        if cls_ok[i]:
            acc["CLS"] = _rec(line, "cls")
        vr = validate_record("RAT", _rec(line, "rat"), acc)
        verdicts.append(vr)
        rat_ok.append(vr.accepted)
        all_accept &= vr.accepted

        ve = validate_record("EXM", _rec(line, "exm"), acc)
        verdicts.append(ve)
        exm_ok.append(ve.accepted)
        all_accept &= ve.accepted

    # RCH (per line, terminal synthesis)
    for i, line in enumerate(lines):
        acc = dict(jur_ctx)
        if cls_ok[i]:
            acc["CLS"] = _rec(line, "cls")
        if rat_ok[i]:
            acc["RAT"] = _rec(line, "rat")
        if exm_ok[i]:
            acc["EXM"] = _rec(line, "exm")
        vc = validate_record("RCH", _rec(line, "rch"), acc)
        verdicts.append(vc)
        all_accept &= vc.accepted

    return all_accept, verdicts


def _rec(line: Any, key: str) -> dict:
    if isinstance(line, dict) and isinstance(line.get(key), dict):
        return line[key]
    return {}


def assembly_gate(emitted: Any) -> ValidationResult:
    """The AUTHORITATIVE completeness gate, unchanged in every condition: delegate
    to the frozen validator.validate_trace. Incremental verdicts route retries;
    this final full-trace check decides completeness (grounding §7.1)."""
    return validator.validate_trace(emitted)
