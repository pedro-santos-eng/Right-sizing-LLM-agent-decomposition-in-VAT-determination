"""surface.py — the fixed activity surface (grounding §1, §2, §3, §5).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_1_SURFACE.md (v1.1).

This module encodes the invariants the paper (§4.2) fixes across ALL conditions:
the subtask set and dependency order, the topological layering, the worker
partitions C1-C4, the worker-slice algebra, the exemption-table reference
artifact R, and the agent-visible case projection that enforces label isolation.

Label isolation (grounding §1) is non-negotiable: ``agent_case_view`` is the only
path from a Case to a prompt, and it drops ``true_category`` (the CLS oracle
label). This module is an AGENT-CONTEXT module: it must never import
``src.oracle.labeler`` or ``src.oracle.scorer`` (grounding §1.2). It imports only
``src.oracle.rules`` (tables/enums) — which itself imports no label source.

Pure: no I/O, no RNG, no clock, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

# Only rules.py is imported here (tables + the frozen Case shape). rules.py has
# no dependency on labeler/scorer, so importing surface cannot reach a label
# source (grounding §1.2 import-graph isolation).
from src.oracle.rules import CATEGORY_TABLE, Case, Category

# ---------------------------------------------------------------------------
# §2. Subtasks and dependency order (T, D). Fixed, imported everywhere.
# ---------------------------------------------------------------------------

# The fixed subtask order used for earliest-error attribution. MUST equal
# scorer.SUBTASK_ORDER (grounding §2; tested in test_surface).
SUBTASKS: tuple[str, ...] = ("CLS", "JUR", "RAT", "EXM", "RCH")

# Subtask -> the set of *subtasks* it depends on (case-field inputs like customer
# status are not subtasks and do not appear here) (grounding §2).
DEPENDS: dict[str, frozenset[str]] = {
    "CLS": frozenset(),
    "JUR": frozenset({"CLS"}),
    "RAT": frozenset({"CLS", "JUR"}),
    "EXM": frozenset({"CLS", "JUR"}),
    "RCH": frozenset({"CLS", "JUR", "RAT", "EXM"}),
}

# Topological layering consistent with DEPENDS. RAT and EXM share a layer.
LAYERS: tuple[frozenset[str], ...] = (
    frozenset({"CLS"}),
    frozenset({"JUR"}),
    frozenset({"RAT", "EXM"}),
    frozenset({"RCH"}),
)

PARALLEL_ELIGIBLE: frozenset[str] = frozenset({"RAT", "EXM"})

# The five run conditions. C1-C4 are worker partitions (§3.1); S0 is the
# no-orchestrator condition (§3.4). Used for run-record identity enums (runlog).
CONDITIONS: tuple[str, ...] = ("C1", "C2", "C3", "C4", "S0")


# ---------------------------------------------------------------------------
# §3.1 Worker partitions (paper §4.3, verbatim in substance).
# Workers within a partition are ordered by the position of their earliest
# subtask in SUBTASKS (the tuple order below already satisfies this).
# ---------------------------------------------------------------------------

PARTITIONS: dict[str, tuple[frozenset[str], ...]] = {
    "C1": (frozenset({"CLS", "JUR", "RAT", "EXM", "RCH"}),),
    "C2": (frozenset({"CLS", "JUR"}), frozenset({"RAT", "EXM", "RCH"})),
    "C3": (frozenset({"CLS"}), frozenset({"JUR"}), frozenset({"RAT", "EXM", "RCH"})),
    "C4": (
        frozenset({"CLS"}),
        frozenset({"JUR"}),
        frozenset({"RAT"}),
        frozenset({"EXM"}),
        frozenset({"RCH"}),
    ),
}


# ---------------------------------------------------------------------------
# §3.2 Atom slices — the C4 rows. Per-subtask visible state S_tau and tool
# permissions F_tau, kept conceptually SEPARATE (grounding §3.2, §4.1).
#
# State atoms are symbolic tokens so the slice algebra (union / intra-worker
# subtraction, §3.3) is exact and testable. Two kinds of atom:
#   - case-field atoms  : projections of agent_case_view (identifier case_id is
#                         not "state" and is always implicitly available).
#   - record atoms      : "record:<TAU>", a structured record produced by a
#                         subtask worker.
#   - reference atom    : the exemption table R (visible state for EXM only).
# ---------------------------------------------------------------------------

# The four tools (F). Names are the callables in tools.py (grounding §4).
ALL_TOOLS: frozenset[str] = frozenset(
    {
        "classification_reference",
        "vat_registration_check",
        "rate_table_lookup",
        "rule_citation_retrieval",
    }
)

# Case-field state atoms = exactly the non-identifier fields of agent_case_view.
CASE_VIEW_ATOMS: frozenset[str] = frozenset(
    {
        "line_items",
        "supplier_country",
        "customer_country",
        "transaction_type",
        "customer_vat_registered",
    }
)

# The reference set R (grounding §5): the exemption table artifact, delivered as
# visible state to whichever worker owns EXM.
REFERENCE_ATOM: str = "exemption_table"


def _record_atom(tau: str) -> str:
    """The structured-record atom produced by subtask ``tau``."""
    return f"record:{tau}"


# S_tau: visible state atoms for each subtask (grounding §3.2 table).
STATE_ATOMS: dict[str, frozenset[str]] = {
    "CLS": frozenset({"line_items"}),
    "JUR": frozenset(
        {
            "supplier_country",
            "customer_country",
            "transaction_type",
            "customer_vat_registered",
            _record_atom("CLS"),
        }
    ),
    "RAT": frozenset({_record_atom("JUR"), _record_atom("CLS")}),
    "EXM": frozenset({_record_atom("JUR"), _record_atom("CLS"), REFERENCE_ATOM}),
    "RCH": frozenset(
        {
            _record_atom("CLS"),
            _record_atom("JUR"),
            _record_atom("RAT"),
            _record_atom("EXM"),
        }
    ),
}

# F_tau: callable tool permissions for each subtask (grounding §3.2 table).
TOOL_ATOMS: dict[str, frozenset[str]] = {
    "CLS": frozenset({"classification_reference"}),
    "JUR": frozenset({"vat_registration_check", "rule_citation_retrieval"}),
    "RAT": frozenset({"rate_table_lookup"}),
    "EXM": frozenset({"rule_citation_retrieval"}),
    "RCH": frozenset({"rule_citation_retrieval"}),
}


@dataclass(frozen=True)
class WorkerSlice:
    """A worker's activity slice: what it is assigned, what it may call, and what
    visible state it receives as input. ``tools`` (F) and ``input_state`` (S) are
    kept separate on purpose (grounding §3.2, §4.1)."""

    assigned: frozenset[str]
    tools: frozenset[str]
    input_state: frozenset[str]


# ---------------------------------------------------------------------------
# §3.3 Composition rule for coarser workers.
# ---------------------------------------------------------------------------


def slice_for(assigned: frozenset[str]) -> WorkerSlice:
    """Compose the activity slice for a worker owning ``assigned`` subtasks.

    - tools       = union of F_tau over assigned.
    - input_state = union of S_tau over assigned, MINUS any record atom the
      worker produces itself (a worker does not receive as input a record it is
      responsible for producing) (grounding §3.3).
    """
    unknown = set(assigned) - set(SUBTASKS)
    if unknown:
        raise ValueError(f"unknown subtasks in slice: {sorted(unknown)}")
    if not assigned:
        raise ValueError("worker slice must own at least one subtask")

    tools: frozenset[str] = frozenset().union(*(TOOL_ATOMS[t] for t in assigned))
    raw_state: frozenset[str] = frozenset().union(*(STATE_ATOMS[t] for t in assigned))
    produced = frozenset(_record_atom(t) for t in assigned)
    input_state = raw_state - produced
    return WorkerSlice(assigned=frozenset(assigned), tools=tools, input_state=input_state)


def partition_slices(condition: str) -> tuple[WorkerSlice, ...]:
    """The ordered worker slices for a partition condition (C1-C4).

    Workers keep the partition's declared order (earliest subtask in SUBTASKS
    first) (grounding §3.1). S0 is not a partition and is handled by Layer 2
    (§3.4); it is not accepted here."""
    if condition not in PARTITIONS:
        raise ValueError(f"{condition!r} is not a partition condition (C1-C4)")
    return tuple(slice_for(group) for group in PARTITIONS[condition])


# ---------------------------------------------------------------------------
# §5. Reference set R — the exemption-table artifact.
# Rendered ONCE from CATEGORY_TABLE into a fixed, deterministic string constant.
# Delivered as visible state (never a callable) to whichever worker owns EXM;
# identical bytes across all conditions.
# ---------------------------------------------------------------------------

# Fixed category order for deterministic rendering (no set/enum-iteration leak).
_CATEGORY_ORDER: tuple[Category, ...] = (
    Category.GEN_GOODS,
    Category.RED_GOODS,
    Category.GEN_SERVICE,
    Category.RED_SERVICE,
    Category.EXEMPT_SUPPLY,
)


def _render_exemption_table() -> str:
    header = "EXEMPTION TABLE (bounded reference R). Only categories marked 'yes' are exempt."
    rows = ["category=%s exempt=%s" % (
        cat.value, "yes" if CATEGORY_TABLE[cat]["exempt"] else "no",
    ) for cat in _CATEGORY_ORDER]
    return header + "\n" + "\n".join(rows) + "\n"


# Byte-stable constant (rendered at import from CATEGORY_TABLE, deterministic).
EXEMPTION_TABLE_TEXT: str = _render_exemption_table()


# ---------------------------------------------------------------------------
# §1.1 The agent-visible case projection (label isolation).
# The ONLY path from a Case to a prompt. Drops LineItem.true_category, which IS
# the CLS oracle label (grounding §1.1). No other key may appear; the result is
# validated against schemas/agent_case_view.schema.json (additionalProperties
# false, all fields required).
# ---------------------------------------------------------------------------


def agent_case_view(case: Case) -> dict:
    """Project a Case to the exact, label-free view an agent may see.

    Returns exactly the keys in grounding §1.1 — no ``true_category`` anywhere.
    Assert on structure, not text, when testing (a description legitimately
    contains the category name, e.g. 'synthetic-GEN_SERVICE'; §1.1, §1.3)."""
    return {
        "case_id": case.case_id,
        "supplier_country": case.supplier_country.value,
        "customer_country": case.customer_country.value,
        "customer_vat_registered": case.customer_vat_registered,
        "transaction_type": case.transaction_type.value,
        "line_items": [
            {
                "line_id": li.line_id,
                "description": li.description,
                "amount": li.amount,
            }
            for li in case.line_items
        ],
    }
