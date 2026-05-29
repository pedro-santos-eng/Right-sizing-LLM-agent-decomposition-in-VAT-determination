"""
Pytest suite for the VAT oracle (Part 1).

Each test maps to a checkbox in the Part-1 readiness gate (grounding §9).
All five oracle modules (rules, generator, labeler, validator, scorer) are
implemented, so every test runs unconditionally.

Run:  pytest -q
"""

from __future__ import annotations

import pytest

from src.oracle import rules
from src.oracle.rules import (
    Case,
    Category,
    Jurisdiction,
    JurPath,
    LineItem,
    Outcome,
    RATE_TABLE,
    Rule,
    RULE_KEYS,
    TxnType,
    resolve_jur,
    resolve_line,
)


def _pick_swap_case(ds, labeler):
    """Find an eval case suitable for the valid-but-wrong injection tests.

    Criteria:
      - first line lands on a banded standard rate (so swapping to the SAME
        jurisdiction's reduced rate is a different-but-table-valid entry);
      - oracle jurisdiction is not 'ES' (so the earliest-error test can swap
        the JUR to ES and observe a real corruption).
    Returns (case, trace, jurisdiction_code).
    """
    for c in ds.eval_cases:
        trace = labeler.label_case(c)
        rat = trace.lines[0].rat.decision
        jur_code = trace.jur.decision["jurisdiction"]
        if rat["rate_band"] == "standard" and jur_code != "ES":
            return c, trace, jur_code
    raise AssertionError("no eval case meets swap-test criteria (standard band, non-ES)")


# ---------------------------------------------------------------------------
# Helpers: build small hand-specified cases.
# ---------------------------------------------------------------------------


def _line(line_id: str, category: Category, amount: float = 100.0) -> LineItem:
    return LineItem(
        line_id=line_id,
        description=f"synthetic-{category.value}",
        amount=amount,
        true_category=category,
    )


def _case(
    supplier: Jurisdiction,
    customer: Jurisdiction,
    txn: TxnType,
    registered: bool,
    categories: list[Category],
    case_id: str = "t_case",
) -> Case:
    lines = tuple(_line(f"L{i+1}", c) for i, c in enumerate(categories))
    return Case(
        case_id=case_id,
        supplier_country=supplier,
        customer_country=customer,
        customer_vat_registered=registered,
        transaction_type=txn,
        line_items=lines,
    )


# ===========================================================================
# test_rules.py  ->  gate: "every final label recomputable deterministically",
#                          "precedence holds", "each family path reachable"
# ===========================================================================


class TestRules:
    def test_case_invariant_b2b_requires_registered(self):
        with pytest.raises(ValueError):
            _case(Jurisdiction.DE, Jurisdiction.FR, TxnType.B2B, False, [Category.GEN_GOODS])

    def test_case_invariant_b2c_requires_unregistered(self):
        with pytest.raises(ValueError):
            _case(Jurisdiction.DE, Jurisdiction.DE, TxnType.B2C, True, [Category.GEN_GOODS])

    def test_domestic_standard_charge(self):
        case = _case(Jurisdiction.DE, Jurisdiction.DE, TxnType.B2C, False, [Category.GEN_GOODS])
        jur = resolve_jur(case, Category.GEN_GOODS)
        assert jur.decision["jur_path"] == JurPath.DOMESTIC.value
        assert jur.decision["jurisdiction"] == "DE"
        det = resolve_line(case, jur, case.line_items[0])
        assert det.rch.decision["outcome"] == Outcome.STANDARD_CHARGE.value
        assert det.rch.decision["reverse_charge"] is False
        assert det.rch.decision["liable_party"] == "supplier"
        # DE standard rate 19% on 100 -> 19.0
        assert det.rch.decision["vat_amount"] == pytest.approx(19.0)

    def test_intra_community_b2b_reverse_charge(self):
        case = _case(Jurisdiction.DE, Jurisdiction.FR, TxnType.B2B, True, [Category.GEN_GOODS])
        jur = resolve_jur(case, Category.GEN_GOODS)
        assert jur.decision["jur_path"] == JurPath.INTRA_COMMUNITY_B2B.value
        # place of supply = customer country
        assert jur.decision["jurisdiction"] == "FR"
        det = resolve_line(case, jur, case.line_items[0])
        assert det.rch.decision["reverse_charge"] is True
        assert det.rch.decision["liable_party"] == "customer"
        assert det.rch.decision["vat_amount"] == pytest.approx(0.0)
        assert det.rch.decision["non_charging_reason"] == "reverse_charge"
        assert det.rch.rule_reference == Rule.RCH_B2B_INTRA.value

    def test_b2c_cross_border_supplier_charges(self):
        case = _case(Jurisdiction.DE, Jurisdiction.FR, TxnType.B2C, False, [Category.RED_GOODS])
        jur = resolve_jur(case, Category.RED_GOODS)
        assert jur.decision["jur_path"] == JurPath.B2C_CROSS_BORDER.value
        # bounded simplification: place of supply = supplier country
        assert jur.decision["jurisdiction"] == "DE"
        det = resolve_line(case, jur, case.line_items[0])
        assert det.rch.decision["outcome"] == Outcome.STANDARD_CHARGE.value
        # DE reduced rate 7% on 100 -> 7.0
        assert det.rat.decision["rate"] == pytest.approx(0.07)
        assert det.rch.decision["vat_amount"] == pytest.approx(7.0)

    def test_precedence_exempt_dominates_reverse_charge(self):
        # Intra-community B2B that WOULD reverse-charge, but line is exempt.
        case = _case(Jurisdiction.DE, Jurisdiction.FR, TxnType.B2B, True, [Category.EXEMPT_SUPPLY])
        jur = resolve_jur(case, Category.EXEMPT_SUPPLY)
        assert jur.decision["jur_path"] == JurPath.INTRA_COMMUNITY_B2B.value
        det = resolve_line(case, jur, case.line_items[0])
        # exemption must dominate the reverse-charge path
        assert det.exm.decision["exempt"] is True
        assert det.rch.decision["outcome"] == Outcome.EXEMPT.value
        assert det.rch.decision["reverse_charge"] is False
        assert det.rch.decision["non_charging_reason"] == "exempt"
        assert det.rch.rule_reference == Rule.RCH_EXEMPT.value

    def test_rate_na_for_exempt(self):
        det_rat = rules.resolve_rat(Jurisdiction.IE, Category.EXEMPT_SUPPLY)
        assert det_rat.decision["rate"] is None
        assert det_rat.rule_reference == Rule.RATE_NA_EXEMPT.value

    def test_reduced_vs_standard_band_selection(self):
        std = rules.resolve_rat(Jurisdiction.ES, Category.GEN_SERVICE)
        red = rules.resolve_rat(Jurisdiction.ES, Category.RED_SERVICE)
        assert std.decision["rate"] == pytest.approx(0.21)
        assert red.decision["rate"] == pytest.approx(0.10)

    def test_all_rule_references_in_closed_set(self):
        # Every resolver must cite a key from the closed RULE_KEYS set.
        case = _case(Jurisdiction.DE, Jurisdiction.FR, TxnType.B2B, True, [Category.GEN_SERVICE])
        jur = resolve_jur(case, Category.GEN_SERVICE)
        det = resolve_line(case, jur, case.line_items[0])
        for rec in (det.cls, jur, det.rat, det.exm, det.rch):
            assert rec.rule_reference in RULE_KEYS

    def test_mixed_invoice_lines_resolve_independently(self):
        # Family 4: one case, two categories, different outcomes within the case.
        case = _case(
            Jurisdiction.DE, Jurisdiction.FR, TxnType.B2B, True,
            [Category.GEN_GOODS, Category.EXEMPT_SUPPLY],
        )
        jur_goods = resolve_jur(case, Category.GEN_GOODS)
        d0 = resolve_line(case, jur_goods, case.line_items[0])
        d1 = resolve_line(case, jur_goods, case.line_items[1])
        assert d0.rch.decision["outcome"] == Outcome.REVERSE_CHARGE.value
        assert d1.rch.decision["outcome"] == Outcome.EXEMPT.value


# ===========================================================================
# test_determinism.py  ->  gate: "same seed produces identical cases"
# ===========================================================================


class TestDeterminism:
    def test_same_seed_identical_cases(self):
        from src.oracle import generator  # noqa
        a = generator.generate_dataset(seed=12345)
        b = generator.generate_dataset(seed=12345)
        assert generator.to_canonical_json(a) == generator.to_canonical_json(b)

    def test_different_seed_differs(self):
        from src.oracle import generator  # noqa
        a = generator.generate_dataset(seed=1)
        b = generator.generate_dataset(seed=2)
        assert generator.to_canonical_json(a) != generator.to_canonical_json(b)

    def test_no_wallclock_or_uuid_leak(self):
        # Two generations a moment apart must be identical (no time/uuid in content).
        from src.oracle import generator  # noqa
        a = generator.generate_dataset(seed=7)
        b = generator.generate_dataset(seed=7)
        assert a == b


# ===========================================================================
# test_dataset.py  ->  gate: "40 eval + 8 dev, disjoint, 10 per family,
#                             complete labels, family paths exercised"
# ===========================================================================


class TestDataset:
    def test_split_sizes(self):
        from src.oracle import generator  # noqa
        ds = generator.generate_dataset(seed=42)
        assert len(ds.eval_cases) == 40
        assert len(ds.dev_cases) == 8

    def test_eval_dev_disjoint(self):
        from src.oracle import generator  # noqa
        ds = generator.generate_dataset(seed=42)
        eval_ids = {c.case_id for c in ds.eval_cases}
        dev_ids = {c.case_id for c in ds.dev_cases}
        assert eval_ids.isdisjoint(dev_ids)

    def test_ten_per_family(self):
        from src.oracle import generator  # noqa
        ds = generator.generate_dataset(seed=42)
        counts: dict[str, int] = {}
        for c in ds.eval_cases:
            fam = generator.family_of(c)
            counts[fam] = counts.get(fam, 0) + 1
        assert sorted(counts.values()) == [10, 10, 10, 10]
        assert len(counts) == 4

    def test_every_case_fully_labeled(self):
        from src.oracle import generator, labeler  # noqa
        ds = generator.generate_dataset(seed=42)
        for c in list(ds.eval_cases) + list(ds.dev_cases):
            trace = labeler.label_case(c)
            assert trace.jur is not None
            for line_det in trace.lines:
                for step in ("cls", "rat", "exm", "rch"):
                    assert getattr(line_det, step) is not None

    def test_family2_exercises_reverse_charge(self):
        from src.oracle import generator, labeler  # noqa
        ds = generator.generate_dataset(seed=42)
        fam2 = [c for c in ds.eval_cases if generator.family_of(c) == "intra_community_b2b"]
        for c in fam2:
            trace = labeler.label_case(c)
            assert any(ld.rch.decision["reverse_charge"] for ld in trace.lines)

    def test_family4_has_multiple_categories(self):
        from src.oracle import generator  # noqa
        ds = generator.generate_dataset(seed=42)
        fam4 = [c for c in ds.eval_cases if generator.family_of(c) == "mixed"]
        for c in fam4:
            cats = {li.true_category for li in c.line_items}
            assert len(cats) >= 2


# ===========================================================================
# test_validator.py  ->  gate: "malformed traces fail validation;
#                               schema-valid-but-wrong may still pass V"
# ===========================================================================


class TestValidator:
    def test_malformed_trace_fails(self):
        from src.oracle import validator  # noqa
        broken = {"final": {}, "lines": [{"cls": {"decision": {}}}]}  # missing fields/citations
        result = validator.validate_trace(broken)
        assert result.ok is False
        assert result.failed_checks  # non-empty

    def test_missing_citation_fails(self):
        from src.oracle import generator, labeler, validator  # noqa
        ds = generator.generate_dataset(seed=42)
        trace = labeler.label_case(ds.eval_cases[0])
        emitted = validator.trace_to_emitted(trace)
        # strip a rule_reference -> citation-presence check must fail
        emitted["lines"][0]["rch"]["rule_reference"] = None
        assert validator.validate_trace(emitted).ok is False

    def test_oracle_trace_passes_validation(self):
        from src.oracle import generator, labeler, validator  # noqa
        ds = generator.generate_dataset(seed=42)
        for c in ds.eval_cases:
            trace = labeler.label_case(c)
            emitted = validator.trace_to_emitted(trace)
            assert validator.validate_trace(emitted).ok is True

    def test_schema_valid_but_wrong_can_pass_validator(self):
        # The hallucinated-output injection produces a schema-conforming, citation-consistent,
        # oracle-WRONG record. Validator may pass it; scorer must catch it. (grounding §4, §6.4)
        from src.oracle import generator, labeler, validator  # noqa
        ds = generator.generate_dataset(seed=42)
        c, trace, jur_code = _pick_swap_case(ds, labeler)
        emitted = validator.trace_to_emitted(trace)
        # swap rate to another VALID table entry (same jurisdiction's reduced band):
        reduced_rate = RATE_TABLE[Jurisdiction(jur_code)]["reduced"]
        emitted["lines"][0]["rat"]["decision"]["rate"] = reduced_rate
        emitted["lines"][0]["rat"]["decision"]["rate_band"] = "reduced"
        emitted["lines"][0]["rat"]["rule_reference"] = Rule.RATE_REDUCED.value
        # validator only checks well-formedness/consistency, not oracle-correctness
        assert validator.validate_trace(emitted).ok is True


# ===========================================================================
# test_scorer.py  ->  gate: "schema-valid-but-wrong scored wrong;
#                            earliest-error attribution correct"
# ===========================================================================


class TestScorer:
    def test_correct_trace_scores_correct(self):
        from src.oracle import generator, labeler, validator, scorer  # noqa
        ds = generator.generate_dataset(seed=42)
        c = ds.eval_cases[0]
        trace = labeler.label_case(c)
        emitted = validator.trace_to_emitted(trace)
        score = scorer.score(emitted, labels=trace)
        assert score.final_answer_accuracy is True
        assert score.earliest_error_subtask is None
        assert all(score.step_accuracy.values())

    def test_schema_valid_but_wrong_scores_wrong(self):
        from src.oracle import generator, labeler, validator, scorer  # noqa
        ds = generator.generate_dataset(seed=42)
        c, trace, jur_code = _pick_swap_case(ds, labeler)
        emitted = validator.trace_to_emitted(trace)
        # inject a valid-but-wrong RAT (as in TestValidator) and confirm scorer flags it
        reduced_rate = RATE_TABLE[Jurisdiction(jur_code)]["reduced"]
        emitted["lines"][0]["rat"]["decision"]["rate"] = reduced_rate
        emitted["lines"][0]["rat"]["decision"]["rate_band"] = "reduced"
        emitted["lines"][0]["rat"]["rule_reference"] = Rule.RATE_REDUCED.value
        score = scorer.score(emitted, labels=trace)
        assert score.final_answer_accuracy is False

    def test_earliest_error_attribution(self):
        from src.oracle import generator, labeler, validator, scorer  # noqa
        ds = generator.generate_dataset(seed=42)
        c, trace, jur_code = _pick_swap_case(ds, labeler)
        emitted = validator.trace_to_emitted(trace)
        # corrupt JUR (swap to a different jurisdiction with a domestic path);
        # earliest failing subtask in fixed order should be JUR
        emitted["jur"]["decision"]["jurisdiction"] = "ES"
        emitted["jur"]["decision"]["jur_path"] = JurPath.DOMESTIC.value
        score = scorer.score(emitted, labels=trace)
        assert score.earliest_error_subtask == "JUR"

    def test_terminal_failure_scored_incorrect(self):
        from src.oracle import scorer  # noqa
        score = scorer.score_terminal_failure(case_id="eval_001")
        assert score.final_answer_accuracy is False
        assert score.trace_consistent is False