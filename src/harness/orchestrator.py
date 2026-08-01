"""orchestrator.py — the deterministic orchestrator for C1-C4 (grounding
HARNESS_GROUNDING_2_ORCHESTRATION §3, §7).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_2_ORCHESTRATION.md (v1.1); Layer-1
interfaces binding.

[DECISION 1] The orchestrator implements the Magentic-One LEDGER SEMANTICS of
paper §4.2 (task ledger, progress ledger, dependency-ordered dispatch,
validation-gated retry) as DETERMINISTIC PYTHON. **It makes no LLM calls** —
LLM calls happen only inside worker invocations (§3.1, §9).

[DECISION 2] Dispatch = one initial invocation per worker over its full assigned
bundle; validation is per record in SUBTASKS order via Layer-1
``validate_record``; repair = per-subtask follow-up invocations in the same
persistent worker conversation, feedback = the verbatim ``failed_checks`` list
and nothing else (§3.2, §9). Budget = ``subtask_retry_budget`` repairs per
subtask. The RCH-owning worker emits the ``final`` block; the harness never
computes it (§3.5). Assembly runs the authoritative ``validator.validate_trace``
gate (§3.5, Layer-1 §7.1).

This module is a CONTROL + VALIDATION module (like ``validation.py``), not a
pure agent-context module: it imports ``validation`` (→ the frozen
``validator``, which type-imports ``labeler``). It does NOT import ``labeler`` or
``scorer`` directly, and ``scorer`` is never reachable from it (grounding §9;
enforced by the extended import-graph test). All agent-context STRING assembly is
delegated to the strictly-isolated ``prompts``/``agents`` modules; the only case
data this module places in agent context is the label-free ``agent_case_view``
projection and agent-produced (or Layer-3-injected) records — never oracle
labels.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from src.harness import runlog, tools as tools_mod
from src.harness.agents import Worker, make_worker
from src.harness.model_client import EXECUTION_CONSTANTS, ExecutionConstants, ModelClient
from src.harness.prompts import (
    CASE_LEVEL_SUBTASKS,
    PER_LINE_SUBTASKS,
    ordered_assigned,
    prompt_hashes,
    worker_id as make_worker_id,
)
from src.harness.surface import (
    CASE_VIEW_ATOMS,
    DEPENDS,
    PARTITIONS,
    REFERENCE_ATOM,
    SUBTASKS,
    WorkerSlice,
    agent_case_view,
    slice_for,
)
from src.harness.validation import assembly_gate, validate_record
from src.oracle.rules import Case

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


# ---------------------------------------------------------------------------
# Run configuration — test-facing overrides that DO NOT touch the frozen
# EXECUTION_CONSTANTS (§1). Production uses the constants' timeout/budget;
# scripted tests may shrink the timeout to exercise the wait_for path cheaply.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    constants: ExecutionConstants = EXECUTION_CONSTANTS
    timeout_s: Optional[float] = None          # override per-call timeout (tests)
    within_case_concurrency: int = 2           # §3.4 cap

    @property
    def call_timeout_s(self) -> float:
        return self.timeout_s if self.timeout_s is not None else self.constants.timeout_s

    @property
    def subtask_retry_budget(self) -> int:
        return self.constants.subtask_retry_budget


# ---------------------------------------------------------------------------
# Ledgers (grounding §3.1). Serialised into the run record.
# ---------------------------------------------------------------------------


@dataclass
class SubtaskState:
    subtask: str
    owner_worker: str
    status: str = "pending"       # pending|in_flight|accepted|failed_terminal
    attempts: int = 0             # total emissions targeting this subtask
    repairs_used: int = 0         # repair invocations consumed (≤ budget)


class _CaseTerminal(Exception):
    """A subtask exhausted its budget (or timed out terminally): the case ends
    with ``validation_exhausted`` and the partial trace is retained (§3.2)."""

    def __init__(self, subtask: str):
        super().__init__(subtask)
        self.subtask = subtask


@dataclass
class CaseResult:
    condition: str
    case_id: str
    status: str                    # ok|validation_exhausted|timeout|no_trace
    emitted: Optional[dict]        # assembled full trace (or partial)
    gate_ok: bool
    failed_checks: tuple[str, ...]
    run_record: dict


# ---------------------------------------------------------------------------
# Identity helpers.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _manifest_identity() -> tuple[str, str]:
    path = _DATA_DIR / "MANIFEST.json"
    if not path.is_file():
        return ("UNKNOWN", "UNKNOWN")
    m = json.loads(path.read_text(encoding="utf-8"))
    return (m.get("oracle_commit", "UNKNOWN"), m.get("dataset_sha256", "UNKNOWN"))


# ===========================================================================
# The orchestrator.
# ===========================================================================


class _Orchestrator:
    def __init__(
        self,
        condition: str,
        case: Case,
        client: ModelClient,
        config: RunConfig,
        injection: tools_mod.InjectionController,
    ):
        if condition not in PARTITIONS:
            raise ValueError(f"{condition!r} is not a partition condition (C1-C4)")
        self.condition = condition
        self.case = case
        self.client = client
        self.config = config
        self.injection = injection

        self.view = agent_case_view(case)   # label-free projection (Layer-1 §1.1)
        self.case_id = self.view["case_id"]
        self.line_ids = [li["line_id"] for li in self.view["line_items"]]

        # Build worker slices + owners in partition order (§3.1).
        self.groups: list[frozenset[str]] = list(PARTITIONS[condition])
        self.workers: dict[str, Worker] = {}
        self.worker_assigned: dict[str, frozenset[str]] = {}
        self.subtask_state: dict[str, SubtaskState] = {}
        for group in self.groups:
            wid = make_worker_id(condition, group)
            self.workers[wid] = make_worker(
                slice_for(group), client, wid, timeout_s=config.call_timeout_s
            )
            self.worker_assigned[wid] = group
            for tau in group:
                self.subtask_state[tau] = SubtaskState(subtask=tau, owner_worker=wid)

        # Accepted records: JUR case-level; the rest per line_id.
        self.accepted_case: dict[str, dict] = {}
        self.accepted_lines: dict[str, dict[str, dict]] = {t: {} for t in SUBTASKS}
        self.final_block: Optional[dict] = None

        # Run-record accumulators.
        self.record_verdicts: list[dict] = []
        self.worker_records: dict[str, dict] = {
            wid: {
                "worker_id": wid,
                "assigned": list(ordered_assigned(grp)),
                "dispatches": 0,
                "retries": 0,
                "retry_verdicts": [],
                "terminal_status": "ok",
            }
            for wid, grp in self.worker_assigned.items()
        }
        self.model_call_log: list[dict] = []

    # -- input-state assembly (dynamic, per case) ---------------------------

    def _input_state_message(self, slice_: WorkerSlice) -> str:
        """Assemble the worker's input-state payload (view slice + accepted
        upstream records). Label-free by construction (agent_case_view + accepted
        agent-produced records only)."""
        payload: dict = {"case_id": self.case_id}
        for atom in sorted(CASE_VIEW_ATOMS & slice_.input_state):
            payload[atom] = self.view[atom]

        upstream = sorted(
            t for t in SUBTASKS if f"record:{t}" in slice_.input_state
        )
        for t in upstream:
            if t in CASE_LEVEL_SUBTASKS and t in self.accepted_case:
                payload[t.lower()] = self.accepted_case[t]
        per_line_up = [t for t in upstream if t in PER_LINE_SUBTASKS]
        if per_line_up:
            payload["lines"] = [
                {
                    "line_id": lid,
                    **{t.lower(): self.accepted_lines[t][lid] for t in per_line_up
                       if lid in self.accepted_lines[t]},
                }
                for lid in self.line_ids
            ]
        # REFERENCE_ATOM (exemption table) is delivered via the system prompt for
        # the EXM owner (grounding §5); not duplicated here.
        assert REFERENCE_ATOM not in payload
        body = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return (
            "Here is your input state as JSON:\n" + body + "\n\n"
            "Produce your full assigned bundle now."
        )

    # -- extracting subtask records from a bundle payload -------------------

    def _records_from_payload(
        self, payload: Optional[dict], subtask: str
    ) -> tuple[dict[str, dict], Optional[str]]:
        """Return ({line_id or '__case__': record}, error). A missing record for
        an owned subtask is a structured payload error (§5 ladder)."""
        if payload is None:
            return {}, f"payload: missing {subtask} record (no payload)"
        if subtask in CASE_LEVEL_SUBTASKS:
            rec = payload.get("jur")
            if not isinstance(rec, dict):
                return {}, "payload: missing jur record"
            return {"__case__": rec}, None
        # per-line
        out: dict[str, dict] = {}
        lines = payload.get("lines")
        by_id: dict[str, dict] = {}
        if isinstance(lines, list):
            for entry in lines:
                if isinstance(entry, dict) and isinstance(entry.get("line_id"), str):
                    by_id[entry["line_id"]] = entry
        missing: list[str] = []
        key = subtask.lower()
        for lid in self.line_ids:
            entry = by_id.get(lid, {})
            rec = entry.get(key)
            if isinstance(rec, dict):
                out[lid] = rec
            else:
                missing.append(lid)
        err = None
        if missing:
            err = f"payload: missing {subtask} record for line(s) {missing}"
        return out, err

    def _accepted_context_for_line(self, lid: str) -> dict[str, dict]:
        ctx: dict[str, dict] = {}
        if "JUR" in self.accepted_case:
            ctx["JUR"] = self.accepted_case["JUR"]
        for u in ("CLS", "RAT", "EXM"):
            if lid in self.accepted_lines[u]:
                ctx[u] = self.accepted_lines[u][lid]
        return ctx

    def _validate_subtask(
        self, subtask: str, records: dict[str, dict], payload_error: Optional[str]
    ) -> tuple[bool, list[str], list[dict]]:
        """Validate all records for a subtask. Returns
        (accepted, failed_checks_verbatim, verdict_dicts)."""
        failed: list[str] = []
        verdicts: list[dict] = []
        if payload_error:
            failed.append(payload_error)
        if subtask in CASE_LEVEL_SUBTASKS:
            rec = records.get("__case__")
            if rec is not None:
                v = validate_record(subtask, rec, accepted={})
                verdicts.append(_verdict_dict(v, line_id=None))
                if not v.accepted:
                    failed.extend(v.failed_checks)
                accepted = v.accepted and not payload_error
            else:
                accepted = False
            return accepted, failed, verdicts
        # per-line
        all_ok = not payload_error
        for lid in self.line_ids:
            rec = records.get(lid)
            if rec is None:
                all_ok = False
                continue
            ctx = self._accepted_context_for_line(lid)
            v = validate_record(subtask, rec, accepted=ctx)
            verdicts.append(_verdict_dict(v, line_id=lid))
            if not v.accepted:
                all_ok = False
                failed.extend(v.failed_checks)
        return all_ok, failed, verdicts

    def _accept_subtask(self, subtask: str, records: dict[str, dict]) -> None:
        if subtask in CASE_LEVEL_SUBTASKS:
            self.accepted_case[subtask] = records["__case__"]
        else:
            self.accepted_lines[subtask] = dict(records)
        self.subtask_state[subtask].status = "accepted"

    # -- injection seams (§3.6; content is Layer 3, no-op by default) --------

    def _apply_interception(
        self, subtask: str, records: dict[str, dict]
    ) -> dict[str, dict]:
        """Between payload extraction and validation, replace a τ record with a
        Layer-3-constructed one if the interception seam fires (§3.6)."""
        out = dict(records)
        for lid, rec in records.items():
            replacement = self.injection.hallucinate(self.case_id, subtask, rec)
            if replacement is not None:
                out[lid] = replacement
                self._log_injection_fired(
                    tools_mod.SEAM_HALLUCINATED_OUTPUT, subtask,
                    "hallucinated_output interception",
                )
        return out

    def _log_injection_fired(self, seam: str, subtask: Optional[str], note: str) -> None:
        ctx = tools_mod.get_context()
        if ctx.log is not None:
            ctx.log.injection_events.append(
                {"seam": seam, "case_id": self.case_id, "subtask": subtask,
                 "fired": True, "note": note}
            )

    def _timeout_forced(self, assigned: frozenset[str]) -> Optional[str]:
        """If the timeout seam designates any owned τ, return that τ (§3.6)."""
        for tau in ordered_assigned(assigned):
            if self.injection.worker_timeout(self.case_id, tau):
                self._log_injection_fired(
                    tools_mod.SEAM_WORKER_TIMEOUT, tau, "worker_timeout seam"
                )
                return tau
        return None

    # -- one worker: initial bundle + per-subtask repair --------------------

    async def _invoke(self, worker: Worker, user_text: str) -> tuple[Optional[dict], Optional[str], bool]:
        """Run one worker invocation. Returns (payload, extraction_error,
        timed_out)."""
        try:
            turn = await worker.run(user_text)
        except asyncio.TimeoutError:
            return None, "payload: worker invocation timed out", True
        for c in turn.model_calls:
            self.model_call_log.append({"worker_id": worker.worker_id, **c.as_dict()})
        return turn.payload, turn.extraction_error, False

    async def _process_worker(self, wid: str) -> None:
        worker = self.workers[wid]
        assigned = self.worker_assigned[wid]
        owned_order = ordered_assigned(assigned)
        wrec = self.worker_records[wid]
        budget = self.config.subtask_retry_budget

        # --- initial bundle dispatch --------------------------------------
        forced = self._timeout_forced(assigned)
        if forced is not None:
            payload, ext_err, timed_out = None, "payload: worker invocation timed out (seam)", True
        else:
            for tau in owned_order:
                self.subtask_state[tau].status = "in_flight"
                self.subtask_state[tau].attempts += 1
            payload, ext_err, timed_out = await self._invoke(
                worker, self._input_state_message(worker.slice)
            )
        wrec["dispatches"] += 1
        if payload is not None and "final" in payload:
            self.final_block = payload.get("final")

        # Validate each owned subtask in SUBTASKS order; accept passers.
        pending: list[str] = []
        for tau in owned_order:
            records, prec_err = self._records_from_payload(payload, tau)
            records = self._apply_interception(tau, records)
            err = ext_err or prec_err
            accepted, failed, verdicts = self._validate_subtask(tau, records, err)
            self.record_verdicts.extend(_tag(verdicts, attempt=0))
            if accepted:
                self._accept_subtask(tau, records)
            else:
                pending.append(tau)
                self._stash_failed(tau, failed)

        # --- per-subtask repair loop (§3.2) -------------------------------
        for tau in owned_order:
            if self.subtask_state[tau].status == "accepted":
                continue
            await self._repair_subtask(worker, wid, tau, budget)

    async def _repair_subtask(self, worker: Worker, wid: str, tau: str, budget: int) -> None:
        wrec = self.worker_records[wid]
        while self.subtask_state[tau].status != "accepted":
            if self.subtask_state[tau].repairs_used >= budget:
                self.subtask_state[tau].status = "failed_terminal"
                wrec["terminal_status"] = "validation_exhausted"
                raise _CaseTerminal(tau)
            self.subtask_state[tau].repairs_used += 1
            self.subtask_state[tau].attempts += 1
            wrec["retries"] += 1

            feedback = self._repair_message(tau)
            payload, ext_err, timed_out = await self._invoke(worker, feedback)
            if payload is not None and "final" in payload:
                self.final_block = payload.get("final")
            records, prec_err = self._records_from_payload(payload, tau)
            records = self._apply_interception(tau, records)
            err = ext_err or prec_err
            accepted, failed, verdicts = self._validate_subtask(tau, records, err)
            tagged = _tag(verdicts, attempt=self.subtask_state[tau].repairs_used)
            self.record_verdicts.extend(tagged)
            wrec["retry_verdicts"].extend(tagged)
            if accepted:
                self._accept_subtask(tau, records)
            else:
                self._stash_failed(tau, failed)

    # Verbatim failed_checks feedback (§3.2, §9): the ONLY variable content is the
    # validator's own failed_checks list. The fixed structural wrapper names the
    # subtask and the re-emit contract — it carries no hint, no restated rule, and
    # no oracle-derived content (grounding §9; bounded interpretation, flagged).
    def _stash_failed(self, tau: str, failed: list[str]) -> None:
        self._last_failed = getattr(self, "_last_failed", {})
        self._last_failed[tau] = list(failed)

    def _repair_message(self, tau: str) -> str:
        failed = getattr(self, "_last_failed", {}).get(tau, [])
        lines = "\n".join(failed)
        return (
            f"Your {tau} output failed validation with the following checks:\n"
            f"{lines}\n\n"
            f"Re-emit only the {tau} record(s) in the same fenced ```json bundle format."
        )

    # -- wave scheduling with the §3.4 within-case concurrency cap ----------

    def _external_deps(self, assigned: frozenset[str]) -> frozenset[str]:
        union: set[str] = set()
        for tau in assigned:
            union |= DEPENDS[tau]
        return frozenset(union - assigned)

    async def run(self) -> CaseResult:
        log = tools_mod.ToolLog()
        ctx = tools_mod.ToolContext(
            log=log, active_case_id=self.case_id, injection=self.injection
        )
        status = "ok"
        with tools_mod.using_context(ctx):
            try:
                remaining = list(self.workers.keys())
                sem = asyncio.Semaphore(self.config.within_case_concurrency)
                while remaining:
                    ready = [
                        wid for wid in remaining
                        if self._external_deps(self.worker_assigned[wid])
                        <= set(self._accepted_subtasks())
                    ]
                    if not ready:
                        # No worker can proceed — a dependency failed terminally.
                        break
                    # Deterministic order (partition order).
                    ready.sort(key=lambda w: list(self.workers).index(w))

                    async def _guarded(wid: str) -> None:
                        async with sem:
                            await self._process_worker(wid)

                    await asyncio.gather(*(_guarded(wid) for wid in ready))
                    for wid in ready:
                        remaining.remove(wid)
            except _CaseTerminal:
                status = "validation_exhausted"

        emitted, gate_ok, gate_failed = self._assemble_and_gate(status)
        if status == "ok" and not gate_ok:
            # Rare per the Layer-1 equivalence invariant; route to the culpable
            # owner and retry within budget (§3.5).
            emitted, gate_ok, gate_failed, status = await self._route_gate_failure(
                emitted, gate_failed, ctx
            )

        run_record = self._build_run_record(status, gate_ok, gate_failed, log)
        return CaseResult(
            condition=self.condition,
            case_id=self.case_id,
            status=status,
            emitted=emitted,
            gate_ok=gate_ok,
            failed_checks=tuple(gate_failed),
            run_record=run_record,
        )

    def _accepted_subtasks(self) -> list[str]:
        return [t for t, s in self.subtask_state.items() if s.status == "accepted"]

    # -- assembly + authoritative full-trace gate (§3.5) --------------------

    def _assemble(self) -> Optional[dict]:
        if "JUR" not in self.accepted_case:
            return None
        lines = []
        for lid in self.line_ids:
            entry = {"line_id": lid}
            complete = True
            for t in ("CLS", "RAT", "EXM", "RCH"):
                rec = self.accepted_lines[t].get(lid)
                if rec is None:
                    complete = False
                    break
                entry[t.lower()] = rec
            if not complete:
                return None
            lines.append(entry)
        if self.final_block is None:
            return None
        return {
            "case_id": self.case_id,
            "jur": self.accepted_case["JUR"],
            "lines": lines,
            "final": self.final_block,
        }

    def _assemble_and_gate(self, status: str) -> tuple[Optional[dict], bool, list[str]]:
        emitted = self._assemble()
        if emitted is None:
            # Partial trace retained; nothing to gate.
            partial = self._partial_trace()
            return partial, False, ["assembly: incomplete trace"]
        result = assembly_gate(emitted)
        return emitted, result.ok, list(result.failed_checks)

    def _partial_trace(self) -> Optional[dict]:
        """Whatever has been accepted so far (retained on terminal failure, §3.2)."""
        if not (self.accepted_case or any(self.accepted_lines.values())):
            return None
        lines = []
        for lid in self.line_ids:
            entry = {"line_id": lid}
            for t in ("CLS", "RAT", "EXM", "RCH"):
                rec = self.accepted_lines[t].get(lid)
                if rec is not None:
                    entry[t.lower()] = rec
            lines.append(entry)
        return {
            "case_id": self.case_id,
            "jur": self.accepted_case.get("JUR"),
            "lines": lines,
            "final": self.final_block,
        }

    async def _route_gate_failure(self, emitted, gate_failed, ctx):
        """Route a residual full-trace gate failure to the culpable owner (§3.5).
        Per the equivalence invariant this is almost always a ``final``-block
        issue → attributed to the RCH owner, which re-emits within RCH budget."""
        rch_state = self.subtask_state.get("RCH")
        if rch_state is None:
            return emitted, False, gate_failed, "validation_exhausted"
        worker = self.workers[rch_state.owner_worker]
        budget = self.config.subtask_retry_budget
        while rch_state.repairs_used < budget:
            rch_state.repairs_used += 1
            self.worker_records[rch_state.owner_worker]["retries"] += 1
            feedback = (
                "The assembled trace failed the final validation gate with:\n"
                + "\n".join(gate_failed)
                + "\n\nRe-emit your bundle (RCH records and the final block) in the "
                "same fenced ```json format."
            )
            payload, ext_err, timed_out = await self._invoke(worker, feedback)
            if payload is not None and "final" in payload:
                self.final_block = payload.get("final")
            records, prec_err = self._records_from_payload(payload, "RCH")
            records = self._apply_interception("RCH", records)
            accepted, failed, verdicts = self._validate_subtask("RCH", records, ext_err or prec_err)
            self.record_verdicts.extend(_tag(verdicts, attempt=rch_state.repairs_used))
            if accepted:
                self._accept_subtask("RCH", records)
            emitted, gate_ok, gate_failed = self._assemble_and_gate("ok")
            if gate_ok:
                return emitted, True, [], "ok"
        return emitted, False, gate_failed, "validation_exhausted"

    # -- run record (§7) ----------------------------------------------------

    def _build_run_record(self, status: str, gate_ok: bool, gate_failed, log) -> dict:
        oracle_commit, dataset_sha256 = _manifest_identity()
        record = runlog.new_run_record(
            condition=self.condition,
            case_id=self.case_id,
            repeat=0,
            oracle_commit=oracle_commit,
            dataset_sha256=dataset_sha256,
        )
        record["workers"] = [self.worker_records[wid] for wid in self.workers]
        record["tool_invocations"] = list(log.tool_invocations)
        record["validation"]["record_verdicts"] = self.record_verdicts
        record["validation"]["final"] = {"ok": gate_ok, "failed_checks": list(gate_failed)}
        record["injection_events"] = list(log.injection_events)

        tot_in = sum(c["input_tokens"] for c in self.model_call_log)
        tot_out = sum(c["output_tokens"] for c in self.model_call_log)
        by_worker: dict[str, dict] = {}
        for c in self.model_call_log:
            b = by_worker.setdefault(c["worker_id"], {"input": 0, "output": 0})
            b["input"] += c["input_tokens"]
            b["output"] += c["output_tokens"]
        latencies = [c["latency_ms"] for c in self.model_call_log if c["latency_ms"] is not None]
        record["accounting"] = {
            "token_counts": {
                "input": tot_in,
                "output": tot_out,
                "total": tot_in + tot_out,
                "by_worker": by_worker,
            },
            "latency_ms": sum(latencies) if latencies else None,
            "model_calls": self.model_call_log,
            "execution_constants": self.config.constants.as_dict(),
            "prompt_hashes": prompt_hashes(self.condition),
            "case_status": status,
        }
        return record


def _verdict_dict(v, line_id: Optional[str]) -> dict:
    d = {
        "subtask": v.subtask,
        "accepted": v.accepted,
        "failed_checks": list(v.failed_checks),
        "deferred_checks": list(v.deferred_checks),
    }
    if line_id is not None:
        d["line_id"] = line_id
    return d


def _tag(verdicts: list[dict], attempt: int) -> list[dict]:
    return [{**v, "attempt": attempt} for v in verdicts]


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


async def run_case(
    condition: str,
    case: Case,
    client: ModelClient,
    *,
    config: RunConfig = RunConfig(),
    injection: Optional[tools_mod.InjectionController] = None,
) -> CaseResult:
    """Run one (condition, case) through the deterministic orchestrator (§3).
    ``injection`` defaults to the Layer-1 no-op controller."""
    orch = _Orchestrator(
        condition, case, client, config, injection or tools_mod.InjectionController()
    )
    return await orch.run()


def run_case_blocking(
    condition: str,
    case: Case,
    client: ModelClient,
    *,
    config: RunConfig = RunConfig(),
    injection: Optional[tools_mod.InjectionController] = None,
) -> CaseResult:
    """Synchronous wrapper for tests / simple callers."""
    return asyncio.run(
        run_case(condition, case, client, config=config, injection=injection)
    )
