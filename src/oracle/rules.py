"""
rules.py — Bounded VAT rule set (the oracle's logic core).

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md. Read it before editing this file.
Where a docstring and the grounding doc disagree, the grounding doc wins.

This module is PURE: no I/O, no network, no LLM, no randomness, no clock.
It defines the bounded tables, the closed set of rule-reference keys, the five
subtask resolvers (CLS, JUR, RAT, EXM, RCH), and the precedence rule
(EXEMPT > REVERSE_CHARGE > STANDARD_CHARGE).

`labeler.py` composes these resolvers over a whole case. `generator.py` is the
only module allowed to use an RNG. `validator.py`/`scorer.py` consume the
rule-reference keys and tables defined here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# §1.1 Jurisdictions (closed set). Rates as exact fractions to keep determinism.
# ---------------------------------------------------------------------------


class Jurisdiction(str, Enum):
    DE = "DE"
    FR = "FR"
    IE = "IE"
    ES = "ES"
    # XX (rest-of-world) intentionally omitted from the default bounded pilot.
    # See grounding §1.1 before reintroducing it.


# Standard/reduced rates per grounding §1.1. Stored as (numerator, denominator)
# or Decimal-friendly strings to avoid float drift; here we use float for the
# skeleton but the implementation SHOULD use decimal.Decimal for exact arithmetic.
RATE_TABLE: dict[Jurisdiction, dict[str, float]] = {
    Jurisdiction.DE: {"standard": 0.19, "reduced": 0.07},
    Jurisdiction.FR: {"standard": 0.20, "reduced": 0.055},
    Jurisdiction.IE: {"standard": 0.23, "reduced": 0.135},
    Jurisdiction.ES: {"standard": 0.21, "reduced": 0.10},
}


# ---------------------------------------------------------------------------
# §1.2 Product/service classification vocabulary (closed set).
# ---------------------------------------------------------------------------


class Category(str, Enum):
    GEN_GOODS = "GEN_GOODS"
    RED_GOODS = "RED_GOODS"
    GEN_SERVICE = "GEN_SERVICE"
    RED_SERVICE = "RED_SERVICE"
    EXEMPT_SUPPLY = "EXEMPT_SUPPLY"


class Kind(str, Enum):
    GOODS = "goods"
    SERVICE = "service"
    EITHER = "either"  # EXEMPT_SUPPLY


# category -> (kind, rate_band, exemption_eligible)
CATEGORY_TABLE: dict[Category, dict] = {
    Category.GEN_GOODS: {"kind": Kind.GOODS, "rate_band": "standard", "exempt": False},
    Category.RED_GOODS: {"kind": Kind.GOODS, "rate_band": "reduced", "exempt": False},
    Category.GEN_SERVICE: {"kind": Kind.SERVICE, "rate_band": "standard", "exempt": False},
    Category.RED_SERVICE: {"kind": Kind.SERVICE, "rate_band": "reduced", "exempt": False},
    Category.EXEMPT_SUPPLY: {"kind": Kind.EITHER, "rate_band": None, "exempt": True},
}


# ---------------------------------------------------------------------------
# §1.3 Transaction roles
# ---------------------------------------------------------------------------


class TxnType(str, Enum):
    B2B = "B2B"
    B2C = "B2C"


class JurPath(str, Enum):
    DOMESTIC = "domestic"
    INTRA_COMMUNITY_B2B = "intra_community_b2b"
    B2C_CROSS_BORDER = "b2c_cross_border"


class Outcome(str, Enum):
    STANDARD_CHARGE = "standard_charge"
    REVERSE_CHARGE = "reverse_charge"
    EXEMPT = "exempt"


# ---------------------------------------------------------------------------
# §3 Rule-reference keys (closed set). Citations must come from this set.
# ---------------------------------------------------------------------------


class Rule(str, Enum):
    # CLS
    CLS_ASSIGNED = "CLS.ASSIGNED"
    # JUR
    JUR_DOMESTIC = "JUR.DOMESTIC"
    JUR_INTRA_B2B = "JUR.INTRA_EU_B2B"
    JUR_B2C_CROSS = "JUR.B2C_CROSS_BORDER"
    # RAT
    RATE_STANDARD = "RATE.STANDARD"
    RATE_REDUCED = "RATE.REDUCED"
    RATE_NA_EXEMPT = "RATE.NA_EXEMPT"
    # EXM
    EXM_EXEMPT_SUPPLY = "EXM.EXEMPT_SUPPLY"
    EXM_NONE = "EXM.NONE"
    # RCH
    RCH_DOMESTIC_SUPPLIER = "RC.DOMESTIC.SUPPLIER_CHARGES"
    RCH_B2B_INTRA = "RC.B2B.INTRA_EU"           # reverse charge applies
    RCH_B2C_SUPPLIER = "RC.B2C.SUPPLIER_CHARGES"
    RCH_EXEMPT = "RC.EXEMPT.NO_CHARGE"


RULE_KEYS: frozenset[str] = frozenset(r.value for r in Rule)


# ---------------------------------------------------------------------------
# Lightweight input/intermediate dataclasses. The on-disk JSON is governed by
# src/schemas/*. These mirror those schemas for in-process use.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    line_id: str
    description: str
    amount: float
    true_category: Category  # ground-truth assigned by the generator (§2.1 CLS)


@dataclass(frozen=True)
class Case:
    case_id: str
    supplier_country: Jurisdiction
    customer_country: Jurisdiction
    customer_vat_registered: bool
    transaction_type: TxnType
    line_items: tuple[LineItem, ...]

    def __post_init__(self) -> None:
        # Grounding §1.3 internal-consistency invariant.
        if self.transaction_type is TxnType.B2B and not self.customer_vat_registered:
            raise ValueError("B2B requires customer_vat_registered=True")
        if self.transaction_type is TxnType.B2C and self.customer_vat_registered:
            raise ValueError("B2C requires customer_vat_registered=False")


@dataclass(frozen=True)
class StepRecord:
    """One subtask record (CLS/JUR/RAT/EXM/RCH). Mirrors src/schemas/<step>.schema.json."""
    subtask: str
    decision: dict
    support: dict
    rule_reference: str


# ===========================================================================
# THE FIVE SUBTASK RESOLVERS (§2.1)
# Each returns the oracle-correct decision for a line item / case.
# These are skeletons: signatures + precedence + the table lookups are fixed;
# fill the bodies against §2.1. Raise on any unbounded input.
# ===========================================================================


def resolve_cls(line: LineItem) -> StepRecord:
    """CLS — classification (§2.1).

    Oracle label = the generator-assigned true category. The oracle does no NLP.
    """
    category = line.true_category
    # TODO(impl): assemble decision/support per cls.schema.json
    return StepRecord(
        subtask="CLS",
        decision={"category": category.value},
        support={"line_id": line.line_id, "description": line.description},
        rule_reference=Rule.CLS_ASSIGNED.value,
    )


def resolve_jur(case: Case, category: Category) -> StepRecord:
    """JUR — jurisdiction / place of supply, simplified (§2.1).

    Domestic if supplier==customer. Otherwise:
      - goods/service B2B (registered, cross-border) -> place of supply = customer country
      - B2C cross-border -> place of supply = supplier country (bounded simplification)
    Returns jurisdiction (country whose regime applies) + the JurPath taken.
    """
    same_country = case.supplier_country == case.customer_country

    if same_country:
        jurisdiction = case.supplier_country
        path = JurPath.DOMESTIC
        rule = Rule.JUR_DOMESTIC
    elif case.transaction_type is TxnType.B2B and case.customer_vat_registered:
        # Both goods (intra-community) and general-rule services land on customer country.
        jurisdiction = case.customer_country
        path = JurPath.INTRA_COMMUNITY_B2B
        rule = Rule.JUR_INTRA_B2B
    else:  # B2C cross-border
        jurisdiction = case.supplier_country
        path = JurPath.B2C_CROSS_BORDER
        rule = Rule.JUR_B2C_CROSS

    # TODO(impl): if a mixed case makes goods-vs-service kind affect JUR, branch here
    #             deterministically and document it (grounding §2.2). For the bounded
    #             set, kind does not change the country selection above.
    return StepRecord(
        subtask="JUR",
        decision={"jurisdiction": jurisdiction.value, "jur_path": path.value},
        support={
            "supplier_country": case.supplier_country.value,
            "customer_country": case.customer_country.value,
            "transaction_type": case.transaction_type.value,
            "customer_vat_registered": case.customer_vat_registered,
            "category": category.value,
        },
        rule_reference=rule.value,
    )


def resolve_rat(jurisdiction: Jurisdiction, category: Category) -> StepRecord:
    """RAT — rate lookup (§2.1). Standard/reduced from RATE_TABLE; n/a for exempt."""
    band = CATEGORY_TABLE[category]["rate_band"]
    if band is None:  # EXEMPT_SUPPLY
        rate = None
        rule = Rule.RATE_NA_EXEMPT
    else:
        rate = RATE_TABLE[jurisdiction][band]
        rule = Rule.RATE_STANDARD if band == "standard" else Rule.RATE_REDUCED
    return StepRecord(
        subtask="RAT",
        decision={"rate_band": band, "rate": rate},
        support={"jurisdiction": jurisdiction.value, "category": category.value},
        rule_reference=rule.value,
    )


def resolve_exm(category: Category) -> StepRecord:
    """EXM — exemption check (§2.1). Only EXEMPT_SUPPLY is exempt; always exempt."""
    is_exempt = CATEGORY_TABLE[category]["exempt"]
    rule = Rule.EXM_EXEMPT_SUPPLY if is_exempt else Rule.EXM_NONE
    return StepRecord(
        subtask="EXM",
        decision={"exempt": is_exempt},
        support={"category": category.value},
        rule_reference=rule.value,
    )


def resolve_rch(
    case: Case,
    category: Category,
    jur_path: JurPath,
    rate: Optional[float],
    is_exempt: bool,
    line_amount: float,
) -> StepRecord:
    """RCH — reverse-charge / liable party synthesis (§2.1) with precedence.

    PRECEDENCE (grounding §2.1): EXEMPT > REVERSE_CHARGE > STANDARD_CHARGE.
    Resolves a single line item to exactly one Outcome.
    """
    if is_exempt:
        outcome = Outcome.EXEMPT
        liable_party = "none"
        reverse_charge = False
        vat_amount = 0.0
        non_charging_reason = "exempt"
        rule = Rule.RCH_EXEMPT
    elif jur_path is JurPath.INTRA_COMMUNITY_B2B:
        outcome = Outcome.REVERSE_CHARGE
        liable_party = "customer"
        reverse_charge = True
        vat_amount = 0.0  # supplier charges nothing; customer self-accounts
        non_charging_reason = "reverse_charge"
        rule = Rule.RCH_B2B_INTRA
    elif jur_path is JurPath.B2C_CROSS_BORDER:
        outcome = Outcome.STANDARD_CHARGE
        liable_party = "supplier"
        reverse_charge = False
        vat_amount = _vat_amount(line_amount, rate)
        non_charging_reason = None
        rule = Rule.RCH_B2C_SUPPLIER
    else:  # DOMESTIC
        outcome = Outcome.STANDARD_CHARGE
        liable_party = "supplier"
        reverse_charge = False
        vat_amount = _vat_amount(line_amount, rate)
        non_charging_reason = None
        rule = Rule.RCH_DOMESTIC_SUPPLIER

    return StepRecord(
        subtask="RCH",
        decision={
            "outcome": outcome.value,
            "reverse_charge": reverse_charge,
            "liable_party": liable_party,
            "vat_amount": vat_amount,
            "non_charging_reason": non_charging_reason,
        },
        support={
            "category": category.value,
            "jur_path": jur_path.value,
            "is_exempt": is_exempt,
            "rate": rate,
            "line_amount": line_amount,
        },
        rule_reference=rule.value,
    )


def _vat_amount(line_amount: float, rate: Optional[float]) -> float:
    """Exact VAT amount. NOTE: switch to decimal.Decimal in the real impl (§1.1)."""
    if rate is None:
        return 0.0
    return round(line_amount * rate, 2)


# ===========================================================================
# Convenience: resolve one line item end-to-end (used by labeler.py).
# Kept here so the precedence wiring lives next to the resolvers.
# ===========================================================================


@dataclass(frozen=True)
class LineDetermination:
    line_id: str
    cls: StepRecord
    rat: StepRecord
    exm: StepRecord
    rch: StepRecord


def resolve_line(case: Case, jur: StepRecord, line: LineItem) -> LineDetermination:
    """Compose CLS/RAT/EXM/RCH for one line item, given the case-level JUR record."""
    cls = resolve_cls(line)
    category = line.true_category
    jurisdiction = Jurisdiction(jur.decision["jurisdiction"])
    jur_path = JurPath(jur.decision["jur_path"])

    rat = resolve_rat(jurisdiction, category)
    exm = resolve_exm(category)
    rch = resolve_rch(
        case=case,
        category=category,
        jur_path=jur_path,
        rate=rat.decision["rate"],
        is_exempt=exm.decision["exempt"],
        line_amount=line.amount,
    )
    return LineDetermination(line_id=line.line_id, cls=cls, rat=rat, exm=exm, rch=rch)