"""tools.py — the four tools F (grounding §4), tool logging (§7.2), and the
injection seams (§8; interfaces + no-op defaults only, content is Layer 3).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_1_SURFACE.md (v1.1).

All four tools:
  - are deterministic;
  - import their tables from src.oracle.rules (ZERO restated tables, §4);
  - enforce closed sets;
  - return STRUCTURED ERRORS, never raise into agent context;
  - log every invocation to the active run log (§7.2);
  - return no wall-clock content.

This is an AGENT-CONTEXT module (grounding §1.2): it must never import
``src.oracle.labeler`` or ``src.oracle.scorer``. It imports only
``src.oracle.rules`` (tables/enums), which reaches no label source.

Design note (flagged for review, grounding §11): the frozen tool signatures in
§4 are positional and carry no logger/context argument, yet §7.2 requires every
invocation to be logged and §8.3 puts an outage seam *inside* rate_table_lookup.
Layer 1 reconciles this with a module-level, no-op-by-default ToolContext (an
explicit log sink + injection controller + active case id) installed per run via
``using_context``. The agent-facing call surface stays exactly as §4 froze it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from src.oracle.rules import (
    CATEGORY_TABLE,
    RATE_TABLE,
    RULE_KEYS,
    Category,
    Jurisdiction,
    Rule,
)

# ---------------------------------------------------------------------------
# §4.4 RULE_TEXT — the ONLY newly authored content in Layer 1. One or two
# sentences per key, statutory register, describing CONDITIONS not answers:
# no case names, no per-input outcomes, no concrete resolutions. Frozen
# byte-identical across all conditions. Keys must equal rules.RULE_KEYS exactly.
# ---------------------------------------------------------------------------

RULE_TEXT: dict[str, str] = {
    Rule.CLS_ASSIGNED.value: (
        "Each line item is assigned to exactly one category from the closed "
        "classification vocabulary according to the goods or services it "
        "describes. The classification fixes the item's kind and rate band."
    ),
    Rule.JUR_DOMESTIC.value: (
        "Where the supplier and the customer are established in the same "
        "jurisdiction, the supply is domestic and the place of supply is that "
        "jurisdiction."
    ),
    Rule.JUR_INTRA_B2B.value: (
        "Where the supplier and the customer are established in different member "
        "jurisdictions and the customer is a taxable person, the place of supply "
        "is the jurisdiction in which the customer is established."
    ),
    Rule.JUR_B2C_CROSS.value: (
        "Where the supplier and the customer are established in different "
        "jurisdictions and the customer is a non-taxable person, the place of "
        "supply is, under the bounded model, the jurisdiction of the supplier."
    ),
    Rule.RATE_STANDARD.value: (
        "A line item in the standard-rated band is taxed at the standard rate "
        "published for the applicable jurisdiction."
    ),
    Rule.RATE_REDUCED.value: (
        "A line item in the reduced-rated band is taxed at the reduced rate "
        "published for the applicable jurisdiction."
    ),
    Rule.RATE_NA_EXEMPT.value: (
        "Where a supply is exempt, no rate band applies and no rate is looked "
        "up; the rate is recorded as not applicable."
    ),
    Rule.EXM_EXEMPT_SUPPLY.value: (
        "A supply falling within the exemption table is exempt from tax; it "
        "carries no chargeable rate, and the exemption must be cited."
    ),
    Rule.EXM_NONE.value: (
        "A supply that does not fall within the exemption table is not exempt "
        "and is taxed according to its rate band."
    ),
    Rule.RCH_DOMESTIC_SUPPLIER.value: (
        "On a domestic supply the supplier accounts for and charges the tax; the "
        "liable party is the supplier."
    ),
    Rule.RCH_B2B_INTRA.value: (
        "On a cross-border business-to-business supply where the customer is a "
        "taxable person, liability shifts to the customer under the reverse-"
        "charge mechanism and the supplier charges no tax."
    ),
    Rule.RCH_B2C_SUPPLIER.value: (
        "On a cross-border supply to a non-taxable person the supplier charges "
        "tax in the applicable place of supply; the liable party is the supplier."
    ),
    Rule.RCH_EXEMPT.value: (
        "Where a supply is exempt, no tax is charged and no reverse charge "
        "arises regardless of the parties; exemption takes precedence."
    ),
}


# ---------------------------------------------------------------------------
# §8 Injection seams — interfaces + no-op defaults only (content is Layer 3).
# Seam signatures the Layer-2 dispatch / Layer-3 controller will implement:
#   - worker_timeout(case_id, subtask)          -> bool     (seam 1)
#   - hallucinate(case_id, subtask, record)     -> dict|None (seam 2)
#   - rate_outage(case_id)                       -> bool     (seam 3, this module)
# The default controller is a pure no-op: every seam is inactive.
# ---------------------------------------------------------------------------

# Seam identifiers used in run-record injection events (§7.2 / §8).
SEAM_WORKER_TIMEOUT = "worker_timeout"
SEAM_HALLUCINATED_OUTPUT = "hallucinated_output"
SEAM_RATE_TABLE_OUTAGE = "rate_table_outage"


class InjectionController:
    """No-op default injection controller (grounding §8). Layer 3 subclasses this
    to supply deterministic target sampling / schedules; Layer 1 never fires."""

    def worker_timeout(self, case_id: str, subtask: str) -> bool:  # seam 1
        return False

    def hallucinate(self, case_id: str, subtask: str, record: dict) -> Optional[dict]:  # seam 2
        return None

    def rate_outage(self, case_id: Optional[str]) -> bool:  # seam 3 (rate_table_lookup)
        return False


@dataclass
class ToolLog:
    """A run's tool-call ledger. ``tool_invocations`` are in call order; injection
    markers land in ``injection_events`` (§7.2). Both are plain dicts so runlog
    can serialize them without importing this module."""

    tool_invocations: list = field(default_factory=list)
    injection_events: list = field(default_factory=list)


@dataclass
class ToolContext:
    """Per-run tool context. No-op default: null log sink (no logging), no active
    case, no-op injection controller."""

    log: Optional[ToolLog] = None
    active_case_id: Optional[str] = None
    injection: InjectionController = field(default_factory=InjectionController)


_NULL_CONTEXT = ToolContext()
_CTX: ToolContext = _NULL_CONTEXT


def get_context() -> ToolContext:
    return _CTX


def set_context(ctx: ToolContext) -> None:
    global _CTX
    _CTX = ctx


@contextmanager
def using_context(ctx: ToolContext) -> Iterator[ToolContext]:
    """Install ``ctx`` for the duration of the block, restoring the previous
    context afterwards (safe for tests and nested runs)."""
    global _CTX
    previous = _CTX
    _CTX = ctx
    try:
        yield ctx
    finally:
        _CTX = previous


def _log_invocation(name: str, arguments: dict, result: dict) -> None:
    ctx = _CTX
    if ctx.log is not None:
        ctx.log.tool_invocations.append(
            {"tool": name, "arguments": arguments, "result": result}
        )


def _log_injection(seam: str, case_id: Optional[str], subtask: Optional[str], note: str) -> None:
    ctx = _CTX
    if ctx.log is not None:
        ctx.log.injection_events.append(
            {
                "seam": seam,
                "case_id": case_id,
                "subtask": subtask,
                "fired": True,
                "note": note,
            }
        )


# ---------------------------------------------------------------------------
# The pinned case registry for the ONE case-keyed tool (vat_registration_check).
# Built from the frozen data/ artifact (the pinned dataset the manifest
# identifies). Only the *input* block is read — customer_vat_registered is an
# input field, not a label (grounding §4.2). oracle_trace is never touched.
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@lru_cache(maxsize=1)
def _case_registry() -> dict[str, bool]:
    """Map case_id -> customer_vat_registered from the frozen dataset.

    The cache holds EXACTLY {case_id: bool} — the single registration field, a
    bare boolean. The surrounding input block (which also carries the CLS label
    true_category) is read transiently and never retained (grounding §1, §4.2)."""
    registry: dict[str, bool] = {}
    for split in ("eval_cases", "dev_cases"):
        split_dir = _DATA_DIR / split
        if not split_dir.is_dir():
            continue
        for path in sorted(split_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            case_input = payload.get("input", {})
            case_id = case_input.get("case_id")
            if case_id is not None:
                # store only the bare boolean, never the input block
                registry[case_id] = bool(case_input.get("customer_vat_registered"))
    return registry


# ---------------------------------------------------------------------------
# §4.1 classification_reference — static, case-independent. Closed category
# vocabulary with each category's kind, derived from rules.CATEGORY_TABLE. Does
# NOT return rate bands or exemption flags, and never sees a case.
# ---------------------------------------------------------------------------

# Fixed category order (no set/enum-iteration leak into output).
_CATEGORY_ORDER: tuple[Category, ...] = (
    Category.GEN_GOODS,
    Category.RED_GOODS,
    Category.GEN_SERVICE,
    Category.RED_SERVICE,
    Category.EXEMPT_SUPPLY,
)


def classification_reference() -> dict:
    result = {
        "categories": [
            {"category": cat.value, "kind": CATEGORY_TABLE[cat]["kind"].value}
            for cat in _CATEGORY_ORDER
        ]
    }
    _log_invocation("classification_reference", {}, result)
    return result


# ---------------------------------------------------------------------------
# §4.2 vat_registration_check — the ONLY case-keyed tool. Returns the input
# field customer_vat_registered for the pinned case. Unknown id -> UNKNOWN_CASE.
# ---------------------------------------------------------------------------


def vat_registration_check(case_id: str) -> dict:
    registry = _case_registry()
    if case_id not in registry:
        result = {"error": "UNKNOWN_CASE"}
    else:
        result = {"case_id": case_id, "customer_vat_registered": registry[case_id]}
    _log_invocation("vat_registration_check", {"case_id": case_id}, result)
    return result


# ---------------------------------------------------------------------------
# §4.3 rate_table_lookup — serves ONLY the eight real rows of rules.RATE_TABLE
# (DE/FR/IE/ES x standard/reduced). Any other input -> NO_SUCH_ENTRY. The exempt
# path (band null / RATE.NA_EXEMPT) is reasoned by the agent, never served here.
# Carries the Layer-3 outage seam (§8.3): for a designated case, the first RAT
# invocation returns TOOL_UNAVAILABLE and recovers thereafter (no-op default).
# ---------------------------------------------------------------------------

_VALID_BANDS = frozenset({"standard", "reduced"})


def rate_table_lookup(jurisdiction: str, band: str) -> dict:
    ctx = _CTX
    arguments = {"jurisdiction": jurisdiction, "band": band}

    # §8.3 outage seam — consulted before the lookup. No-op by default.
    if ctx.injection.rate_outage(ctx.active_case_id):
        result = {"error": "TOOL_UNAVAILABLE"}
        _log_injection(
            SEAM_RATE_TABLE_OUTAGE,
            ctx.active_case_id,
            "RAT",
            "rate_table_lookup transient outage (m=1)",
        )
        _log_invocation("rate_table_lookup", arguments, result)
        return result

    if band not in _VALID_BANDS:
        # Unknown band, "exempt", null, etc. — the exempt path is not served.
        result = {"error": "NO_SUCH_ENTRY"}
    else:
        try:
            rate = RATE_TABLE[Jurisdiction(jurisdiction)][band]
        except (KeyError, ValueError):
            result = {"error": "NO_SUCH_ENTRY"}
        else:
            result = {"jurisdiction": jurisdiction, "band": band, "rate": rate}
    _log_invocation("rate_table_lookup", arguments, result)
    return result


# ---------------------------------------------------------------------------
# §4.4 rule_citation_retrieval — closed set = rules.RULE_KEYS (13 keys). Returns
# the frozen RULE_TEXT for the key. Unknown key -> UNKNOWN_RULE_KEY.
# ---------------------------------------------------------------------------


def rule_citation_retrieval(rule_key: str) -> dict:
    if rule_key in RULE_TEXT:
        result = {"rule_key": rule_key, "text": RULE_TEXT[rule_key]}
    else:
        result = {"error": "UNKNOWN_RULE_KEY"}
    _log_invocation("rule_citation_retrieval", {"rule_key": rule_key}, result)
    return result


# The callable surface, by name (matches surface.ALL_TOOLS / F_tau atoms).
TOOLS = {
    "classification_reference": classification_reference,
    "vat_registration_check": vat_registration_check,
    "rate_table_lookup": rate_table_lookup,
    "rule_citation_retrieval": rule_citation_retrieval,
}
