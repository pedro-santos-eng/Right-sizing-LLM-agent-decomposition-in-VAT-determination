"""test_injection.py — Layer-3 injection plan, controller, and seams (grounding
HARNESS_GROUNDING_3_INJECTION.md §8, §10).

All runtime tests use the scripted client — no network, no API key. This module
MAY import ``labeler`` (it is not an agent-context module) to check
oracle-incorrectness of the committed plan. Tests NEVER rewrite the committed
plan — regeneration is compared in-memory (§9).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import bundle_payload, fence, happy_script, turn
from scripts.generate_injection_plan import (
    INJECTION_SEED,
    PLAN_PATH,
    build_plan,
    serialize_plan,
)
from src.harness import injection as inj
from src.harness.agents import TOOL_CAP_EXHAUSTED, make_worker
from src.harness.injection import make_controller
from src.harness.model_client import make_scripted_client
from src.harness.orchestrator import RunConfig, run_case
from src.harness.prompts import ordered_assigned, worker_id
from src.harness.runlog import validate_run_record
from src.harness.s0 import run_s0
from src.harness.surface import PARTITIONS, SUBTASKS, slice_for
from src.harness.validation import validate_record
from src.oracle import generator, labeler, validator

CFG = RunConfig(timeout_s=5)


@pytest.fixture(scope="module")
def plan():
    return inj.load_plan()


@pytest.fixture(scope="module")
def eval_by_id():
    ds = generator.generate_dataset(seed=42)
    return {c.case_id: c for c in ds.eval_cases}


def _emitted(case):
    return validator.trace_to_emitted(labeler.label_case(case))


def _silent_cls_case(plan, eval_by_id):
    """A τ=CLS case whose first line is non-exempt → the CLS swap is silent
    (does not break the EXM↔CLS consistency downstream or at assembly)."""
    for cid, tau in plan["tau_by_case"].items():
        if tau == "CLS" and _emitted(eval_by_id[cid])["lines"][0]["exm"]["decision"]["exempt"] is False:
            return cid
    raise AssertionError("no silent CLS case found")


# ---------------------------------------------------------------------------
# §10: plan artifact committed; regeneration byte-identical.
# ---------------------------------------------------------------------------


class TestPlanArtifact:
    def test_regeneration_byte_identical(self):
        # Compared in-memory; the committed file is never rewritten by the test.
        assert serialize_plan(build_plan()) == PLAN_PATH.read_text(encoding="utf-8")

    def test_seed_and_shape(self, plan):
        assert plan["injection_seed"] == INJECTION_SEED == 20260801
        assert set(plan) == {
            "injection_seed", "generator_version", "tau_by_case",
            "hallucinated_record_by_case", "outage_cases", "content_sha256",
        }

    def test_tau_uniform_over_T_first_line(self, plan):
        tau = plan["tau_by_case"]
        assert len(tau) == 40
        assert all(t in SUBTASKS for t in tau.values())
        # every τ appears (uniform draw over T across 40 cases)
        assert set(tau.values()) == set(SUBTASKS)

    def test_outage_one_per_block_of_five(self, plan):
        outage = plan["outage_cases"]
        assert len(outage) == 8  # §5: 8 blocks → 8 cases, m=1, 20%
        ids = sorted(int(c.split("_")[1]) for c in outage)
        # exactly one case per block [1..5],[6..10],...,[36..40]
        blocks = {(n - 1) // 5 for n in ids}
        assert len(blocks) == 8 and blocks == set(range(8))

    def test_content_sha256_matches(self, plan):
        from scripts.generate_injection_plan import _content_sha256

        assert plan["content_sha256"] == _content_sha256(plan)


# ---------------------------------------------------------------------------
# §10: every hallucinated record passes validate_record AND differs from oracle.
# ---------------------------------------------------------------------------


class TestHallucinatedRecords:
    def test_all_records_validate_and_differ(self, plan, eval_by_id):
        for cid, tau in plan["tau_by_case"].items():
            record = plan["hallucinated_record_by_case"][cid]
            # (a) record-level / input-blind validation passes (§4).
            v = validate_record(tau, record, accepted={})
            assert v.accepted, (cid, tau, v.failed_checks)
            # (b) the decision is oracle-incorrect.
            emitted = _emitted(eval_by_id[cid])
            oracle = emitted["jur"] if tau == "JUR" else emitted["lines"][0][tau.lower()]
            assert record["decision"] != oracle["decision"], (cid, tau)
            assert record["subtask"] == tau


# ---------------------------------------------------------------------------
# §3 controller seam behaviors (unit).
# ---------------------------------------------------------------------------


class TestController:
    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            make_controller("banana")

    def test_none_mode_no_plan_needed(self):
        c = make_controller("none")
        assert c.worker_timeout("eval_001", "CLS") is False
        assert c.hallucinate("eval_001", "CLS", {}) is None
        assert c.rate_outage("eval_001") is False

    def test_timeout_fires_once_only_on_matching_tau(self, plan):
        cid, tau = next(iter(plan["tau_by_case"].items()))
        c = make_controller("timeout")
        other = next(t for t in SUBTASKS if t != tau)
        assert c.worker_timeout(cid, other) is False           # τ mismatch
        assert c.worker_timeout(cid, tau) is True              # initial fires
        assert c.worker_timeout(cid, tau) is False             # repairs never re-forced

    def test_hallucinate_fires_once_returns_plan_record(self, plan):
        cid, tau = next(iter(plan["tau_by_case"].items()))
        c = make_controller("hallucination")
        first = c.hallucinate(cid, tau, {"x": 1})
        assert first == plan["hallucinated_record_by_case"][cid]
        assert c.hallucinate(cid, tau, {"x": 1}) is None       # once per case
        assert c.hallucinate(cid, next(t for t in SUBTASKS if t != tau), {}) is None

    def test_outage_first_call_only_on_planned_cases(self, plan):
        c = make_controller("outage")
        oc = plan["outage_cases"][0]
        non = next(f"eval_{n:03d}" for n in range(1, 41) if f"eval_{n:03d}" not in plan["outage_cases"])
        assert c.rate_outage(oc) is True       # first lookup fails
        assert c.rate_outage(oc) is False      # recovers
        assert c.rate_outage(non) is False     # not a planned outage case

    def test_marker_accessors(self, plan):
        cid, tau = next(iter(plan["tau_by_case"].items()))
        c = make_controller("hallucination")
        assert c.tau_for(cid) == tau
        assert c.did_fire(cid) is False
        c.hallucinate(cid, tau, {})
        assert c.did_fire(cid) is True
        assert c.plan_sha256 == plan["content_sha256"]


# ---------------------------------------------------------------------------
# §8/§10 seam integration (scripted client).
# ---------------------------------------------------------------------------


class TestSeamsIntegration:
    def test_hallucination_silent_error_survives_in_trace(self, plan, eval_by_id):
        cid = _silent_cls_case(plan, eval_by_id)
        case = eval_by_id[cid]
        emitted = _emitted(case)
        oracle_cat = emitted["lines"][0]["cls"]["decision"]["category"]
        res = asyncio.run(run_case(
            "C1", case, make_scripted_client(happy_script("C1", emitted)),
            config=CFG, injection=make_controller("hallucination"),
        ))
        assert res.status == "ok"  # silent: no retry, passes assembly gate
        # the injected (oracle-wrong) CLS reached the assembled trace.
        assert res.emitted["lines"][0]["cls"]["decision"]["category"] != oracle_cat
        assert res.emitted["lines"][0]["cls"]["decision"]["category"] == \
            plan["hallucinated_record_by_case"][cid]["decision"]["category"]
        marker = res.run_record["accounting"]["injection"]
        assert marker["mode"] == "hallucination" and marker["fired"] is True
        assert marker["plan_sha256"] == plan["content_sha256"]

    def test_timeout_fires_once_then_natural_repair(self, plan, eval_by_id):
        # C4: the τ-owning single-subtask worker is force-timed-out on its initial
        # invocation, then its natural per-subtask repair proceeds (§3.1).
        cid = plan["outage_cases"][0]  # any eval case
        case = eval_by_id[cid]
        emitted = _emitted(case)
        res = asyncio.run(run_case(
            "C4", case, make_scripted_client(happy_script("C4", emitted)),
            config=CFG, injection=make_controller("timeout"),
        ))
        assert res.status == "ok"  # recovered via natural repair
        seams = [e["seam"] for e in res.run_record["injection_events"]]
        assert seams.count("worker_timeout") == 1  # fired exactly once
        # the τ owner shows exactly one retry (the forced timeout → one repair).
        tau = plan["tau_by_case"][cid]
        owner = worker_id("C4", frozenset({tau}))
        wrec = next(w for w in res.run_record["workers"] if w["worker_id"] == owner)
        assert wrec["retries"] == 1

    def test_outage_fires_inside_rate_table_lookup_then_recovers(self, plan, eval_by_id):
        cid = plan["outage_cases"][0]
        case = eval_by_id[cid]
        emitted = _emitted(case)
        rat_grp = frozenset({"RAT"})
        jur = emitted["jur"]["decision"]["jurisdiction"]
        # RAT worker: call the tool (outage → TOOL_UNAVAILABLE), call again
        # (recovers), then emit its bundle.
        script = {}
        for grp in PARTITIONS["C4"]:
            wid = worker_id("C4", grp)
            if grp == rat_grp:
                call = {"tool_calls": [{"name": "rate_table_lookup",
                                        "arguments": {"jurisdiction": jur, "band": "standard"}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}}
                script[wid] = [call, dict(call), turn(fence(bundle_payload(grp, emitted)))]
            else:
                script[wid] = [turn(fence(bundle_payload(grp, emitted)))]
        res = asyncio.run(run_case(
            "C4", case, make_scripted_client(script), config=CFG,
            injection=make_controller("outage"),
        ))
        seams = [e["seam"] for e in res.run_record["injection_events"]]
        assert seams.count("rate_table_outage") == 1  # first lookup only
        calls = [t for t in res.run_record["tool_invocations"] if t["tool"] == "rate_table_lookup"]
        assert calls[0]["result"] == {"error": "TOOL_UNAVAILABLE"}      # outage
        assert "rate" in calls[1]["result"]                             # recovered

    def test_s0_tau_slot_substitution(self, plan, eval_by_id):
        cid = _silent_cls_case(plan, eval_by_id)
        case = eval_by_id[cid]
        emitted = _emitted(case)
        oracle_cat = emitted["lines"][0]["cls"]["decision"]["category"]
        res = asyncio.run(run_s0(
            case, make_scripted_client({"S0": [turn(fence(emitted))]}),
            config=CFG, injection=make_controller("hallucination"),
        ))
        # the τ slot of the emitted trace was replaced by the plan record.
        assert res.emitted["lines"][0]["cls"]["decision"]["category"] == \
            plan["hallucinated_record_by_case"][cid]["decision"]["category"]
        assert res.emitted["lines"][0]["cls"]["decision"]["category"] != oracle_cat
        assert res.run_record["accounting"]["injection"]["mode"] == "hallucination"

    def test_markers_present_for_all_four_modes(self, plan, eval_by_id):
        cid = _silent_cls_case(plan, eval_by_id)
        case = eval_by_id[cid]
        emitted = _emitted(case)

        # none
        r_none = asyncio.run(run_case("C1", case, make_scripted_client(happy_script("C1", emitted)), config=CFG))
        m = r_none.run_record["accounting"]["injection"]
        assert m["mode"] == "none" and m["fired"] is False and m["plan_sha256"] is None
        assert validate_run_record(r_none.run_record).ok

        # hallucination / timeout / outage
        for mode, cond, cid2 in (
            ("hallucination", "C1", cid),
            ("timeout", "C4", plan["outage_cases"][0]),
            ("outage", "C4", plan["outage_cases"][0]),
        ):
            case2 = eval_by_id[cid2]
            em2 = _emitted(case2)
            res = asyncio.run(run_case(
                cond, case2, make_scripted_client(happy_script(cond, em2)),
                config=CFG, injection=make_controller(mode),
            ))
            marker = res.run_record["accounting"]["injection"]
            assert marker["mode"] == mode
            assert marker["plan_sha256"] == plan["content_sha256"]
            assert validate_run_record(res.run_record).ok


# ---------------------------------------------------------------------------
# §6 / §10: TOOL_CAP_EXHAUSTED distinct and tested.
# ---------------------------------------------------------------------------


class TestToolCap:
    def test_cap_exhaustion_tagged(self):
        # A worker whose model always requests tools hits the cap; the turn's
        # extraction_error is the distinct TOOL_CAP_EXHAUSTED marker (§6).
        tool_turn = {
            "tool_calls": [{"name": "rule_citation_retrieval",
                            "arguments": {"rule_key": "CLS.ASSIGNED"}}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client = make_scripted_client({"w": [tool_turn] * 20})
        worker = make_worker(slice_for(frozenset({"JUR"})), client, "w", timeout_s=5)
        turn_result = asyncio.run(worker.run("go"))
        assert turn_result.extraction_error == TOOL_CAP_EXHAUSTED
        assert turn_result.payload is None

    def test_cap_exhaustion_history_healed_on_repair(self):
        # Harness fix (ratified 2026-08-02): after cap exhaustion the history
        # ends with an assistant message whose tool_calls have no results; the
        # NEXT invocation (a repair) must heal it — every pending tool_use id
        # answered with a deterministic cancellation result BEFORE the new
        # user message — or the real API rejects the request (400).
        tool_turn = {
            "tool_calls": [{"name": "rule_citation_retrieval",
                            "arguments": {"rule_key": "CLS.ASSIGNED"}}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        final_turn = {
            "content": 'done ```json\n{"jur": {}}\n```',
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client = make_scripted_client({"w": [tool_turn] * 9 + [final_turn]})
        worker = make_worker(slice_for(frozenset({"JUR"})), client, "w", timeout_s=5)
        first = asyncio.run(worker.run("go"))
        assert first.extraction_error == TOOL_CAP_EXHAUSTED
        # The dangling assistant tool_calls message is the current tail.
        assert worker.history[-1].role == "assistant"
        assert worker.history[-1].tool_calls
        asyncio.run(worker.run("repair: re-emit"))
        # Well-formedness: every assistant message carrying tool_calls is
        # immediately followed by exactly one tool result per call, matching
        # ids in order — over the WHOLE history.
        h = worker.history
        for i, msg in enumerate(h):
            if msg.role == "assistant" and msg.tool_calls:
                for j, tc in enumerate(msg.tool_calls):
                    follow = h[i + 1 + j]
                    assert follow.role == "tool"
                    assert follow.tool_call_id == tc.id
        # The healed results carry the deterministic cancellation payload.
        healed = [m for m in h if m.role == "tool"
                  and "TOOL_CALL_CANCELLED" in m.content]
        assert len(healed) == 1
        # And the user repair message sits AFTER the healing results.
        cancel_idx = next(i for i, m in enumerate(h)
                          if m.role == "tool" and "TOOL_CALL_CANCELLED" in m.content)
        repair_idx = next(i for i, m in enumerate(h)
                          if m.role == "user" and m.content.startswith("repair:"))
        assert cancel_idx < repair_idx
