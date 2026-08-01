"""s0.py — the S0 monolith agent, its whole-trace repair loop, and the S0′
matched-token knobs (grounding HARNESS_GROUNDING_2_ORCHESTRATION §6).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_2_ORCHESTRATION.md (v1.1); Layer-1
interfaces binding.

S0 (paper §4.5): one agent, ReAct/function-calling style, full
``agent_case_view``, full tool set F, and the exemption table R as visible
state; it emits the COMPLETE ``final_trace`` JSON (all records + ``final``) in
one payload. Validation is the authoritative ``validate_trace`` ONLY (whole-trace
repair unit); on failure it gets a whole-trace repair message with the verbatim
``failed_checks``, in the same persistent context, up to
``s0_trace_repair_budget = 3`` repairs. No ledgers, no per-subtask dispatch, no
incremental verdicts (§6).

S0′ assembly knobs (paper §6.3): three sanctioned budget-expansion slots —
extended role description, exemplar slots (filled only with dev-case-derived
exemplars), and an intermediate-scratchpad instruction — plus per-case token
measurement, so Layer 4 can tune S0′ to within ±10% of a matched budget. The
tuning LOOP itself is Layer 4; Layer 2 provides the knobs and the measurement.

Same module class as ``orchestrator.py``: control + validation, not pure
agent-context. All agent-context STRING assembly reuses the strictly-isolated
``prompts``/``agents`` components; no ``labeler``/``scorer`` import (§9).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from src.harness import runlog, tools as tools_mod
from src.harness.agents import make_worker
from src.harness.model_client import ModelClient
from src.harness.orchestrator import RunConfig, _manifest_identity
from src.harness.prompts import (
    ROLE_PREAMBLE,
    SUBTASK_INSTRUCTIONS,
    TOOL_USE_RULES,
    ordered_assigned,
    output_contract,
)
from src.harness.surface import (
    EXEMPTION_TABLE_TEXT,
    SUBTASKS,
    agent_case_view,
    slice_for,
)
from src.harness.validation import assembly_gate
from src.oracle.rules import Case

S0_WORKER_ID = "S0"
_FULL = frozenset(SUBTASKS)


# ---------------------------------------------------------------------------
# S0′ matched-token knobs (paper §6.3). Empty by default → plain S0. Filled by
# Layer 4 from the DEV split only (grounding §6; never near eval cases).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S0Knobs:
    extended_role: str = ""
    exemplars: tuple[str, ...] = ()          # dev-case-derived exemplars only
    scratchpad_instruction: str = ""

    def is_plain(self) -> bool:
        return not (self.extended_role or self.exemplars or self.scratchpad_instruction)


# ---------------------------------------------------------------------------
# S0 prompt assembly — reuses the shared static components (§4) plus the three
# S0′ slots (§6). S0 emits the FULL final_trace (case_id, jur, lines, final),
# unlike a bundle worker (whose case_id is added by the orchestrator).
# ---------------------------------------------------------------------------


def assemble_s0_prompt(knobs: S0Knobs = S0Knobs()) -> str:
    slice_ = slice_for(_FULL)
    ordered = ordered_assigned(_FULL)
    sections: list[str] = [ROLE_PREAMBLE]

    # S0′ slot 1: extended role description.
    if knobs.extended_role:
        sections += ["", knobs.extended_role]

    sections += [
        "",
        "You solve the ENTIRE VAT determination for a case in one pass, with no "
        "orchestrator and no per-subtask dispatch. You own all five subtasks: "
        + ", ".join(ordered) + ".",
        "You will receive the full case view (parties, transaction type, "
        "registration, and line items).",
        "Tools you may call: " + ", ".join(sorted(slice_.tools)) + ".",
        "",
    ]
    for t in ordered:
        sections.append(SUBTASK_INSTRUCTIONS[t])
    sections.append("")
    for t in ordered:
        sections.append(output_contract(t))
    sections.append("")

    sections.append("Exemption table (reference R), authoritative:")
    sections.append(EXEMPTION_TABLE_TEXT.rstrip("\n"))
    sections.append("")

    sections.append(TOOL_USE_RULES)

    # S0′ slot 2: intermediate-scratchpad instruction.
    if knobs.scratchpad_instruction:
        sections += ["", knobs.scratchpad_instruction]

    # S0′ slot 3: exemplar slots (dev-derived only).
    if knobs.exemplars:
        sections += ["", "Worked exemplars (for format guidance only):"]
        sections += list(knobs.exemplars)

    sections += [
        "",
        "Emit exactly one JSON object in a single fenced ```json block as the "
        "LAST content of your final message, of the complete shape: "
        '{"case_id": <id>, "jur": <JUR record>, "lines": [{"line_id": <id>, '
        '"cls": <record>, "rat": <record>, "exm": <record>, "rch": <record>}, '
        '...], "final": <aggregation block>}. Produce every record and the final '
        "block yourself.",
    ]
    return "\n".join(sections)


def s0_prompt_hash(knobs: S0Knobs = S0Knobs()) -> str:
    return hashlib.sha256(assemble_s0_prompt(knobs).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result type (mirrors orchestrator.CaseResult; S0 has no ledgers/verdicts).
# ---------------------------------------------------------------------------


@dataclass
class S0Result:
    case_id: str
    status: str                    # ok|validation_exhausted|timeout|no_trace
    emitted: Optional[dict]
    gate_ok: bool
    failed_checks: tuple[str, ...]
    run_record: dict

    @property
    def total_tokens(self) -> int:
        """Per-case token measurement for S0′ matched-token tuning (§6)."""
        return self.run_record["accounting"]["token_counts"]["total"]


def measure_s0_tokens(result: S0Result) -> int:
    """The per-case token budget Layer 4 matches S0′ to within ±10% (§6)."""
    return result.total_tokens


# ---------------------------------------------------------------------------
# S0 execution — whole-trace repair loop (§6).
# ---------------------------------------------------------------------------


def _s0_input_message(view: dict) -> str:
    body = json.dumps(view, sort_keys=True, ensure_ascii=True)
    return (
        "Here is the full case view as JSON:\n" + body + "\n\n"
        "Solve the entire determination and emit the complete final_trace now."
    )


def _apply_s0_interception(
    emitted: dict, case_id: str, injection: tools_mod.InjectionController, log
) -> None:
    """Replace a τ slot of the emitted trace if the interception seam fires
    (§3.6 for S0: the τ slot of the emitted trace). Mutates ``emitted``."""
    def _fire(subtask: str) -> None:
        if log is not None:
            log.injection_events.append(
                {"seam": tools_mod.SEAM_HALLUCINATED_OUTPUT, "case_id": case_id,
                 "subtask": subtask, "fired": True, "note": "S0 interception"}
            )

    jur = emitted.get("jur")
    if isinstance(jur, dict):
        rep = injection.hallucinate(case_id, "JUR", jur)
        if rep is not None:
            emitted["jur"] = rep
            _fire("JUR")
    for line in emitted.get("lines", []):
        if not isinstance(line, dict):
            continue
        for t in ("CLS", "RAT", "EXM", "RCH"):
            rec = line.get(t.lower())
            if isinstance(rec, dict):
                rep = injection.hallucinate(case_id, t, rec)
                if rep is not None:
                    line[t.lower()] = rep
                    _fire(t)


async def run_s0(
    case: Case,
    client: ModelClient,
    *,
    config: RunConfig = RunConfig(),
    knobs: S0Knobs = S0Knobs(),
    injection: Optional[tools_mod.InjectionController] = None,
) -> S0Result:
    """Run one case under S0: one agent, whole-trace repair, no ledgers (§6)."""
    injection = injection or tools_mod.InjectionController()
    view = agent_case_view(case)
    case_id = view["case_id"]
    system = assemble_s0_prompt(knobs)
    worker = make_worker(
        slice_for(_FULL), client, S0_WORKER_ID,
        timeout_s=config.call_timeout_s, system=system,
    )

    budget = config.constants.s0_trace_repair_budget
    log = tools_mod.ToolLog()
    ctx = tools_mod.ToolContext(log=log, active_case_id=case_id, injection=injection)

    model_calls: list[dict] = []
    status = "no_trace"
    emitted: Optional[dict] = None
    gate_ok = False
    gate_failed: list[str] = []
    repairs_used = 0
    retries = 0

    with tools_mod.using_context(ctx):
        # initial attempt + whole-trace repairs
        user_text = _s0_input_message(view)
        while True:
            forced_timeout = any(
                injection.worker_timeout(case_id, t) for t in ordered_assigned(_FULL)
            )
            if forced_timeout:
                log.injection_events.append(
                    {"seam": tools_mod.SEAM_WORKER_TIMEOUT, "case_id": case_id,
                     "subtask": None, "fired": True, "note": "S0 worker_timeout seam"}
                )
                timed_out = True
                payload, ext_err = None, "payload: S0 invocation timed out (seam)"
            else:
                try:
                    turn = await worker.run(user_text)
                    for c in turn.model_calls:
                        model_calls.append({"worker_id": S0_WORKER_ID, **c.as_dict()})
                    payload, ext_err = turn.payload, turn.extraction_error
                    timed_out = False
                except asyncio.TimeoutError:
                    payload, ext_err = None, "payload: S0 invocation timed out"
                    timed_out = True

            if payload is not None:
                _apply_s0_interception(payload, case_id, injection, log)
                result = assembly_gate(payload)
                emitted = payload
                gate_ok = result.ok
                gate_failed = list(result.failed_checks)
            else:
                gate_ok = False
                gate_failed = [ext_err or "payload: no trace"]

            if gate_ok:
                status = "ok"
                break

            # whole-trace repair (§6): verbatim failed_checks feedback only.
            if repairs_used >= budget:
                status = "timeout" if (timed_out and emitted is None) else "validation_exhausted"
                break
            repairs_used += 1
            retries += 1
            user_text = (
                "The trace you emitted failed validation with the following "
                "checks:\n" + "\n".join(gate_failed) + "\n\n"
                "Re-emit the complete final_trace in the same fenced ```json format."
            )

    run_record = _build_s0_run_record(
        case_id, status, gate_ok, gate_failed, log, model_calls,
        config, knobs, retries,
    )
    return S0Result(
        case_id=case_id,
        status=status,
        emitted=emitted,
        gate_ok=gate_ok,
        failed_checks=tuple(gate_failed),
        run_record=run_record,
    )


def run_s0_blocking(
    case: Case,
    client: ModelClient,
    *,
    config: RunConfig = RunConfig(),
    knobs: S0Knobs = S0Knobs(),
    injection: Optional[tools_mod.InjectionController] = None,
) -> S0Result:
    return asyncio.run(
        run_s0(case, client, config=config, knobs=knobs, injection=injection)
    )


def _build_s0_run_record(
    case_id, status, gate_ok, gate_failed, log, model_calls, config, knobs, retries
) -> dict:
    oracle_commit, dataset_sha256 = _manifest_identity()
    record = runlog.new_run_record(
        condition="S0",
        case_id=case_id,
        repeat=0,
        oracle_commit=oracle_commit,
        dataset_sha256=dataset_sha256,
    )
    record["workers"] = [
        {
            "worker_id": S0_WORKER_ID,
            "assigned": list(ordered_assigned(_FULL)),
            "dispatches": 1,
            "retries": retries,
            "retry_verdicts": [],
            "terminal_status": "ok" if status == "ok" else status,
        }
    ]
    record["tool_invocations"] = list(log.tool_invocations)
    # S0 has NO incremental per-record verdicts (§6): only the final whole-trace gate.
    record["validation"]["record_verdicts"] = []
    record["validation"]["final"] = {"ok": gate_ok, "failed_checks": list(gate_failed)}
    record["injection_events"] = list(log.injection_events)

    tot_in = sum(c["input_tokens"] for c in model_calls)
    tot_out = sum(c["output_tokens"] for c in model_calls)
    latencies = [c["latency_ms"] for c in model_calls if c["latency_ms"] is not None]
    record["accounting"] = {
        "token_counts": {
            "input": tot_in,
            "output": tot_out,
            "total": tot_in + tot_out,
            "by_worker": {S0_WORKER_ID: {"input": tot_in, "output": tot_out}},
        },
        "latency_ms": sum(latencies) if latencies else None,
        "model_calls": model_calls,
        "execution_constants": config.constants.as_dict(),
        "prompt_hashes": {S0_WORKER_ID: s0_prompt_hash(knobs)},
        "case_status": status,
        "s0_knobs_plain": knobs.is_plain(),
    }
    return record
