"""test_validation_incremental.py — incremental <-> full-trace equivalence
(grounding §7.1, §10).

Gate box: incremental validate_record, evaluated over SUBTASKS in dependency
order, yields all-accept IFF validator.validate_trace passes on the assembled
trace. Verified on (a) all 48 frozen oracle traces and (b) >=40 single-field
mutations spanning all four check families.
"""

from __future__ import annotations

import copy

import pytest

from src.harness import validation
from src.harness.validation import (
    assembly_gate,
    validate_record,
    verdicts_in_dependency_order,
)
from src.oracle import generator, labeler, validator
from src.oracle.rules import Rule


def _emitted_traces():
    ds = generator.generate_dataset(seed=42)
    cases = list(ds.eval_cases) + list(ds.dev_cases)
    return [validator.trace_to_emitted(labeler.label_case(c)) for c in cases]


ALL_TRACES = _emitted_traces()
EVAL_TRACES = ALL_TRACES[:40]


# ---------------------------------------------------------------------------
# (a) the 48 oracle traces: all-accept AND validate_trace pass.
# ---------------------------------------------------------------------------


class TestOracleTracesAllAccept:
    def test_forty_eight_traces(self):
        assert len(ALL_TRACES) == 48

    def test_all_accept_and_validate_trace_pass(self):
        for emitted in ALL_TRACES:
            all_accept, verdicts = verdicts_in_dependency_order(emitted)
            full = validator.validate_trace(emitted).ok
            assert full is True
            assert all_accept is True, [v for v in verdicts if not v.accepted]

    def test_assembly_gate_delegates_to_validate_trace(self):
        emitted = ALL_TRACES[0]
        assert assembly_gate(emitted).ok == validator.validate_trace(emitted).ok is True


# ---------------------------------------------------------------------------
# (b) single-field mutations spanning the four check families. For each
# mutation, per-record all-accept must equal validate_trace.ok (biconditional).
# ---------------------------------------------------------------------------


def _m_schema_category(t):
    t["lines"][0]["cls"]["decision"]["category"] = "NOT_A_CATEGORY"
    return "schema"


def _m_schema_jur_path(t):
    t["jur"]["decision"]["jur_path"] = "weird_path"
    return "schema"


def _m_required_drop_rate(t):
    t["lines"][0]["rat"]["decision"].pop("rate", None)
    return "required"


def _m_citation_bad_rch(t):
    t["lines"][0]["rch"]["rule_reference"] = "FAKE.KEY"
    return "citation"


def _m_citation_bad_jur(t):
    t["jur"]["rule_reference"] = "NOPE"
    return "citation"


def _m_consistency_wrong_rate(t):
    t["lines"][0]["rat"]["decision"]["rate"] = 0.999
    return "consistency"


def _m_consistency_exm_exempt(t):
    t["lines"][0]["exm"]["decision"]["exempt"] = True
    t["lines"][0]["exm"]["rule_reference"] = Rule.EXM_EXEMPT_SUPPLY.value
    return "consistency"


def _m_consistency_rch_exempt(t):
    t["lines"][0]["rch"]["decision"]["outcome"] = "exempt"
    t["lines"][0]["rch"]["rule_reference"] = Rule.RCH_EXEMPT.value
    return "consistency"


def _m_consistency_rch_reverse(t):
    t["lines"][0]["rch"]["decision"]["reverse_charge"] = True
    t["lines"][0]["rch"]["rule_reference"] = Rule.RCH_B2B_INTRA.value
    return "consistency"


_MUTATORS = [
    _m_schema_category,
    _m_schema_jur_path,
    _m_required_drop_rate,
    _m_citation_bad_rch,
    _m_citation_bad_jur,
    _m_consistency_wrong_rate,
    _m_consistency_exm_exempt,
    _m_consistency_rch_exempt,
    _m_consistency_rch_reverse,
]


def _candidates():
    out = []
    for base in EVAL_TRACES:
        for mutate in _MUTATORS:
            t = copy.deepcopy(base)
            family = mutate(t)
            out.append((family, t))
    return out


class TestEquivalenceUnderMutation:
    def test_biconditional_holds_on_every_candidate(self):
        for family, t in _candidates():
            all_accept, _ = verdicts_in_dependency_order(t)
            full_ok = validator.validate_trace(t).ok
            assert all_accept == full_ok, (family, validator.validate_trace(t).failed_checks)

    def test_corpus_spans_four_families_with_forty_plus_breakers(self):
        breakers_by_family: dict[str, int] = {}
        for family, t in _candidates():
            if not validator.validate_trace(t).ok:  # this mutation broke validation
                # and per-record must catch it (biconditional, both False)
                all_accept, _ = verdicts_in_dependency_order(t)
                assert all_accept is False
                breakers_by_family[family] = breakers_by_family.get(family, 0) + 1
        assert set(breakers_by_family) == {"schema", "required", "citation", "consistency"}
        assert sum(breakers_by_family.values()) >= 40


# ---------------------------------------------------------------------------
# Deferral semantics — a check whose upstream is absent is DEFERRED, not passed.
# ---------------------------------------------------------------------------


class TestDeferral:
    def _banded_rat_trace(self):
        for t in ALL_TRACES:
            if t["lines"][0]["rat"]["decision"]["rate_band"] is not None:
                return copy.deepcopy(t)
        raise AssertionError("no banded RAT line found")

    def test_rat_consistency_deferred_without_jur(self):
        t = self._banded_rat_trace()
        t["lines"][0]["rat"]["decision"]["rate"] = 0.999  # wrong rate
        rat = t["lines"][0]["rat"]
        jur = t["jur"]

        # no upstream: consistency deferred, record not rejected
        v0 = validate_record("RAT", rat, accepted={})
        assert v0.accepted is True
        assert any("await" in d.lower() or "jur" in d.lower() for d in v0.deferred_checks)

        # with accepted JUR: the deferred check now runs and fails
        v1 = validate_record("RAT", rat, accepted={"JUR": jur})
        assert v1.accepted is False
        assert v1.failed_checks

    def test_clean_record_accepts(self):
        t = ALL_TRACES[0]
        v = validate_record("CLS", t["lines"][0]["cls"], accepted={})
        assert v.accepted is True and not v.failed_checks
