"""generate_injection_plan.py — offline generator for the Layer-3 injection plan.

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_3_INJECTION.md v1.0 (§1–§5, §11).

Injection content is PRECOMPUTED OFFLINE [DECISION 1], not generated at runtime.
This script MAY import ``src.oracle.labeler`` (guaranteeing oracle-incorrectness
needs the oracle label at generation time); it is imported by NO ``src/harness/``
module, so runtime label-isolation is preserved by construction (§1 rationale).

It writes ``data/injection_plan.json`` — the sole write path (§9: the plan file is
never hand-edited; regeneration is the only write path). Determinism is testable
as byte-equality of regeneration.

Plan shape (§1):
  {injection_seed, generator_version, tau_by_case, hallucinated_record_by_case,
   outage_cases, content_sha256}

Per-record hallucination recipe is §4; sampling is §2; outage blocks are §5. For
EVERY hallucinated record the generator asserts BOTH:
  (a) validate_record passes (record-level / input-blind — §4 "input-blind by
      design", DECISION 3), and
  (b) the record's decision differs from the oracle label.
For RAT it additionally asserts the flipped-band rate equals the bounded table
entry for the decided jurisdiction (§4 "RAT-vs-table", made explicit rather than
left to validate_record's deferral).

Run: ``python -m scripts.generate_injection_plan`` (from repo root).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.harness.surface import SUBTASKS
from src.harness.validation import validate_record
from src.oracle import generator as oracle_generator
from src.oracle import labeler, validator
from src.oracle.rules import (
    RATE_TABLE,
    Category,
    Jurisdiction,
    Rule,
)

# §2 sampling constants (all ratified DECISION 2).
INJECTION_SEED = 20260801
GENERATOR_VERSION = "L3-injection-v1"
DATASET_SEED = 42

_REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = _REPO_ROOT / "data" / "injection_plan.json"

# Fixed vocabulary order for the CLS "next category" perturbation (§4). Matches
# surface/tools _CATEGORY_ORDER.
_CATEGORY_ORDER: tuple[str, ...] = (
    Category.GEN_GOODS.value,
    Category.RED_GOODS.value,
    Category.GEN_SERVICE.value,
    Category.RED_SERVICE.value,
    Category.EXEMPT_SUPPLY.value,
)

# Fixed outcome order for the RCH "next outcome" perturbation (§4).
_OUTCOME_ORDER: tuple[str, ...] = ("standard_charge", "reverse_charge", "exempt")

_BAND_RULE = {"standard": Rule.RATE_STANDARD.value, "reduced": Rule.RATE_REDUCED.value}


# ---------------------------------------------------------------------------
# Deterministic sampling (§2, §5). A single random.Random(INJECTION_SEED) is
# threaded: first the per-case τ draws in case-id order, then the 8 outage-block
# draws in block order. The order is fixed and documented so regeneration is
# byte-identical.
# ---------------------------------------------------------------------------


def _sample(eval_cases) -> tuple[dict[str, str], list[str]]:
    import random

    rng = random.Random(INJECTION_SEED)
    tau_by_case: dict[str, str] = {}
    for case in eval_cases:  # eval_001..eval_040 in fixed order
        tau_by_case[case.case_id] = rng.choice(list(SUBTASKS))  # uniform over T

    # §5: 40 cases = 8 blocks of 5 consecutive case-ids (4 families × 2 blocks),
    # one outage case per block → 8 affected cases (20%), m = 1.
    outage_cases: list[str] = []
    for start in range(0, len(eval_cases), 5):
        block = [c.case_id for c in eval_cases[start:start + 5]]
        outage_cases.append(rng.choice(block))
    return tau_by_case, outage_cases


# ---------------------------------------------------------------------------
# §4 per-subtask perturbations. Each returns a full emitted-record dict
# {subtask, decision, support, rule_reference} for the case's FIRST line
# (lowest line_id = line_items[0]; §2 first-line targeting).
# ---------------------------------------------------------------------------


def _next_different(seq: tuple[str, ...], value: str) -> str:
    """The next element after ``value`` in cyclic order (guaranteed != value)."""
    i = seq.index(value)
    return seq[(i + 1) % len(seq)]


def _hallucinate_cls(case, oracle_line, oracle_jur) -> dict:
    oracle_cat = oracle_line["cls"]["decision"]["category"]
    wrong = _next_different(_CATEGORY_ORDER, oracle_cat)
    li = case.line_items[0]
    return {
        "subtask": "CLS",
        "decision": {"category": wrong},
        "support": {"line_id": li.line_id, "description": li.description},
        "rule_reference": Rule.CLS_ASSIGNED.value,
    }


def _hallucinate_jur(case, oracle_line, oracle_jur) -> dict:
    path = oracle_jur["decision"]["jur_path"]
    supplier = case.supplier_country.value
    customer = case.customer_country.value
    category = oracle_line["cls"]["decision"]["category"]
    # domestic ↔ intra_community_b2b with jurisdiction switched supplier↔customer;
    # b2c_cross_border → domestic-at-supplier (§4).
    if path == "domestic":
        new_path, new_jur, rule = "intra_community_b2b", customer, Rule.JUR_INTRA_B2B.value
    elif path == "intra_community_b2b":
        new_path, new_jur, rule = "domestic", supplier, Rule.JUR_DOMESTIC.value
    else:  # b2c_cross_border
        new_path, new_jur, rule = "domestic", supplier, Rule.JUR_DOMESTIC.value
    return {
        "subtask": "JUR",
        "decision": {"jurisdiction": new_jur, "jur_path": new_path},
        "support": {
            "supplier_country": supplier,
            "customer_country": customer,
            "transaction_type": case.transaction_type.value,
            "customer_vat_registered": case.customer_vat_registered,
            "category": category,
        },
        "rule_reference": rule,
    }


def _hallucinate_rat(case, oracle_line, oracle_jur) -> dict:
    decided_jur = oracle_jur["decision"]["jurisdiction"]
    oracle_band = oracle_line["rat"]["decision"]["rate_band"]
    category = oracle_line["cls"]["decision"]["category"]
    # flip band standard ↔ reduced; exempt-slot oracle → standard (§4). Use the
    # flipped band's table rate for the decided jurisdiction.
    flipped = "reduced" if oracle_band == "standard" else "standard"
    rate = RATE_TABLE[Jurisdiction(decided_jur)][flipped]
    return {
        "subtask": "RAT",
        "decision": {"rate_band": flipped, "rate": rate},
        "support": {"jurisdiction": decided_jur, "category": category},
        "rule_reference": _BAND_RULE[flipped],
    }


def _hallucinate_exm(case, oracle_line, oracle_jur) -> dict:
    oracle_exempt = oracle_line["exm"]["decision"]["exempt"]
    wrong = not oracle_exempt
    category = oracle_line["cls"]["decision"]["category"]
    rule = Rule.EXM_EXEMPT_SUPPLY.value if wrong else Rule.EXM_NONE.value
    return {
        "subtask": "EXM",
        "decision": {"exempt": wrong},
        "support": {"category": category},
        "rule_reference": rule,
    }


def _hallucinate_rch(case, oracle_line, oracle_jur) -> dict:
    oracle_outcome = oracle_line["rch"]["decision"]["outcome"]
    wrong = _next_different(_OUTCOME_ORDER, oracle_outcome)
    decided_jur = oracle_jur["decision"]["jurisdiction"]
    line_amount = case.line_items[0].amount
    category = oracle_line["cls"]["decision"]["category"]

    if wrong == "reverse_charge":
        decision = {
            "outcome": "reverse_charge",
            "reverse_charge": True,
            "liable_party": "customer",
            "vat_amount": 0.0,
            "non_charging_reason": "reverse_charge",
        }
        rule = Rule.RCH_B2B_INTRA.value
    elif wrong == "exempt":
        decision = {
            "outcome": "exempt",
            "reverse_charge": False,
            "liable_party": "none",   # amended §2.1: exempt → liable_party none
            "vat_amount": 0.0,
            "non_charging_reason": "exempt",
        }
        rule = Rule.RCH_EXEMPT.value
    else:  # standard_charge — arithmetically consistent vat on the line amount
        oracle_rate = oracle_line["rat"]["decision"]["rate"]
        rate = oracle_rate if oracle_rate is not None else RATE_TABLE[Jurisdiction(decided_jur)]["standard"]
        decision = {
            "outcome": "standard_charge",
            "reverse_charge": False,
            "liable_party": "supplier",
            "vat_amount": round(line_amount * rate, 2),
            "non_charging_reason": None,
        }
        # standard_charge has two canonical keys (domestic / b2c); the domestic
        # supplier-charges key is chosen (§4 "the outcome's key"; either passes
        # citation-presence — flagged as a bounded pick).
        rule = Rule.RCH_DOMESTIC_SUPPLIER.value

    return {
        "subtask": "RCH",
        "decision": decision,
        "support": {"category": category, "jur_path": oracle_jur["decision"]["jur_path"]},
        "rule_reference": rule,
    }


_HALLUCINATORS = {
    "CLS": _hallucinate_cls,
    "JUR": _hallucinate_jur,
    "RAT": _hallucinate_rat,
    "EXM": _hallucinate_exm,
    "RCH": _hallucinate_rch,
}


def _oracle_record(tau: str, oracle_line, oracle_jur) -> dict:
    if tau == "JUR":
        return oracle_jur
    return oracle_line[tau.lower()]


# ---------------------------------------------------------------------------
# Plan assembly + the two (three, for RAT) offline assertions (§4, §10 gate).
# ---------------------------------------------------------------------------


def build_plan() -> dict:
    ds = oracle_generator.generate_dataset(seed=DATASET_SEED)
    eval_cases = list(ds.eval_cases)
    tau_by_case, outage_cases = _sample(eval_cases)

    hallucinated: dict[str, dict] = {}
    for case in eval_cases:
        tau = tau_by_case[case.case_id]
        emitted = validator.trace_to_emitted(labeler.label_case(case))
        oracle_jur = emitted["jur"]
        oracle_line = emitted["lines"][0]  # first line (lowest line_id, §2)

        record = _HALLUCINATORS[tau](case, oracle_line, oracle_jur)

        # (a) record-level / input-blind validation passes (§4, DECISION 3).
        verdict = validate_record(tau, record, accepted={})
        assert verdict.accepted, (
            f"{case.case_id} τ={tau}: injected record fails validate_record: "
            f"{verdict.failed_checks}"
        )
        # (b) the decision differs from the oracle label (oracle-incorrect, §4).
        assert record["decision"] != _oracle_record(tau, oracle_line, oracle_jur)["decision"], (
            f"{case.case_id} τ={tau}: injected decision equals oracle"
        )
        # RAT-vs-table made explicit (§4): the flipped rate is the real table entry.
        if tau == "RAT":
            j = Jurisdiction(oracle_jur["decision"]["jurisdiction"])
            band = record["decision"]["rate_band"]
            assert record["decision"]["rate"] == RATE_TABLE[j][band]

        hallucinated[case.case_id] = record

    plan = {
        "injection_seed": INJECTION_SEED,
        "generator_version": GENERATOR_VERSION,
        "tau_by_case": tau_by_case,
        "hallucinated_record_by_case": hallucinated,
        "outage_cases": outage_cases,
    }
    plan["content_sha256"] = _content_sha256(plan)
    return plan


def _content_sha256(plan_without_hash: dict) -> str:
    """SHA-256 over the canonical content, EXCLUDING content_sha256 (§1, §7)."""
    body = {k: v for k, v in plan_without_hash.items() if k != "content_sha256"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_plan(plan: dict) -> str:
    """Canonical, deterministic serialization (the committed file's exact bytes)."""
    return json.dumps(plan, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


def write_plan() -> Path:
    plan = build_plan()
    PLAN_PATH.write_text(serialize_plan(plan), encoding="utf-8")
    return PLAN_PATH


def main() -> None:
    plan = build_plan()
    PLAN_PATH.write_text(serialize_plan(plan), encoding="utf-8")
    # Summary (no oracle labels printed — only τ distribution + outage ids).
    from collections import Counter

    dist = Counter(plan["tau_by_case"].values())
    print(f"wrote {PLAN_PATH.relative_to(_REPO_ROOT)}")
    print(f"  injection_seed:  {plan['injection_seed']}")
    print(f"  generator:       {plan['generator_version']}")
    print("  tau distribution:  " + ", ".join(f"{t}={dist.get(t, 0)}" for t in SUBTASKS))
    print(f"  outage_cases:    {plan['outage_cases']}")
    print(f"  content_sha256:  {plan['content_sha256']}")


if __name__ == "__main__":
    main()
