"""test_orchestrator.py — the deterministic orchestrator (grounding
HARNESS_GROUNDING_2_ORCHESTRATION §3, §7, §8, §10).

Every test here runs against the scripted client — no network, no API key (§8).
Covers: happy path C1-C4, per-subtask repair with verbatim feedback, budget
exhaustion + partial-trace retention, payload-extraction ladder, timeout
(real wait_for + seam-forced), C4 RAT‖EXM concurrency + cap, accounting, the
authoritative assembly gate, and injection seams (honored + logged).
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from conftest import bundle_payload, fence, happy_script, turn
from src.harness import tools as tools_mod
from src.harness.model_client import make_scripted_client
from src.harness.orchestrator import RunConfig, run_case
from src.harness.prompts import worker_id
from src.harness.runlog import validate_run_record
from src.harness.surface import PARTITIONS
from src.oracle import validator

CONDITIONS = ("C1", "C2", "C3", "C4")
CFG = RunConfig(timeout_s=5)


def _multi_line_case(dataset):
    for c in list(dataset.eval_cases) + list(dataset.dev_cases):
        if len(c.line_items) >= 2:
            return c
    return dataset.dev_cases[0]


def _run(condition, case, script, **kw):
    return asyncio.run(
        run_case(condition, case, make_scripted_client(script), config=CFG, **kw)
    )


# --- §8/§10 happy path per condition ---------------------------------------


class TestHappyPath:
    @pytest.mark.parametrize("condition", CONDITIONS)
    def test_assembled_trace_passes_validate_trace(self, condition, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        res = _run(condition, case, happy_script(condition, emitted))
        assert res.status == "ok"
        assert res.gate_ok is True
        # The assembled trace equals the oracle trace and passes the authoritative gate.
        assert res.emitted["case_id"] == case.case_id
        assert validator.validate_trace(res.emitted).ok is True

    @pytest.mark.parametrize("condition", CONDITIONS)
    def test_run_record_complete_and_valid(self, condition, dataset, emitted_for):
        case = _multi_line_case(dataset)
        res = _run(condition, case, happy_script(condition, emitted_for(case)))
        rec = res.run_record
        assert validate_run_record(rec).ok, validate_run_record(rec).errors
        assert rec["identity"]["condition"] == condition
        assert len(rec["workers"]) == len(PARTITIONS[condition])
        assert rec["validation"]["final"]["ok"] is True
        # EXECUTION_CONSTANTS + PROMPT_HASHES echoed (grounding §7).
        assert rec["accounting"]["execution_constants"]["model"] == "claude-haiku-4-5-20251001"
        assert rec["accounting"]["prompt_hashes"]


# --- §8 repair path (verbatim feedback, budget decrement, eventual accept) --


class _RecordingClient:
    """Wraps a scripted client and records the latest user message per create."""

    def __init__(self, inner):
        self._inner = inner
        self.user_messages = []

    async def create(self, messages, tools, *, tag=""):
        for m in reversed(messages):
            if m.role == "user":
                self.user_messages.append(m.content)
                break
        return await self._inner.create(messages, tools, tag=tag)


class TestRepair:
    def _c4_script(self, emitted, rat_turns):
        s = {}
        for grp in PARTITIONS["C4"]:
            wid = worker_id("C4", grp)
            if grp == frozenset({"RAT"}):
                s[wid] = rat_turns
            else:
                s[wid] = [turn(fence(bundle_payload(grp, emitted)))]
        return s

    def test_wrong_rate_repaired_with_verbatim_feedback(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        bad = copy.deepcopy(emitted)
        for ln in bad["lines"]:
            ln["rat"]["decision"]["rate"] = 0.999
        rat_turns = [
            turn(fence(bundle_payload(frozenset({"RAT"}), bad))),
            turn(fence(bundle_payload(frozenset({"RAT"}), emitted))),
        ]
        client = _RecordingClient(make_scripted_client(self._c4_script(emitted, rat_turns)))
        res = asyncio.run(run_case("C4", case, client, config=CFG))
        assert res.status == "ok" and res.gate_ok
        # RAT owner consumed exactly one repair.
        rat_rec = next(w for w in res.run_record["workers"] if w["worker_id"] == "C4:RAT")
        assert rat_rec["retries"] == 1
        # The repair feedback carried the verbatim validator failed_checks (§3.2/§9).
        repair_msgs = [m for m in client.user_messages if "failed validation" in m]
        assert repair_msgs
        assert "consistency: RAT rate 0.999 != table" in repair_msgs[-1]

    def test_bad_citation_repaired(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        bad = copy.deepcopy(emitted)
        for ln in bad["lines"]:
            ln["rat"]["rule_reference"] = "FAKE.KEY"
        rat_turns = [
            turn(fence(bundle_payload(frozenset({"RAT"}), bad))),
            turn(fence(bundle_payload(frozenset({"RAT"}), emitted))),
        ]
        res = asyncio.run(run_case("C4", case, make_scripted_client(self._c4_script(emitted, rat_turns)), config=CFG))
        assert res.status == "ok" and res.gate_ok

    def test_malformed_payload_consumes_budget_then_recovers(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        rat_turns = [
            turn("no json block at all"),                       # extraction failure
            turn("```json\n{not json}\n```"),                    # parse failure
            turn(fence(bundle_payload(frozenset({"RAT"}), emitted))),  # recover
        ]
        res = asyncio.run(run_case("C4", case, make_scripted_client(self._c4_script(emitted, rat_turns)), config=CFG))
        assert res.status == "ok" and res.gate_ok
        rat_rec = next(w for w in res.run_record["workers"] if w["worker_id"] == "C4:RAT")
        assert rat_rec["retries"] == 2

    def test_budget_exhaustion_terminal_and_partial_retained(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        bad = copy.deepcopy(emitted)
        for ln in bad["lines"]:
            ln["rat"]["decision"]["rate"] = 0.999
        # initial + 3 repairs, all bad -> validation_exhausted
        rat_turns = [turn(fence(bundle_payload(frozenset({"RAT"}), bad))) for _ in range(4)]
        res = asyncio.run(run_case("C4", case, make_scripted_client(self._c4_script(emitted, rat_turns)), config=CFG))
        assert res.status == "validation_exhausted"
        assert res.gate_ok is False
        rat_rec = next(w for w in res.run_record["workers"] if w["worker_id"] == "C4:RAT")
        assert rat_rec["terminal_status"] == "validation_exhausted"
        assert rat_rec["retries"] == 3  # 3 repairs (initial emission is not a repair)
        # partial trace retained (CLS/JUR accepted before RAT failed).
        assert res.emitted is not None
        assert res.emitted["jur"] is not None


# --- §8 timeout machinery ---------------------------------------------------


class TestTimeout:
    def _c4_script(self, emitted, rat_turns):
        s = {}
        for grp in PARTITIONS["C4"]:
            wid = worker_id("C4", grp)
            s[wid] = rat_turns if grp == frozenset({"RAT"}) else [turn(fence(bundle_payload(grp, emitted)))]
        return s

    def test_real_delay_exceeds_timeout_then_recovers(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        rat_turns = [
            {"delay_s": 0.2, "text": "late", "usage": {"input_tokens": 1, "output_tokens": 1}},
            turn(fence(bundle_payload(frozenset({"RAT"}), emitted))),
        ]
        # per-call timeout 0.05 s << 0.2 s scripted delay -> wait_for fires.
        cfg = RunConfig(timeout_s=0.05)
        res = asyncio.run(run_case("C4", case, make_scripted_client(self._c4_script(emitted, rat_turns)), config=cfg))
        assert res.status == "ok" and res.gate_ok

    def test_seam_forced_timeout_logged_then_recovers(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)

        class TimeoutOnce(tools_mod.InjectionController):
            def __init__(self):
                self._fired = False

            def worker_timeout(self, case_id, subtask):
                if subtask == "RAT" and not self._fired:
                    self._fired = True
                    return True
                return False

        rat_turns = [turn(fence(bundle_payload(frozenset({"RAT"}), emitted)))]  # repair succeeds
        res = asyncio.run(
            run_case("C4", case, make_scripted_client(self._c4_script(emitted, rat_turns)),
                     config=CFG, injection=TimeoutOnce())
        )
        assert res.status == "ok" and res.gate_ok
        seams = [e["seam"] for e in res.run_record["injection_events"]]
        assert tools_mod.SEAM_WORKER_TIMEOUT in seams


# --- §8/§10 C4 concurrency (RAT‖EXM), cap honored; C1-C3 sequential ---------


class _ConcurrencyProbe:
    def __init__(self, inner):
        self._inner = inner
        self.current = 0
        self.max_concurrent = 0

    async def create(self, messages, tools, *, tag=""):
        self.current += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        try:
            await asyncio.sleep(0.02)  # force overlap window
            return await self._inner.create(messages, tools, tag=tag)
        finally:
            self.current -= 1


class TestConcurrency:
    def test_c4_rat_exm_run_concurrently_capped_at_two(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        probe = _ConcurrencyProbe(make_scripted_client(happy_script("C4", emitted_for(case))))
        res = asyncio.run(run_case("C4", case, probe, config=CFG))
        assert res.status == "ok"
        assert probe.max_concurrent == 2  # exactly RAT‖EXM, cap honored

    @pytest.mark.parametrize("condition", ("C1", "C2", "C3"))
    def test_non_c4_strictly_sequential(self, condition, dataset, emitted_for):
        case = _multi_line_case(dataset)
        probe = _ConcurrencyProbe(make_scripted_client(happy_script(condition, emitted_for(case))))
        res = asyncio.run(run_case(condition, case, probe, config=CFG))
        assert res.status == "ok"
        assert probe.max_concurrent == 1

    def test_c4_deterministic_across_repeats(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        r1 = _run("C4", case, happy_script("C4", emitted))
        r2 = _run("C4", case, happy_script("C4", emitted))
        assert r1.emitted == r2.emitted
        assert r1.run_record["accounting"]["token_counts"] == r2.run_record["accounting"]["token_counts"]


# --- §8 accounting (synthesised usage propagates exactly) -------------------


class TestAccounting:
    def test_token_totals_and_by_worker(self, dataset, emitted_for):
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)
        script = {}
        for grp in PARTITIONS["C4"]:
            wid = worker_id("C4", grp)
            script[wid] = [turn(fence(bundle_payload(grp, emitted)), input_tokens=11, output_tokens=7)]
        res = _run("C4", case, script)
        tc = res.run_record["accounting"]["token_counts"]
        n_workers = len(PARTITIONS["C4"])
        assert tc["input"] == 11 * n_workers
        assert tc["output"] == 7 * n_workers
        assert tc["total"] == 18 * n_workers
        assert set(tc["by_worker"]) == set(script)
        assert all(bw == {"input": 11, "output": 7} for bw in tc["by_worker"].values())
        # every model call recorded (grounding §7).
        assert len(res.run_record["accounting"]["model_calls"]) == n_workers


# --- §10 assembly gate is authoritative + injection interception ------------


class TestGateAndInjection:
    def test_interception_conforming_record_no_retry_but_logged(self, dataset, emitted_for):
        # A GEN_GOODS↔GEN_SERVICE swap is schema-valid, citation-consistent
        # (still CLS.ASSIGNED), and V-consistent (same standard band, non-exempt):
        # a conforming-but-oracle-wrong record — exactly ORACLE_GROUNDING §4's
        # hallucination shape — which fires NO retry by design (grounding §3.6).
        _SWAP = {"GEN_GOODS": "GEN_SERVICE", "GEN_SERVICE": "GEN_GOODS"}
        case = None
        for c in list(dataset.eval_cases) + list(dataset.dev_cases):
            emit = emitted_for(c)
            if all(ln["cls"]["decision"]["category"] in _SWAP for ln in emit["lines"]):
                case = c
                break
        assert case is not None, "no case with only GEN_GOODS/GEN_SERVICE lines"
        emitted = emitted_for(case)

        class FlipCls(tools_mod.InjectionController):
            def hallucinate(self, case_id, subtask, record):
                if subtask == "CLS":
                    r = copy.deepcopy(record)
                    r["decision"]["category"] = _SWAP[r["decision"]["category"]]
                    return r
                return None

        res = asyncio.run(
            run_case("C1", case, make_scripted_client(happy_script("C1", emitted)),
                     config=CFG, injection=FlipCls())
        )
        # A conforming injected record fires no retry (grounding §3.6).
        cls_worker = res.run_record["workers"][0]
        assert cls_worker["retries"] == 0
        assert res.status == "ok" and res.gate_ok
        seams = [e["seam"] for e in res.run_record["injection_events"]]
        assert tools_mod.SEAM_HALLUCINATED_OUTPUT in seams
        # and the injected (oracle-wrong) category actually reached the trace.
        assert res.emitted["lines"][0]["cls"]["decision"]["category"] == _SWAP[
            emitted["lines"][0]["cls"]["decision"]["category"]
        ]

    def test_outage_seam_passthrough_logged(self, dataset, emitted_for):
        # The orchestrator passes case context so the Layer-1 rate outage seam can
        # fire inside rate_table_lookup when a worker calls it (grounding §3.6).
        case = _multi_line_case(dataset)
        emitted = emitted_for(case)

        class Outage(tools_mod.InjectionController):
            def rate_outage(self, case_id):
                return True

        rat_grp = frozenset({"RAT"})
        script = {}
        for grp in PARTITIONS["C4"]:
            wid = worker_id("C4", grp)
            if grp == rat_grp:
                script[wid] = [
                    turn("", input_tokens=1, output_tokens=1,
                         tool_calls=[{"name": "rate_table_lookup",
                                      "arguments": {"jurisdiction": "DE", "band": "standard"}}]),
                    turn(fence(bundle_payload(grp, emitted))),
                ]
            else:
                script[wid] = [turn(fence(bundle_payload(grp, emitted)))]
        res = asyncio.run(
            run_case("C4", case, make_scripted_client(script), config=CFG, injection=Outage())
        )
        seams = [e["seam"] for e in res.run_record["injection_events"]]
        assert tools_mod.SEAM_RATE_TABLE_OUTAGE in seams
        # the tool invocation was logged too (§7.2).
        assert any(t["tool"] == "rate_table_lookup" for t in res.run_record["tool_invocations"])
