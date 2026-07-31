"""runlog.py — the run-record schema, writer, reader, and validator (grounding §7.2).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_1_SURFACE.md (v1.1).

One run record per (condition, case_id, repeat). Layer 1 defines the schema,
writer, reader, and validator; Layers 2-4 fill the fields. Required content
(§7.2): identity (with oracle_commit + dataset_sha256 echoed from the manifest),
per-worker events, tool invocations in call order, validation verdicts + the
final validate_trace outcome, injection events (no-op markers in Layer 1), and
accounting placeholders (tokens / latency, populated later).

Wall-clock timestamps live ONLY in the log envelope, never in agent-visible or
trace content (§7.2). ``write_run_record`` stamps the envelope; nothing else in
Layer 1 reads the clock.

This module is not an agent-context module; it imports only ``surface`` (for the
condition / subtask / seam enums) — which reaches no label source.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from src.harness.surface import CONDITIONS, SUBTASKS
from src.harness.tools import (
    SEAM_HALLUCINATED_OUTPUT,
    SEAM_RATE_TABLE_OUTAGE,
    SEAM_WORKER_TIMEOUT,
)

SCHEMA_VERSION = "1"

TERMINAL_STATUSES = ("ok", "validation_exhausted", "timeout", "no_trace")
SEAMS = (SEAM_WORKER_TIMEOUT, SEAM_HALLUCINATED_OUTPUT, SEAM_RATE_TABLE_OUTAGE)


# ---------------------------------------------------------------------------
# The run-record JSON Schema. Embedded (not a file): grounding §9 lists only
# schemas/agent_case_view.schema.json under schemas/, and §11 forbids creating
# other schema files. Top level is strict; nested event objects are extensible
# (additionalProperties true) so Layers 2-4 can add tokens/latency/etc.
# ---------------------------------------------------------------------------

RUN_RECORD_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://harness.local/schemas/run_record.schema.json",
    "title": "Harness run record (one per condition x case x repeat)",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "identity",
        "workers",
        "tool_invocations",
        "validation",
        "injection_events",
        "accounting",
        "envelope",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "identity": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "condition",
                "case_id",
                "repeat",
                "oracle_commit",
                "dataset_sha256",
            ],
            "properties": {
                "condition": {"enum": list(CONDITIONS)},
                "case_id": {"type": "string"},
                "repeat": {"type": "integer", "minimum": 0},
                "oracle_commit": {"type": "string"},
                "dataset_sha256": {"type": "string"},
            },
        },
        "workers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["worker_id", "assigned", "terminal_status"],
                "properties": {
                    "worker_id": {"type": "string"},
                    "assigned": {"type": "array", "items": {"enum": list(SUBTASKS)}},
                    "dispatches": {"type": "integer", "minimum": 0},
                    "retries": {"type": "integer", "minimum": 0},
                    "retry_verdicts": {"type": "array"},
                    "terminal_status": {"enum": list(TERMINAL_STATUSES)},
                },
            },
        },
        "tool_invocations": {
            "type": "array",
            "description": "In call order (§7.2).",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["tool", "arguments", "result"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                    "result": {"type": "object"},
                },
            },
        },
        "validation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["record_verdicts", "final"],
            "properties": {
                "record_verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["subtask", "accepted"],
                        "properties": {
                            "subtask": {"enum": list(SUBTASKS)},
                            "accepted": {"type": "boolean"},
                            "failed_checks": {"type": "array", "items": {"type": "string"}},
                            "deferred_checks": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "final": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ok", "failed_checks"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "failed_checks": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "injection_events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["seam", "fired"],
                "properties": {
                    "seam": {"enum": list(SEAMS)},
                    "case_id": {"type": ["string", "null"]},
                    "subtask": {"type": ["string", "null"]},
                    "fired": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
        "accounting": {
            "type": "object",
            "additionalProperties": True,
            "required": ["token_counts", "latency_ms"],
            "properties": {
                "token_counts": {"type": "object"},
                "latency_ms": {"type": ["number", "null"]},
            },
        },
        "envelope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["written_at_utc"],
            "properties": {
                # The ONLY wall-clock value in a run record (§7.2). null until
                # write_run_record stamps it.
                "written_at_utc": {"type": ["string", "null"]},
            },
        },
    },
}


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(RUN_RECORD_SCHEMA)


@dataclass(frozen=True)
class RunRecordValidation:
    ok: bool
    errors: tuple[str, ...]


def new_run_record(
    condition: str,
    case_id: str,
    repeat: int,
    oracle_commit: str,
    dataset_sha256: str,
) -> dict:
    """A skeleton run record: identity filled, everything else empty/placeholder.
    Layers 2-4 populate workers / tool_invocations / validation / injection /
    accounting. The envelope timestamp is stamped at write time."""
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "condition": condition,
            "case_id": case_id,
            "repeat": repeat,
            "oracle_commit": oracle_commit,
            "dataset_sha256": dataset_sha256,
        },
        "workers": [],
        "tool_invocations": [],
        "validation": {"record_verdicts": [], "final": {"ok": False, "failed_checks": []}},
        "injection_events": [],
        "accounting": {"token_counts": {}, "latency_ms": None},
        "envelope": {"written_at_utc": None},
    }


def injection_event(
    seam: str,
    case_id: str | None = None,
    subtask: str | None = None,
    fired: bool = False,
    note: str = "",
) -> dict:
    """Build an injection-event marker (§8). Layer 1 emits no-op markers
    (fired=False by default)."""
    if seam not in SEAMS:
        raise ValueError(f"unknown seam {seam!r}")
    return {"seam": seam, "case_id": case_id, "subtask": subtask, "fired": fired, "note": note}


def validate_run_record(record: Any) -> RunRecordValidation:
    """Validate a run record against RUN_RECORD_SCHEMA."""
    errors = sorted(_validator().iter_errors(record), key=lambda e: list(e.path))
    messages = tuple(
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
    )
    return RunRecordValidation(ok=not messages, errors=messages)


def _now_utc() -> str:
    """The single clock read in Layer 1 — envelope only (§7.2)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_run_record(record: dict, path: str | Path) -> None:
    """Validate, stamp the envelope timestamp if unset, and write pretty JSON.

    Raises ValueError if the record does not conform, so malformed records fail
    loudly rather than being written silently."""
    result = validate_run_record(record)
    if not result.ok:
        raise ValueError(f"run record failed schema: {result.errors}")
    if record.get("envelope", {}).get("written_at_utc") is None:
        record.setdefault("envelope", {})["written_at_utc"] = _now_utc()
    Path(path).write_text(
        json.dumps(record, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def read_run_record(path: str | Path) -> dict:
    """Read and validate a run record from disk."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    result = validate_run_record(record)
    if not result.ok:
        raise ValueError(f"run record on disk failed schema: {result.errors}")
    return record
