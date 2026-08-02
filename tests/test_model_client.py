"""test_model_client.py — EXECUTION_CONSTANTS, the scripted client, and payload
extraction (grounding HARNESS_GROUNDING_2_ORCHESTRATION §1, §2, §5).
"""

from __future__ import annotations

import asyncio

import pytest

from src.harness.agents import extract_payload
from src.harness.model_client import (
    EXECUTION_CONSTANTS,
    Message,
    ToolCall,
    make_scripted_client,
)


# --- §1 EXECUTION_CONSTANTS frozen + complete ------------------------------


class TestExecutionConstants:
    def test_pinned_values(self):
        c = EXECUTION_CONSTANTS
        assert c.model == "claude-haiku-4-5-20251001"
        assert c.temperature == 0.2
        assert c.top_p is None  # §4.6 amended 2026-08-02: top_p unset (echoed null)
        assert c.max_tokens == 4096
        assert c.timeout_s == 120
        assert c.case_wall_cap_s == 1200
        assert c.subtask_retry_budget == 3
        assert c.s0_trace_repair_budget == 3
        assert c.api_transport_retries == 3
        assert c.api_backoff_s == (2, 4, 8)
        assert c.seed is None

    def test_as_dict_is_json_serialisable_and_stable(self):
        import json

        d = EXECUTION_CONSTANTS.as_dict()
        assert json.loads(json.dumps(d)) == d
        # echoed field set is complete (grounding §1 table)
        assert set(d) == {
            "model", "temperature", "top_p", "max_tokens", "timeout_s",
            "case_wall_cap_s", "subtask_retry_budget", "s0_trace_repair_budget",
            "api_transport_retries", "api_backoff_s", "seed",
        }

    def test_frozen_immutable(self):
        with pytest.raises(Exception):
            EXECUTION_CONSTANTS.temperature = 0.9  # type: ignore[misc]


# --- §2 scripted client -----------------------------------------------------


class TestScriptedClient:
    def test_replays_text_and_usage(self):
        client = make_scripted_client(
            {"w": [{"text": "hello", "usage": {"input_tokens": 7, "output_tokens": 3}}]}
        )
        resp = asyncio.run(client.create([Message("user", "hi")], [], tag="w"))
        assert resp.content == "hello"
        assert resp.usage.input_tokens == 7 and resp.usage.output_tokens == 3
        assert resp.usage.total == 10
        assert resp.finish_reason == "stop"

    def test_tool_call_turn(self):
        client = make_scripted_client(
            {"w": [{"tool_calls": [{"name": "rate_table_lookup",
                                    "arguments": {"jurisdiction": "DE", "band": "standard"}}]}]}
        )
        resp = asyncio.run(client.create([Message("user", "hi")], [], tag="w"))
        assert resp.finish_reason == "tool_calls"
        assert resp.tool_calls[0].name == "rate_table_lookup"
        assert resp.tool_calls[0].arguments == {"jurisdiction": "DE", "band": "standard"}

    def test_per_tag_queues_are_independent(self):
        client = make_scripted_client(
            {"a": [{"text": "A1"}, {"text": "A2"}], "b": [{"text": "B1"}]}
        )
        r1 = asyncio.run(client.create([], [], tag="a"))
        r2 = asyncio.run(client.create([], [], tag="b"))
        r3 = asyncio.run(client.create([], [], tag="a"))
        assert (r1.content, r2.content, r3.content) == ("A1", "B1", "A2")

    def test_raise_timeout(self):
        client = make_scripted_client({"w": [{"raise_timeout": True}]})
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(client.create([], [], tag="w"))

    def test_exhausted_script_raises(self):
        from src.harness.model_client import ScriptExhausted

        client = make_scripted_client({"w": []})
        with pytest.raises(ScriptExhausted):
            asyncio.run(client.create([], [], tag="w"))


# --- §5 payload extraction (last fenced json block) ------------------------


class TestExtraction:
    def test_extracts_last_json_block(self):
        text = '```json\n{"a": 1}\n```\nthen\n```json\n{"b": 2}\n```'
        payload, err = extract_payload(text)
        assert err is None and payload == {"b": 2}

    def test_no_block_is_structured_error(self):
        payload, err = extract_payload("no code fence here")
        assert payload is None and err.startswith("payload:")

    def test_unparsable_json_is_structured_error(self):
        payload, err = extract_payload("```json\n{not valid}\n```")
        assert payload is None and "invalid json" in err

    def test_non_object_payload_rejected(self):
        payload, err = extract_payload("```json\n[1,2,3]\n```")
        assert payload is None and "not an object" in err
