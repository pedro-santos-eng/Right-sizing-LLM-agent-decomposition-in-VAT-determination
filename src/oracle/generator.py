"""generator.py — seeded synthetic VAT case generation (grounding §6, §7).

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md.

This is the ONLY module allowed to touch an RNG, and it must be fully
deterministic: one ``random.Random(seed)`` threaded through generation, no
global ``random`` calls, no wall-clock, no uuid, no set-iteration leaking into
output (grounding §6). Same seed in -> byte-identical cases out.

Dataset layout (grounding §7):
  - 40 evaluation cases, stratified 10 per scenario family.
  - 8 development cases, disjoint from eval by construction (distinct id range
    drawn from the same seeded stream).

Scenario families and the structural signature ``family_of`` uses to recover
them (priority: a case with >=2 distinct categories is "mixed" regardless of
party config; otherwise the party/transaction attributes decide):
  - "domestic"            : supplier_country == customer_country
  - "intra_community_b2b" : cross-border B2B (customer VAT-registered)
  - "b2c"                 : cross-border B2C
  - "mixed"               : >=2 distinct line-item categories
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from .rules import (
    Case,
    Category,
    Jurisdiction,
    LineItem,
    TxnType,
)

# Fixed-order sequences. Never iterate enums/sets into output; pick from these
# lists so the RNG draws are reproducible across machines (grounding §6).
JURISDICTIONS: tuple[Jurisdiction, ...] = (
    Jurisdiction.DE,
    Jurisdiction.FR,
    Jurisdiction.IE,
    Jurisdiction.ES,
)
NON_EXEMPT_CATEGORIES: tuple[Category, ...] = (
    Category.GEN_GOODS,
    Category.RED_GOODS,
    Category.GEN_SERVICE,
    Category.RED_SERVICE,
)
ALL_CATEGORIES: tuple[Category, ...] = NON_EXEMPT_CATEGORIES + (Category.EXEMPT_SUPPLY,)

FAMILY_ORDER: tuple[str, ...] = ("domestic", "intra_community_b2b", "b2c", "mixed")

EVAL_PER_FAMILY = 10
DEV_PER_FAMILY = 2


@dataclass(frozen=True)
class Dataset:
    """A generated dataset split into disjoint eval/dev case sets."""
    seed: int
    eval_cases: tuple[Case, ...]
    dev_cases: tuple[Case, ...]


# ---------------------------------------------------------------------------
# Family recovery (used by tests and stratification checks).
# ---------------------------------------------------------------------------


def family_of(case: Case) -> str:
    """Recover the scenario family of a case from its structure (grounding §7)."""
    distinct = {li.true_category for li in case.line_items}
    if len(distinct) >= 2:
        return "mixed"
    if case.supplier_country == case.customer_country:
        return "domestic"
    if case.transaction_type is TxnType.B2B:
        return "intra_community_b2b"
    return "b2c"


# ---------------------------------------------------------------------------
# Deterministic primitives. Every draw goes through the threaded Random.
# ---------------------------------------------------------------------------


def _amount(rng: random.Random) -> float:
    # Whole-euro amounts keep VAT arithmetic and JSON serialization clean.
    return float(rng.randint(50, 10_000))


def _two_distinct_countries(rng: random.Random) -> tuple[Jurisdiction, Jurisdiction]:
    supplier = rng.choice(JURISDICTIONS)
    customer = rng.choice(tuple(j for j in JURISDICTIONS if j != supplier))
    return supplier, customer


def _line(rng: random.Random, index: int, category: Category) -> LineItem:
    return LineItem(
        line_id=f"L{index + 1}",
        description=f"synthetic-{category.value}",
        amount=_amount(rng),
        true_category=category,
    )


def _distinct_categories(rng: random.Random, n: int) -> list[Category]:
    # Guarantee >=2 distinct (family 4 invariant): seed with two distinct picks.
    cats = list(rng.sample(ALL_CATEGORIES, 2))
    cats += [rng.choice(ALL_CATEGORIES) for _ in range(n - 2)]
    return cats


# ---------------------------------------------------------------------------
# Per-family case builders. Each builder consumes the RNG deterministically and
# guarantees the structural signature ``family_of`` recovers (single distinct
# category for the non-mixed families; >=2 for mixed).
# ---------------------------------------------------------------------------


def _domestic_case(rng: random.Random, case_id: str) -> Case:
    country = rng.choice(JURISDICTIONS)
    txn = rng.choice((TxnType.B2B, TxnType.B2C))
    registered = txn is TxnType.B2B
    category = rng.choice(ALL_CATEGORIES)
    return Case(
        case_id=case_id,
        supplier_country=country,
        customer_country=country,
        customer_vat_registered=registered,
        transaction_type=txn,
        line_items=(_line(rng, 0, category),),
    )


def _intra_case(rng: random.Random, case_id: str) -> Case:
    supplier, customer = _two_distinct_countries(rng)
    # Non-exempt so the reverse-charge path is actually exercised (family 2).
    category = rng.choice(NON_EXEMPT_CATEGORIES)
    return Case(
        case_id=case_id,
        supplier_country=supplier,
        customer_country=customer,
        customer_vat_registered=True,
        transaction_type=TxnType.B2B,
        line_items=(_line(rng, 0, category),),
    )


def _b2c_case(rng: random.Random, case_id: str) -> Case:
    supplier, customer = _two_distinct_countries(rng)
    category = rng.choice(ALL_CATEGORIES)
    return Case(
        case_id=case_id,
        supplier_country=supplier,
        customer_country=customer,
        customer_vat_registered=False,
        transaction_type=TxnType.B2C,
        line_items=(_line(rng, 0, category),),
    )


def _mixed_case(rng: random.Random, case_id: str) -> Case:
    path = rng.choice(("domestic", "intra", "b2c"))
    if path == "domestic":
        country = rng.choice(JURISDICTIONS)
        supplier = customer = country
        txn = rng.choice((TxnType.B2B, TxnType.B2C))
        registered = txn is TxnType.B2B
    elif path == "intra":
        supplier, customer = _two_distinct_countries(rng)
        txn = TxnType.B2B
        registered = True
    else:  # b2c
        supplier, customer = _two_distinct_countries(rng)
        txn = TxnType.B2C
        registered = False

    n = rng.choice((2, 3))
    categories = _distinct_categories(rng, n)
    lines = tuple(_line(rng, i, cat) for i, cat in enumerate(categories))
    return Case(
        case_id=case_id,
        supplier_country=supplier,
        customer_country=customer,
        customer_vat_registered=registered,
        transaction_type=txn,
        line_items=lines,
    )


_FAMILY_BUILDERS = {
    "domestic": _domestic_case,
    "intra_community_b2b": _intra_case,
    "b2c": _b2c_case,
    "mixed": _mixed_case,
}


# ---------------------------------------------------------------------------
# Dataset generation.
# ---------------------------------------------------------------------------


def generate_dataset(seed: int) -> Dataset:
    """Generate the full stratified dataset deterministically from ``seed``."""
    rng = random.Random(seed)

    eval_cases: list[Case] = []
    for family in FAMILY_ORDER:
        builder = _FAMILY_BUILDERS[family]
        for _ in range(EVAL_PER_FAMILY):
            case_id = f"eval_{len(eval_cases) + 1:03d}"
            eval_cases.append(builder(rng, case_id))

    dev_cases: list[Case] = []
    for family in FAMILY_ORDER:
        builder = _FAMILY_BUILDERS[family]
        for _ in range(DEV_PER_FAMILY):
            case_id = f"dev_{len(dev_cases) + 1:03d}"
            dev_cases.append(builder(rng, case_id))

    return Dataset(seed=seed, eval_cases=tuple(eval_cases), dev_cases=tuple(dev_cases))


# ---------------------------------------------------------------------------
# Canonical serialization (stable, byte-identical for identical content).
# ---------------------------------------------------------------------------


def case_to_dict(case: Case) -> dict:
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
                "true_category": li.true_category.value,
            }
            for li in case.line_items
        ],
    }


def dataset_to_dict(dataset: Dataset) -> dict:
    return {
        "seed": dataset.seed,
        "eval_cases": [case_to_dict(c) for c in dataset.eval_cases],
        "dev_cases": [case_to_dict(c) for c in dataset.dev_cases],
    }


def to_canonical_json(dataset: Dataset) -> str:
    """Stable JSON: sorted keys, no whitespace drift, no set/hash-order leak."""
    return json.dumps(
        dataset_to_dict(dataset),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
