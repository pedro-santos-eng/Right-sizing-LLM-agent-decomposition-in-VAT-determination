"""model_client.py — frozen execution constants and the pinned model client
(grounding HARNESS_GROUNDING_2_ORCHESTRATION §1, §2).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_2_ORCHESTRATION.md (v1.1); Layer-1
interfaces (docs/HARNESS_GROUNDING_1_SURFACE.md v1.1) are binding.

This module exposes:

  - ``EXECUTION_CONSTANTS`` — the single frozen constants object (§1), echoed
    verbatim into every run record.
  - a small, provider-neutral ``ModelClient`` interface (Message / ToolSpec /
    ToolCall / Usage / ModelResponse) — "the interface is ours" (§2).
  - ``make_scripted_client(script)`` — the in-repo scripted client used by every
    Layer-2 test except the live smoke; it replays scripted turns and
    synthesizes ``usage`` numbers (§2, §8). No network, no API key.
  - ``make_model_client()`` — the real client. It lazily binds the ``anthropic``
    SDK and reads the key from ``ANTHROPIC_API_KEY`` only (§1). Used solely by
    the skip-if-unconfigured live smoke (§10).

**Flag for review (grounding §2 fallback clause, DECISION 1 part b).** §2 names
AutoGen's ``AnthropicChatCompletionClient`` / ``AssistantAgent`` as the default
backend but authorises implementing "the same ``make_model_client()`` interface
directly on the ``anthropic`` SDK" when the maintenance-mode AutoGen client
blocks "usage metadata per call, tool-call plumbing, timeout behavior". This
harness's PRIMARY cost metric is per-call token accounting (§7) with explicit
per-call timeout / transport-retry policy (§1) and last-fenced-JSON extraction
(§5); AutoGen's ``AssistantAgent`` abstracts the internal model calls behind its
own tool loop, which blocks deterministic per-call accounting and control. We
therefore invoke the §2 fallback: the interface is ours, the live backend is the
``anthropic`` SDK. AutoGen 0.7.5 remains pinned in pyproject as the documented
reference backend. This is flagged, not silent (§9). autogen is NOT imported
anywhere in Layer 2.

This module is a TRUE agent-context module (it shapes what is sent to the LLM):
it imports nothing from ``src.oracle`` — not even ``rules`` — so it cannot reach
a label source (grounding §1.2, §9 import-graph rule).

Pure at import: no network, no clock read, no key access at import time.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence


# ---------------------------------------------------------------------------
# §1. Fixed execution constants (paper §4.6; completed and pinned here).
# The list of fields is FROZEN (grounding §1 / DECISION 4). Echoed verbatim into
# every run record via ``as_dict``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionConstants:
    """The frozen (model, decoding, budget, timeout, transport) pin set (§1)."""

    model: str = "claude-haiku-4-5-20251001"
    temperature: float = 0.2                     # paper §4.6 (committed)
    top_p: Optional[float] = None                # §4.6 amended 2026-08-02: unset
                                                 # (model rejects temp+top_p); echoed as null
    max_tokens: int = 4096                       # DECISION 4
    timeout_s: int = 120                         # DECISION 4 (per model call)
    case_wall_cap_s: int = 1200                  # DECISION 4 (safety)
    subtask_retry_budget: int = 3                # paper §4.5 (C1-C4 repair unit)
    s0_trace_repair_budget: int = 3              # paper §4.5 (S0 repair unit)
    api_transport_retries: int = 3               # DECISION 4 (infrastructure)
    api_backoff_s: tuple[int, ...] = (2, 4, 8)   # DECISION 4 (429/5xx backoff)
    seed: Optional[int] = None                   # §1: API has no seed control

    def as_dict(self) -> dict:
        """A deterministic, JSON-serialisable echo of the constants (§1, §7)."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
            "case_wall_cap_s": self.case_wall_cap_s,
            "subtask_retry_budget": self.subtask_retry_budget,
            "s0_trace_repair_budget": self.s0_trace_repair_budget,
            "api_transport_retries": self.api_transport_retries,
            "api_backoff_s": list(self.api_backoff_s),
            "seed": self.seed,
        }


EXECUTION_CONSTANTS = ExecutionConstants()


# ---------------------------------------------------------------------------
# The provider-neutral ModelClient interface (grounding §2 "the interface is
# ours"). Messages, tool specs, tool calls, usage, response.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A native-function-calling tool description handed to the model. ``name``
    matches a Layer-1 tool callable; ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    """A model-requested tool invocation (native function calling)."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Message:
    """One message in a worker/S0 conversation. ``role`` ∈
    {system, user, assistant, tool}. Assistant tool-call turns carry
    ``tool_calls``; tool-result turns carry ``tool_call_id`` + ``name``."""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass(frozen=True)
class Usage:
    """Per-call token usage from the API ``usage`` block (§2, §7)."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelResponse:
    """One model-call result. ``content`` is the assistant text (possibly empty
    on a pure tool-call turn); ``tool_calls`` are native function calls; ``usage``
    is per-call; ``finish_reason`` ∈ {stop, tool_calls, length, ...};
    ``api_retries`` counts transport retries (§1, infrastructure); ``latency_ms``
    is wall-clock latency (log envelope only — never in agent content)."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    api_retries: int = 0
    latency_ms: Optional[float] = None


class ModelClient(Protocol):
    """Provider-neutral async chat client. One ``create`` per model call so the
    caller (the worker loop) controls the tool-call loop, per-call accounting,
    and timeout (grounding §2 fallback rationale)."""

    async def create(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        tag: str = "",
    ) -> ModelResponse:
        ...


# ---------------------------------------------------------------------------
# The scripted client (grounding §2, §8). Deterministic, no network, no key.
#
# A ``script`` is a mapping tag -> ordered list of scripted turns, where each
# turn is a dict. Turns are consumed FIFO per tag; the ``tag`` is the worker id
# (deterministic), so C4's concurrent RAT‖EXM stays deterministic regardless of
# interleaving (grounding §3.4). A missing/empty tag falls back to the ""
# default queue. Turn keys:
#   - "text": str                 -> a final assistant message (finish_reason
#                                    "stop"); the harness extracts its JSON.
#   - "tool_calls": [ {"name","arguments","id"?}, ... ]
#                                 -> a tool-call turn (finish_reason "tool_calls").
#   - "usage": {"input_tokens","output_tokens"}  (synthesised; §8 accounting).
#   - "finish_reason": str        (override; defaults per turn kind).
#   - "api_retries": int          (synthesised transport retries; §7).
#   - "delay_s": float            -> await asyncio.sleep(delay_s) before
#                                    responding, to drive the real wait_for
#                                    timeout path (§8 timeout test).
#   - "raise_timeout": True       -> raise asyncio.TimeoutError immediately
#                                    (deterministic timeout, no real delay).
#   - "require_in_history": str   -> history-sensitive turn: emit the turn's
#                                    "text"/"tool_calls" ONLY if some message
#                                    content contains this substring; otherwise
#                                    emit "absent_text" (default: a context-less
#                                    plain end_turn reply, no fenced block). This
#                                    models the live model's dependence on the
#                                    initial input-state message being present in
#                                    the conversation: with only a bare repair
#                                    message and no task, the real model returns
#                                    end_turn prose and never a valid bundle
#                                    (§3.1 timeout path; DEVLOG 2026-08-06).
# ---------------------------------------------------------------------------


class ScriptExhausted(RuntimeError):
    """Raised when the scripted client is asked for a turn it does not have —
    a test-authoring error, surfaced loudly rather than hanging."""


class ScriptedClient:
    """In-repo scripted ``ModelClient`` (grounding §2). ~fits the "~50-line"
    budget the doc anticipates."""

    def __init__(self, script: dict[str, Sequence[dict]] | Sequence[dict]):
        # Accept either a flat list (single "" queue) or a tag->list mapping.
        if isinstance(script, dict):
            self._queues: dict[str, list[dict]] = {
                tag: list(turns) for tag, turns in script.items()
            }
        else:
            self._queues = {"": list(script)}
        self.calls: list[dict] = []  # observability for tests

    def _next_turn(self, tag: str) -> dict:
        queue = self._queues.get(tag)
        if not queue:
            queue = self._queues.get("")
        if not queue:
            raise ScriptExhausted(f"no scripted turn for tag {tag!r}")
        return queue.pop(0)

    async def create(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        tag: str = "",
    ) -> ModelResponse:
        turn = self._next_turn(tag)
        self.calls.append({"tag": tag, "turn": turn})

        if turn.get("raise_timeout"):
            raise asyncio.TimeoutError(f"scripted timeout for tag {tag!r}")
        require = turn.get("require_in_history")
        if require is not None and not any(
            require in (m.content or "") for m in messages
        ):
            # Task context absent from history: mimic the live model replying
            # with context-less prose (finish_reason end_turn, no fenced block).
            return ModelResponse(
                content=turn.get(
                    "absent_text",
                    "I don't have the task in front of me — please restate it.",
                ),
                tool_calls=(),
                usage=Usage(input_tokens=0, output_tokens=0),
                finish_reason=turn.get("absent_finish_reason", "stop"),
                api_retries=0,
                latency_ms=0.0,
            )
        delay = turn.get("delay_s")
        if delay:
            await asyncio.sleep(float(delay))

        usage_d = turn.get("usage", {})
        usage = Usage(
            input_tokens=int(usage_d.get("input_tokens", 0)),
            output_tokens=int(usage_d.get("output_tokens", 0)),
        )
        raw_calls = turn.get("tool_calls")
        if raw_calls:
            tool_calls = tuple(
                ToolCall(
                    id=str(c.get("id", f"call_{i}")),
                    name=c["name"],
                    arguments=dict(c.get("arguments", {})),
                )
                for i, c in enumerate(raw_calls)
            )
            finish = turn.get("finish_reason", "tool_calls")
            return ModelResponse(
                content=turn.get("text", ""),
                tool_calls=tool_calls,
                usage=usage,
                finish_reason=finish,
                api_retries=int(turn.get("api_retries", 0)),
                latency_ms=0.0,
            )
        return ModelResponse(
            content=turn.get("text", ""),
            tool_calls=(),
            usage=usage,
            finish_reason=turn.get("finish_reason", "stop"),
            api_retries=int(turn.get("api_retries", 0)),
            latency_ms=0.0,
        )


def make_scripted_client(
    script: dict[str, Sequence[dict]] | Sequence[dict],
) -> ScriptedClient:
    """Build the scripted client used by all non-live Layer-2 tests (§2, §8)."""
    return ScriptedClient(script)


# ---------------------------------------------------------------------------
# The real client (grounding §2 fallback: anthropic SDK behind our interface).
# Lazily imported so this module (and every test) loads with no ``anthropic``
# dependency and no key access. Used only by the skip-if-unconfigured live smoke.
# ---------------------------------------------------------------------------


class AnthropicModelClient:
    """``ModelClient`` implemented directly on the ``anthropic`` async SDK
    (grounding §2 fallback). Applies EXECUTION_CONSTANTS decoding/budget/timeout,
    counts transport retries with 2/4/8 s backoff (§1), and extracts per-call
    ``usage`` and ``stop_reason`` (§2, §7). The API key is read from
    ``ANTHROPIC_API_KEY`` only and never logged (§1, §9)."""

    def __init__(self, constants: ExecutionConstants = EXECUTION_CONSTANTS):
        # Lazy import: keep the module (and all tests) free of the dependency.
        from anthropic import AsyncAnthropic  # noqa: WPS433 (intentional lazy import)

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._c = constants
        self._client = AsyncAnthropic(api_key=api_key)  # key never stored elsewhere

    @staticmethod
    def _to_anthropic(messages: Sequence[Message]) -> tuple[str, list[dict]]:
        """Split our messages into (system_text, anthropic message list)."""
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
        # Canonical Anthropic shape (harness fix, ratified 2026-08-02): tool
        # results are user-role blocks, and a request must not carry consecutive
        # same-role messages — multi-call tool rounds and the timeout path
        # otherwise produce user-after-user.
        merged: list[dict] = []
        for msg in out:
            content = msg["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if merged and merged[-1]["role"] == msg["role"]:
                merged[-1]["content"].extend(content)
            else:
                merged.append({"role": msg["role"], "content": list(content)})
        return "\n\n".join(system_parts), merged

    async def create(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        tag: str = "",
    ) -> ModelResponse:
        import time

        system_text, msg_list = self._to_anthropic(messages)
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        kwargs: dict[str, Any] = {
            "model": self._c.model,
            "max_tokens": self._c.max_tokens,
            "messages": msg_list,
        }
        # §4.6 amendment (2026-08-02): the pinned model rejects specifying both
        # sampling parameters, so emit each only when set. top_p is unset (None).
        if self._c.temperature is not None:
            kwargs["temperature"] = self._c.temperature
        if self._c.top_p is not None:
            kwargs["top_p"] = self._c.top_p
        if system_text:
            kwargs["system"] = system_text
        if tool_defs:
            kwargs["tools"] = tool_defs

        retries = 0
        start = time.monotonic()
        while True:
            try:
                resp = await asyncio.wait_for(
                    self._client.messages.create(**kwargs),
                    timeout=self._c.timeout_s,
                )
                break
            except Exception as exc:  # noqa: BLE001 — classify transport-retryable
                if retries < self._c.api_transport_retries and _is_transient(exc):
                    backoff = self._c.api_backoff_s[
                        min(retries, len(self._c.api_backoff_s) - 1)
                    ]
                    retries += 1
                    await asyncio.sleep(backoff)
                    continue
                raise
        latency_ms = (time.monotonic() - start) * 1000.0

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
        usage = Usage(
            input_tokens=int(getattr(resp.usage, "input_tokens", 0)),
            output_tokens=int(getattr(resp.usage, "output_tokens", 0)),
        )
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason=getattr(resp, "stop_reason", "stop") or "stop",
            api_retries=retries,
            latency_ms=latency_ms,
        )


def _is_transient(exc: Exception) -> bool:
    """Classify an exception as transport-retryable (429/5xx/timeout) (§1)."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or (isinstance(status, int) and 500 <= status < 600)


def make_model_client(constants: ExecutionConstants = EXECUTION_CONSTANTS) -> ModelClient:
    """The real client (grounding §2). Lazily binds the ``anthropic`` SDK and the
    key; only the live smoke (§10) calls this."""
    return AnthropicModelClient(constants)
