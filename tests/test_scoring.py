"""test_scoring.py — offline scoring reproduces every §6.1 rule
(grounding HARNESS_GROUNDING_4_SWEEP §4; ORACLE_GROUNDING §5).

Hand-built synthetic records; expectations computed by hand.
"""

from __future__ import annotations

import copy

import pytest

from scripts import sweep_common as sc
from scripts.score_runs import score_record
from src.harness.injection import load_plan
from src.oracle import generator, labeler, validator

PRICE = {"model": "x", "usd_per_1m_input_tokens": 1.0, "usd_per_1m_output_tokens": 5.0}


@pytest.fixture(scope="module")
def eval_by_id():
    ds = generator.generate_dataset(seed=42)
    return {c.case_id: c for c in ds.eval_cases}


def _emitted(case):
    return validator.trace_to_emitted(labeler.label_case(case))


def _raw(case_id, emitted, *, mode="none", status="ok", tokens=(100, 50),
         retries=0, tool_calls=1, injection=None):
    tin, tout = tokens
    return {
        "schema_version": sc.RAW_SCHEMA_VERSION,
        "sweep": {"phase": 1, "mode": mode, "condition": "C1", "case_id": case_id, "repeat": 0},
        "terminal_status": status,
        "emitted_trace": emitted,
        "wall_clock": {"duration_ms": 12.5},
        "plan_sha256": None,
        "run_record": {
            "accounting": {
                "token_counts": {"input": tin, "output": tout, "total": tin + tout},
                "injection": injection or {"mode": mode, "tau": None, "fired": False, "plan_sha256": None, "details": {}},
            },
            "workers": [{"retries": retries}],
            "tool_invocations": [{"tool": "t", "arguments": {}, "result": {}}] * tool_calls,
        },
    }


class TestCorrect:
    def test_all_correct(self, eval_by_id):
        case = eval_by_id["eval_001"]
        r = score_record(_raw("eval_001", _emitted(case)), PRICE)
        assert r["final_answer_accuracy"] is True
        assert all(r[f] for f in ("jurisdiction_ok", "rate_ok", "exemption_ok",
                                  "reverse_charge_ok", "liable_party_ok", "vat_or_reason_ok"))
        assert all(r[f"step_{t}_ok"] for t in ("CLS", "JUR", "RAT", "EXM", "RCH"))
        assert all(r[f"step_{t}_status"] == "correct" for t in ("CLS", "JUR", "RAT", "EXM", "RCH"))
        assert r["trace_consistent"] is True
        assert r["earliest_failing_subtask"] == ""
        assert r["earliest_error_ties"] == ""

    def test_dollar_derivation(self, eval_by_id):
        # 100 input @ $1/1M + 50 output @ $5/1M = 0.0001 + 0.00025 = 0.00035
        r = score_record(_raw("eval_001", _emitted(eval_by_id["eval_001"]), tokens=(100, 50)), PRICE)
        assert r["dollars"] == pytest.approx(0.00035)
        assert r["prompt_tokens"] == 100 and r["completion_tokens"] == 50
        assert r["total_tokens"] == 150
        assert r["latency_ms"] == 12.5


class TestWrongFields:
    def test_wrong_jurisdiction(self, eval_by_id):
        case = eval_by_id["eval_001"]
        e = copy.deepcopy(_emitted(case))
        e["jur"]["decision"]["jurisdiction"] = "FR" if e["jur"]["decision"]["jurisdiction"] != "FR" else "DE"
        r = score_record(_raw("eval_001", e), PRICE)
        assert r["jurisdiction_ok"] is False
        assert r["final_answer_accuracy"] is False
        assert r["earliest_failing_subtask"] == "JUR"

    def test_wrong_rate_earliest_rat(self, eval_by_id):
        case = eval_by_id["eval_001"]
        e = copy.deepcopy(_emitted(case))
        e["lines"][0]["rat"]["decision"]["rate"] = 0.999
        r = score_record(_raw("eval_001", e), PRICE)
        assert r["rate_ok"] is False
        assert r["step_RAT_status"] == "incorrect"
        assert r["earliest_failing_subtask"] == "RAT"
        assert r["earliest_error_ties"] == "RAT"  # EXM still correct → no tie


class TestSameLayerTie:
    def test_rat_and_exm_wrong_same_layer(self, eval_by_id):
        case = eval_by_id["eval_001"]
        e = copy.deepcopy(_emitted(case))
        e["lines"][0]["rat"]["decision"]["rate"] = 0.999                    # RAT wrong
        cur = e["lines"][0]["exm"]["decision"]["exempt"]
        e["lines"][0]["exm"]["decision"]["exempt"] = not cur                # EXM wrong
        r = score_record(_raw("eval_001", e), PRICE)
        # single label by fixed order = RAT; both recorded in the aux column.
        assert r["earliest_failing_subtask"] == "RAT"
        assert r["earliest_error_ties"] == "RAT,EXM"


class TestTerminalAndMissing:
    def test_terminal_no_trace(self, eval_by_id):
        r = score_record(_raw("eval_001", None, status="validation_exhausted"), PRICE)
        assert r["terminal"] is True
        assert r["final_answer_accuracy"] is False
        assert r["trace_consistent"] is False
        assert r["earliest_failing_subtask"] == "CLS"
        assert all(r[f"step_{t}_status"] == "missing" for t in ("CLS", "JUR", "RAT", "EXM", "RCH"))
        # cost/latency counted in full even for terminal (§6.1).
        assert r["total_tokens"] == 150 and r["latency_ms"] == 12.5

    def test_terminal_forces_incorrect_even_with_partial_trace(self, eval_by_id):
        # A structurally-complete but terminal-flagged run is still final-incorrect.
        case = eval_by_id["eval_001"]
        r = score_record(_raw("eval_001", _emitted(case), status="timeout"), PRICE)
        assert r["terminal"] is True
        assert r["final_answer_accuracy"] is False
        assert r["trace_consistent"] is False

    def test_missing_step_recorded_missing(self, eval_by_id):
        case = eval_by_id["eval_001"]
        e = copy.deepcopy(_emitted(case))
        e["lines"][0].pop("rch")  # drop a per-line record
        r = score_record(_raw("eval_001", e), PRICE)
        assert r["step_RCH_status"] == "missing"
        assert r["step_RCH_ok"] is False
        assert r["earliest_failing_subtask"] == "RCH"


class TestSubstitutionSuccess:
    def _hallucinated(self, eval_by_id):
        """A CLS-targeted hallucination case whose injected record survives into
        the emitted trace (so record_substituted is True)."""
        plan = load_plan()
        cls_case = next(
            cid for cid, t in plan["tau_by_case"].items()
            if t == "CLS"
            and _emitted(eval_by_id[cid])["lines"][0]["exm"]["decision"]["exempt"] is False
        )
        e = copy.deepcopy(_emitted(eval_by_id[cls_case]))
        e["lines"][0]["cls"] = copy.deepcopy(plan["hallucinated_record_by_case"][cls_case])
        marker = {"mode": "hallucination", "tau": "CLS", "fired": True,
                  "plan_sha256": plan["content_sha256"], "details": {}}
        return cls_case, e, marker

    def test_hallucination_success_is_literal_validated_trace(self, eval_by_id):
        # §6.4-literal: substitution_success = validated trace within budget
        # (terminal ok), IDENTICAL to timeout/outage — independent of survival.
        cls_case, e, marker = self._hallucinated(eval_by_id)
        r = score_record(_raw(cls_case, e, mode="hallucination", status="ok",
                              injection=marker), PRICE)
        assert r["substitution_success"] is True
        # record_substituted is the separate survival column.
        assert r["record_substituted"] is True

    def test_hallucination_terminal_is_success_failure_but_may_survive(self, eval_by_id):
        # A poisoned record that survives but whose run never validated:
        # substitution_success False (no validated trace), record_substituted True.
        cls_case, e, marker = self._hallucinated(eval_by_id)
        r = score_record(_raw(cls_case, e, mode="hallucination", status="timeout",
                              injection=marker), PRICE)
        assert r["substitution_success"] is False
        assert r["record_substituted"] is True

    def test_none_mode_substitution_na(self, eval_by_id):
        r = score_record(_raw("eval_001", _emitted(eval_by_id["eval_001"])), PRICE)
        assert r["substitution_success"] is None
        assert r["record_substituted"] is None

    # §6.4 for timeout / outage: fraction reaching a validated trace within budget.
    def _injected(self, mode, tau="RCH"):
        return {"mode": mode, "tau": tau, "fired": True, "plan_sha256": None, "details": {}}

    def test_timeout_validated_trace_is_success(self, eval_by_id):
        case = eval_by_id["eval_001"]
        r = score_record(_raw("eval_001", _emitted(case), mode="timeout",
                              status="ok", injection=self._injected("timeout")), PRICE)
        assert r["substitution_success"] is True
        assert r["record_substituted"] is None  # record-survival is hallucination-only

    def test_timeout_exhausted_is_failure(self, eval_by_id):
        r = score_record(_raw("eval_001", None, mode="timeout",
                              status="validation_exhausted",
                              injection=self._injected("timeout")), PRICE)
        assert r["substitution_success"] is False

    def test_outage_validated_trace_is_success(self, eval_by_id):
        case = eval_by_id["eval_001"]
        r = score_record(_raw("eval_001", _emitted(case), mode="outage",
                              status="ok", injection=self._injected("outage")), PRICE)
        assert r["substitution_success"] is True
        assert r["record_substituted"] is None  # record-survival is hallucination-only

    def test_injected_mode_not_fired_is_na(self, eval_by_id):
        # §6.4 denominator is INJECTED (fired) cases; a non-fired cell is not
        # applicable (None -> dropped by analyze), not a failure.
        case = eval_by_id["eval_001"]
        marker = {"mode": "timeout", "tau": None, "fired": False,
                  "plan_sha256": None, "details": {}}
        r = score_record(_raw("eval_001", _emitted(case), mode="timeout",
                              status="ok", injection=marker), PRICE)
        assert r["substitution_success"] is None
