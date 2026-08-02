"""prompts.py — worker prompt assembly P_r (grounding
HARNESS_GROUNDING_2_ORCHESTRATION §4).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_2_ORCHESTRATION.md (v1.1); Layer-1
interfaces binding.

Prompt assembly is a PURE FUNCTION OF THE SLICE (§4): identical slices ⇒
identical prompts, so the paper's "only the partition of subtasks across workers
changes" holds at the prompt level too, by construction. In particular the
{RAT,EXM,RCH} worker is byte-identical between C2 and C3 (same slice).

This module holds only STATIC components (§4):
  - a shared role-preamble template,
  - per-subtask instruction blocks,
  - per-subtask output contracts rendered FROM the frozen schema $defs
    (generated, never hand-copied — §4, §10),
  - the tool-use rules,
  - the EXEMPTION_TABLE_TEXT inclusion rule (EXM owner only).
``assemble_prompt(slice)`` composes them. ``prompt_hashes`` snapshots SHA-256 of
every assembled system message per condition (§4) for the run record.

There is NO per-condition tuning: any wording change edits a shared component
and propagates to every condition uniformly (§4, §9).

This is a TRUE agent-context module (grounding §1.2): it imports only
``src.harness.surface`` (which reaches at most ``src.oracle.rules``) and reads
the frozen output schema as a data file. It never imports
``validator``/``labeler``/``scorer`` and constructs no label.

Pure: no network, no clock, no LLM.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from src.harness.surface import (
    EXEMPTION_TABLE_TEXT,
    PARTITIONS,
    SUBTASKS,
    WorkerSlice,
    slice_for,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "final_trace.schema.json"

# Per-line subtasks emit one record per line item; JUR is case-level (grounding
# ORACLE_GROUNDING §2.2). This governs the bundle payload shape.
PER_LINE_SUBTASKS: frozenset[str] = frozenset({"CLS", "RAT", "EXM", "RCH"})
CASE_LEVEL_SUBTASKS: frozenset[str] = frozenset({"JUR"})

# $defs entry name per subtask (matches the consolidated schema, grounding §6).
_SUBTASK_DEF = {"CLS": "cls", "JUR": "jur", "RAT": "rat", "EXM": "exm", "RCH": "rch"}


# ---------------------------------------------------------------------------
# Worker identity — deterministic, condition-tagged (used for scripting, run
# records, and hash keys). NOT part of the prompt (the prompt is condition-free).
# ---------------------------------------------------------------------------


def ordered_assigned(assigned: frozenset[str]) -> tuple[str, ...]:
    """``assigned`` in fixed SUBTASKS order."""
    return tuple(t for t in SUBTASKS if t in assigned)


def worker_id(condition: str, assigned: frozenset[str]) -> str:
    """Stable id, e.g. ``C4:RAT`` or ``C1:CLS+JUR+RAT+EXM+RCH`` (§3.2 run record)."""
    return f"{condition}:{'+'.join(ordered_assigned(assigned))}"


# ---------------------------------------------------------------------------
# Static components (§4). Shared across all conditions — no per-condition tuning.
# ---------------------------------------------------------------------------

ROLE_PREAMBLE = (
    "You are a VAT determination worker in a decomposed multi-agent system. "
    "You are responsible for a fixed slice of the determination and nothing "
    "else. Work only from the state you are given and the tools you may call. "
    "Do not restate table values from memory; obtain rates, category kinds, and "
    "citation keys through the tools. Be deterministic and terse."
)

# Per-subtask instruction blocks. These describe the CONDITIONS of each subtask,
# never the answer for a specific input (grounding §4.4 register; label
# isolation). Keyed by subtask; shared verbatim across all conditions.
SUBTASK_INSTRUCTIONS: dict[str, str] = {
    "CLS": (
        "CLS — classification. Assign each line item to exactly one category "
        "from the closed vocabulary. Call classification_reference to obtain the "
        "vocabulary and each category's kind. Cite CLS.ASSIGNED."
    ),
    "JUR": (
        "JUR — jurisdiction / place of supply. From the supplier and customer "
        "countries, the transaction type, and the customer's VAT-registration "
        "status, determine the jurisdiction whose regime applies and the path "
        "taken (domestic / intra_community_b2b / b2c_cross_border). You may call "
        "vat_registration_check for the registration input and "
        "rule_citation_retrieval for the governing citation."
    ),
    "RAT": (
        "RAT — rate lookup. For each non-exempt line, look up the rate for the "
        "jurisdiction and rate band with rate_table_lookup; never invent a rate. "
        "For an exempt line, no rate band applies: record rate_band and rate as "
        "null and cite RATE.NA_EXEMPT."
    ),
    "EXM": (
        "EXM — exemption check. Using the exemption table provided in your state, "
        "decide whether each line is exempt. An exempt line must cite the "
        "exemption rule; a non-exempt line is taxed according to its band."
    ),
    "RCH": (
        "RCH — reverse-charge / liable-party synthesis. For each line resolve "
        "exactly one outcome under the precedence EXEMPT > REVERSE_CHARGE > "
        "STANDARD_CHARGE, and set reverse_charge, liable_party, vat_amount, and "
        "the non-charging reason accordingly. Cite the governing rule obtained "
        "from rule_citation_retrieval."
    ),
}

TOOL_USE_RULES = (
    "Tool-use rules: cite only rule keys returned by rule_citation_retrieval; "
    "never guess table values — call rate_table_lookup for rates and "
    "classification_reference for category kinds; call vat_registration_check "
    "for the registration input where relevant."
)


# ---------------------------------------------------------------------------
# Output contracts — generated from the frozen schema $defs (§4, §10).
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _defs() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]


def _render_decision_fields(decision_schema: dict) -> str:
    """Render the decision sub-schema's required fields + enums, deterministically."""
    props = decision_schema.get("properties", {})
    required = decision_schema.get("required", [])
    parts: list[str] = []
    for name in required:  # required order is the schema's own order (deterministic)
        spec = props.get(name, {})
        if "enum" in spec:
            allowed = ", ".join(json.dumps(v) for v in spec["enum"])
            parts.append(f"{name} (one of: {allowed})")
        elif "const" in spec:
            parts.append(f"{name} == {json.dumps(spec['const'])}")
        else:
            typ = spec.get("type", "any")
            typ_s = "|".join(typ) if isinstance(typ, list) else str(typ)
            parts.append(f"{name} ({typ_s})")
    return "; ".join(parts)


def output_contract(subtask: str) -> str:
    """The emitted-record contract for one subtask, GENERATED from the schema
    $defs (never hand-copied — §4, §10). Shape is {subtask, decision, support,
    rule_reference} per grounding §6."""
    entry = _defs()[_SUBTASK_DEF[subtask]]
    decision = entry["properties"]["decision"]
    fields = _render_decision_fields(decision)
    return (
        f"{subtask} record: an object with keys subtask (== \"{subtask}\"), "
        f"decision {{{fields}}}, support (object of the inputs you consumed), and "
        f"rule_reference (a citation key)."
    )


def _object_contract(label: str, def_name: str) -> str:
    """Render an object contract (required keys, optional keys, closed-set) FROM
    the frozen schema $defs — same generate-not-copy mechanism as
    ``output_contract`` (§4, §10)."""
    d = _defs()[def_name]
    props = d.get("properties", {})
    required = d.get("required", [])

    def describe(k: str) -> str:
        spec = props.get(k, {})
        if "enum" in spec:
            return f'{k} (one of: {", ".join(json.dumps(v) for v in spec["enum"])})'
        if "$ref" in spec:
            return f"{k} (the {k.upper()} record)"
        typ = spec.get("type", "any")
        typ_s = "|".join(typ) if isinstance(typ, list) else str(typ)
        return f"{k} ({typ_s})"

    req_s = "; ".join(describe(k) for k in required)
    optional = [k for k in props if k not in required]
    opt_s = f" Optional: {'; '.join(describe(k) for k in optional)}." if optional else ""
    closed = " Emit no other keys." if d.get("additionalProperties") is False else ""
    return f'"{label}": an object whose required keys are: {req_s}.{opt_s}{closed}'


def final_contract() -> str:
    """The ``final`` aggregation-block contract, generated from the schema $defs.
    Public so ``s0.py`` reuses the identical rendering (§4, §6)."""
    return _object_contract("final", "final")


# ---------------------------------------------------------------------------
# Bundle emission contract — the worker emits ONE JSON object covering its whole
# assigned bundle (grounding §3.2). Per-line subtasks appear under "lines" keyed
# by line_id; JUR (if owned) at top level "jur"; the RCH owner additionally
# emits the "final" aggregation block (§3.5 — the harness never computes final).
# ---------------------------------------------------------------------------


def _bundle_contract(assigned: frozenset[str]) -> str:
    ordered = ordered_assigned(assigned)
    per_line = [t for t in ordered if t in PER_LINE_SUBTASKS]
    lines: list[str] = []
    lines.append(
        "Emit exactly one JSON object in a single fenced ```json block as the "
        "LAST content of your final message. The object must contain only the "
        "records for the subtasks you own:"
    )
    if per_line:
        keys = ", ".join(f'"{t.lower()}"' for t in per_line)
        lines.append(
            f'  - "lines": an array with exactly one entry per line item. Each '
            f'entry is an object with keys "line_id" (the line id) and {keys} — '
            f'one record per owned per-line subtask ({", ".join(per_line)}). Every '
            f"listed key is required on every line; emit no line without all of them."
        )
    if "JUR" in assigned:
        lines.append('  - "jur": your single case-level JUR record.')
    if "RCH" in assigned:
        lines.append(
            "  - " + final_contract()
            + ' Each "lines" entry summarises one line item. You must produce this '
            "block yourself; it is not computed for you."
        )
    return "\n".join(lines)


def _input_state_description(slice_: WorkerSlice) -> str:
    """Describe the SHAPE of state the worker receives (slice-dependent, not
    case-dependent) so the prompt stays a pure function of the slice (§4)."""
    atoms = sorted(slice_.input_state)
    if not atoms:
        return "You will receive: the case identifier only."
    friendly = []
    for a in atoms:
        if a.startswith("record:"):
            friendly.append(f"the accepted {a.split(':', 1)[1]} record(s)")
        elif a == "exemption_table":
            friendly.append("the exemption table (reference R)")
        else:
            friendly.append(a)
    return "You will receive: " + ", ".join(friendly) + "."


# ---------------------------------------------------------------------------
# assemble_prompt — the pure slice -> system-message function (§4).
# ---------------------------------------------------------------------------


def assemble_prompt(slice_: WorkerSlice) -> str:
    """Compose the worker system message from static components + the slice.
    Deterministic and condition-free: identical slices ⇒ identical prompts (§4)."""
    ordered = ordered_assigned(slice_.assigned)
    sections: list[str] = [ROLE_PREAMBLE, ""]

    sections.append("Your assigned subtasks: " + ", ".join(ordered) + ".")
    sections.append(_input_state_description(slice_))
    sections.append("Tools you may call: " + (", ".join(sorted(slice_.tools)) or "none") + ".")
    sections.append("")

    for t in ordered:
        sections.append(SUBTASK_INSTRUCTIONS[t])
    sections.append("")

    for t in ordered:
        sections.append(output_contract(t))
    sections.append("")

    # EXEMPTION_TABLE_TEXT inclusion rule: EXM owner only (§4, §5).
    if "EXM" in slice_.assigned:
        sections.append("Exemption table (reference R), authoritative:")
        sections.append(EXEMPTION_TABLE_TEXT.rstrip("\n"))
        sections.append("")

    sections.append(TOOL_USE_RULES)
    sections.append("")
    sections.append(_bundle_contract(slice_.assigned))

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Prompt freezing — SHA-256 of every assembled system message per condition (§4).
# Written into every run record; a change after freeze aborts the run (Layer 4).
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hashes(condition: str) -> dict[str, str]:
    """{worker_id: sha256(system_message)} for each worker in a partition
    condition (C1-C4). S0 hashes are provided by ``s0.py`` (§6)."""
    if condition not in PARTITIONS:
        raise ValueError(f"{condition!r} is not a partition condition (C1-C4)")
    out: dict[str, str] = {}
    for group in PARTITIONS[condition]:
        wid = worker_id(condition, group)
        out[wid] = _sha256(assemble_prompt(slice_for(group)))
    return out


def all_partition_prompt_hashes() -> dict[str, str]:
    """Merged worker prompt hashes across C1-C4 (deterministic). Demonstrates the
    {RAT,EXM,RCH} slice is identical across C2 and C3 (same hash)."""
    out: dict[str, str] = {}
    for condition in ("C1", "C2", "C3", "C4"):
        out.update(prompt_hashes(condition))
    return out
