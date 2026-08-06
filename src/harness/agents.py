"""agents.py — worker construction and the ReAct tool loop (grounding
HARNESS_GROUNDING_2_ORCHESTRATION §4, §5).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_2_ORCHESTRATION.md (v1.1); Layer-1
interfaces binding.

A ``Worker`` wraps one activity slice as a single tool-using LLM agent
(grounding §2 fallback: our own loop over the ``ModelClient`` interface — see
model_client.py's flag). ``make_worker(slice, client)`` builds it; its system
message is ``prompts.assemble_prompt(slice)`` and its tools are exactly
``slice.tools`` (Layer-1 implementations, no re-wrapping of semantics — §4).

The loop is native function calling: the model may call tools (executed against
the Layer-1 tool callables under the active ToolContext, which logs every
invocation) and then emits ONE JSON payload in a fenced ```json block as the
last content of its final message (§5). ``extract_payload`` pulls it out; a
missing/unparsable block is surfaced as a structured extraction error that the
orchestrator maps to a validation failure (§5 failure ladder).

Worker context is PERSISTENT within a case (repairs continue the same
conversation) and discarded across cases (grounding §3.2) — the orchestrator
owns that lifecycle by holding/dropping Worker instances.

This is a TRUE agent-context module (grounding §1.2, §9): it imports only
``surface``/``tools``/``prompts``/``model_client`` — reaching at most
``src.oracle.rules`` — and never ``validator``/``labeler``/``scorer``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from src.harness import tools as tools_mod
from src.harness.model_client import Message, ModelClient, ModelResponse, ToolSpec
from src.harness.prompts import assemble_prompt
from src.harness.surface import WorkerSlice

# Bounded safety cap on tool-call rounds per worker turn. NOT a paper knob:
# per-call timeout and the case wall cap (§1) bound runtime; this only prevents a
# pathological tool-call loop. Flagged for review (grounding §9 ambiguity rule).
MAX_TOOL_ITERATIONS = 8

# Distinct extraction_error emitted when the tool-call cap is hit while the last
# response still requests tools (grounding L3 §6; DEVLOG 2026-08-01 follow-up).
TOOL_CAP_EXHAUSTED = "TOOL_CAP_EXHAUSTED"


# ---------------------------------------------------------------------------
# Native-function-calling tool specs (JSON Schemas) for the four Layer-1 tools.
# Names/shapes mirror tools.py signatures exactly; no semantics re-stated (§4).
# ---------------------------------------------------------------------------

TOOL_SPECS: dict[str, ToolSpec] = {
    "classification_reference": ToolSpec(
        name="classification_reference",
        description="Return the closed category vocabulary with each category's kind.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "vat_registration_check": ToolSpec(
        name="vat_registration_check",
        description="Return the customer's VAT-registration input field for a case.",
        parameters={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    ),
    "rate_table_lookup": ToolSpec(
        name="rate_table_lookup",
        description="Look up the rate for a (jurisdiction, band) from the bounded table.",
        parameters={
            "type": "object",
            "properties": {
                "jurisdiction": {"type": "string"},
                "band": {"type": "string"},
            },
            "required": ["jurisdiction", "band"],
            "additionalProperties": False,
        },
    ),
    "rule_citation_retrieval": ToolSpec(
        name="rule_citation_retrieval",
        description="Return the statutory text for a citation key from the closed set.",
        parameters={
            "type": "object",
            "properties": {"rule_key": {"type": "string"}},
            "required": ["rule_key"],
            "additionalProperties": False,
        },
    ),
}


# ---------------------------------------------------------------------------
# Per-call accounting record (grounding §7). Populated from the API usage block.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCall:
    input_tokens: int
    output_tokens: int
    finish_reason: str
    api_retries: int
    latency_ms: Optional[float]

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "finish_reason": self.finish_reason,
            "api_retries": self.api_retries,
            "latency_ms": self.latency_ms,
        }


@dataclass
class WorkerTurn:
    """The result of one worker invocation (initial bundle or a repair)."""

    text: str
    payload: Optional[dict]
    extraction_error: Optional[str]
    model_calls: list[ModelCall] = field(default_factory=list)


class Worker:
    """One activity slice as a tool-using agent. Persistent within a case."""

    def __init__(
        self,
        slice_: WorkerSlice,
        client: ModelClient,
        worker_id: str,
        *,
        timeout_s: float,
        max_tool_iters: int = MAX_TOOL_ITERATIONS,
        system: Optional[str] = None,
    ):
        self.slice = slice_
        self.client = client
        self.worker_id = worker_id
        self.timeout_s = timeout_s
        self.max_tool_iters = max_tool_iters
        # S0 supplies its own system message (whole-trace contract + S0' knobs,
        # grounding §6); C1-C4 workers use the pure slice→prompt assembly (§4).
        self.system = system if system is not None else assemble_prompt(slice_)
        self._tool_specs: list[ToolSpec] = [
            TOOL_SPECS[name] for name in sorted(slice_.tools)
        ]
        self.history: list[Message] = [Message(role="system", content=self.system)]
        # Persistent, cumulative per-worker call accounting (§3.2 continuity).
        self.model_calls: list[ModelCall] = []

    # -- tool execution -----------------------------------------------------

    def _execute_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.slice.tools:
            # A tool outside this worker's F_tau is not permitted (§3.2 slice).
            return {"error": "TOOL_NOT_PERMITTED"}
        fn = tools_mod.TOOLS.get(name)
        if fn is None:
            return {"error": "UNKNOWN_TOOL"}
        try:
            return fn(**arguments)
        except TypeError:
            return {"error": "BAD_ARGUMENTS"}

    # -- history hygiene ------------------------------------------------------

    def _heal_dangling_tool_calls(self) -> None:
        """Harness fix (ratified 2026-08-02): if the previous invocation ended
        with the assistant still requesting tools — the tool-cap exhaustion
        path returns TOOL_CAP_EXHAUSTED with the assistant ``tool_calls``
        message already in the persistent history and no ``tool`` results —
        then appending the next user message directly after it produces a
        dangling ``tool_use`` block and the API rejects the request (400:
        "tool_use ids ... without tool_result blocks"). Before ANY new user
        message, answer every pending call with a deterministic cancellation
        result so the history is well-formed on every exit path, present and
        future. The cancellation payload is harness infrastructure of the same
        class as TOOL_NOT_PERMITTED / TOOL_UNAVAILABLE and is
        condition-invariant."""
        if not self.history:
            return
        last = self.history[-1]
        if last.role != "assistant" or not last.tool_calls:
            return
        for tc in last.tool_calls:
            self.history.append(
                Message(
                    role="tool",
                    content=json.dumps(
                        {"error": "TOOL_CALL_CANCELLED",
                         "reason": "tool iteration cap reached"},
                        sort_keys=True,
                        ensure_ascii=True,
                    ),
                    tool_call_id=tc.id,
                    name=tc.name,
                )
            )

    # -- one invocation (initial or repair) ---------------------------------

    async def run(self, user_text: str, *, force_timeout: bool = False) -> WorkerTurn:
        """Append ``user_text``, run the tool loop until a final text message,
        and return the extracted payload. Raises asyncio.TimeoutError if any
        model call exceeds ``timeout_s`` (the orchestrator handles it, §3.2).

        ``force_timeout`` models the §3.6 worker_timeout seam (D1-A): the
        in-flight model call is cancelled at the FIRST model call — after
        ``user_text`` is in history but before any assistant turn, and with no
        usage — which is exactly the state a real 120 s timeout leaves behind.
        We raise immediately (no real wait, no logged model_calls) so natural
        repair (§3.1) runs against a conversation that already carries the task,
        just as every non-injected repair does."""
        self._heal_dangling_tool_calls()
        self.history.append(Message(role="user", content=user_text))
        if force_timeout:
            raise asyncio.TimeoutError("forced worker_timeout seam")
        turn_calls: list[ModelCall] = []
        iters = 0
        while True:
            resp: ModelResponse = await asyncio.wait_for(
                self.client.create(self.history, self._tool_specs, tag=self.worker_id),
                timeout=self.timeout_s,
            )
            call = ModelCall(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                finish_reason=resp.finish_reason,
                api_retries=resp.api_retries,
                latency_ms=resp.latency_ms,
            )
            self.model_calls.append(call)
            turn_calls.append(call)
            self.history.append(
                Message(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                )
            )
            if resp.tool_calls:
                if iters < self.max_tool_iters:
                    iters += 1
                    for tc in resp.tool_calls:
                        result = self._execute_tool(tc.name, tc.arguments)
                        self.history.append(
                            Message(
                                role="tool",
                                content=json.dumps(result, sort_keys=True, ensure_ascii=True),
                                tool_call_id=tc.id,
                                name=tc.name,
                            )
                        )
                    continue
                # Cap reached while the last response STILL requests tools: tag
                # this distinctly so Layer-4 analysis can attribute these
                # terminals separately from ordinary no-fenced-block failures
                # (grounding L3 §6; DEVLOG 2026-08-01 follow-up).
                return WorkerTurn(
                    text=resp.content,
                    payload=None,
                    extraction_error=TOOL_CAP_EXHAUSTED,
                    model_calls=turn_calls,
                )
            payload, err = extract_payload(resp.content)
            return WorkerTurn(
                text=resp.content,
                payload=payload,
                extraction_error=err,
                model_calls=turn_calls,
            )


def make_worker(
    slice_: WorkerSlice,
    client: ModelClient,
    worker_id: str,
    *,
    timeout_s: float,
    max_tool_iters: int = MAX_TOOL_ITERATIONS,
    system: Optional[str] = None,
) -> Worker:
    """Build a worker for ``slice_`` (grounding §4)."""
    return Worker(
        slice_, client, worker_id,
        timeout_s=timeout_s, max_tool_iters=max_tool_iters, system=system,
    )


# ---------------------------------------------------------------------------
# §5 structured output channel: extract the LAST fenced ```json block.
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_payload(text: str) -> tuple[Optional[dict], Optional[str]]:
    """Return (payload, None) on success, else (None, "payload: <reason>").
    Uses the LAST fenced ```json block (grounding §5). A missing block or
    unparsable JSON is a structured extraction error the orchestrator maps to a
    validation failure consuming repair budget (§5 failure ladder)."""
    if not isinstance(text, str):
        return None, "payload: no assistant text"
    matches = _JSON_BLOCK.findall(text)
    if not matches:
        return None, "payload: no fenced json block in final message"
    raw = matches[-1]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"payload: invalid json ({exc.msg})"
    if not isinstance(parsed, dict):
        return None, "payload: json payload is not an object"
    return parsed, None
