"""freeze_dataset.py — freeze the 40 eval + 8 dev cases to disk.

SOURCE OF TRUTH: docs/ORACLE_GROUNDING.md (§6 determinism, §7 dataset, §9 gate).

Runs ``generate_dataset(seed)`` and ``label_case`` over the full dataset, then
writes:

  data/MANIFEST.json            — pinned seed, oracle commit hash, dataset
                                  SHA-256, per-family counts, generation time
  data/eval_cases/eval_NNN.json — one canonical JSON per eval case (input + oracle trace)
  data/dev_cases/dev_NNN.json   — one canonical JSON per dev case (input + oracle trace)

The manifest is what downstream consumers (harness, analysis) read to know
which dataset they're running against. The per-case files are the actual data.

Usage:
    python -m scripts.freeze_dataset                    # freeze with default seed 42
    python -m scripts.freeze_dataset --seed 42          # explicit seed
    python -m scripts.freeze_dataset --verify           # regenerate and compare against on-disk
    python -m scripts.freeze_dataset --allow-dirty      # freeze even with uncommitted changes

Refuses to freeze if the git working tree is dirty (the commit hash would not
identify the code that produced the dataset). Use ``--allow-dirty`` to override
for local experimentation; never use it in CI or for an official freeze.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.oracle import generator as g
from src.oracle import labeler, validator

# Paths are resolved relative to the repository root (the parent of scripts/).
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
EVAL_DIR = DATA_DIR / "eval_cases"
DEV_DIR = DATA_DIR / "dev_cases"
MANIFEST_PATH = DATA_DIR / "MANIFEST.json"

DEFAULT_SEED = 42


# ---------------------------------------------------------------------------
# Canonical serialization. Single source of truth for "the same dataset."
# ---------------------------------------------------------------------------


def _canonical_json(obj) -> str:
    """Stable JSON: sorted keys, ASCII, compact separators. Byte-identical for
    identical content (matches generator.to_canonical_json conventions)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _pretty_json(obj) -> str:
    """Human-readable JSON for on-disk files. Stable: sorted keys, 2-space indent.
    Byte-identical for identical content, just easier to diff than compact form."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


def _step_to_dict(record) -> dict:
    return {
        "subtask": record.subtask,
        "decision": dict(record.decision),
        "support": dict(record.support),
        "rule_reference": record.rule_reference,
    }


def _case_record(case, trace) -> dict:
    """One frozen case = the input case + the oracle trace. Self-contained so
    the file is meaningful without re-running the generator."""
    return {
        "case_id": case.case_id,
        "input": g.case_to_dict(case),
        "oracle_trace": validator.trace_to_emitted(trace),
    }


# ---------------------------------------------------------------------------
# Git inspection. Pure read; no commits or pushes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitState:
    commit_hash: str       # "unversioned" if not in a git repo
    is_dirty: bool


def _git_state() -> GitState:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).decode()
        return GitState(commit_hash=commit, is_dirty=bool(status.strip()))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return GitState(commit_hash="unversioned", is_dirty=False)


# ---------------------------------------------------------------------------
# Build the dataset payload (in-memory) before writing or verifying.
# ---------------------------------------------------------------------------


def _build(seed: int) -> tuple[dict, dict[str, dict]]:
    """Generate + label + validate the full dataset. Returns (manifest_core, case_files).

    Raises if any oracle trace fails validation — a sanity check enforcing
    §9's "every case has a complete oracle label" gate at freeze time.
    """
    dataset = g.generate_dataset(seed=seed)

    case_files: dict[str, dict] = {}
    eval_family_counts: Counter = Counter()
    dev_family_counts: Counter = Counter()

    for c in dataset.eval_cases:
        trace = labeler.label_case(c)
        result = validator.validate_trace(validator.trace_to_emitted(trace))
        if not result.ok:
            raise RuntimeError(f"oracle trace failed validation for {c.case_id}: {result.failed_checks}")
        rel = f"eval_cases/{c.case_id}.json"
        case_files[rel] = _case_record(c, trace)
        eval_family_counts[g.family_of(c)] += 1

    for c in dataset.dev_cases:
        trace = labeler.label_case(c)
        result = validator.validate_trace(validator.trace_to_emitted(trace))
        if not result.ok:
            raise RuntimeError(f"oracle trace failed validation for {c.case_id}: {result.failed_checks}")
        rel = f"dev_cases/{c.case_id}.json"
        case_files[rel] = _case_record(c, trace)
        dev_family_counts[g.family_of(c)] += 1

    # The dataset fingerprint: hash of the canonical JSON of the *generated*
    # dataset (input cases only — matches generator.to_canonical_json).
    dataset_sha256 = hashlib.sha256(g.to_canonical_json(dataset).encode("utf-8")).hexdigest()

    # A second hash over the *case files* (input + oracle trace, the actual on-disk content).
    case_files_sha256 = hashlib.sha256(
        _canonical_json({k: case_files[k] for k in sorted(case_files)}).encode("utf-8")
    ).hexdigest()

    manifest_core = {
        "seed": seed,
        "n_eval_cases": len(dataset.eval_cases),
        "n_dev_cases": len(dataset.dev_cases),
        "eval_family_counts": dict(eval_family_counts),
        "dev_family_counts": dict(dev_family_counts),
        "dataset_sha256": dataset_sha256,
        "case_files_sha256": case_files_sha256,
    }
    return manifest_core, case_files


# ---------------------------------------------------------------------------
# Freeze: write to disk.
# ---------------------------------------------------------------------------


def freeze(seed: int, allow_dirty: bool) -> int:
    git = _git_state()
    if git.is_dirty and not allow_dirty:
        print(
            "ERROR: git working tree is dirty. The commit hash would not identify\n"
            "the code that produced this dataset. Commit your changes first, or\n"
            "pass --allow-dirty for local experimentation (never for an official freeze).",
            file=sys.stderr,
        )
        return 2

    manifest_core, case_files = _build(seed)

    DATA_DIR.mkdir(exist_ok=True)
    EVAL_DIR.mkdir(exist_ok=True)
    DEV_DIR.mkdir(exist_ok=True)

    for rel, payload in case_files.items():
        (DATA_DIR / rel).write_text(_pretty_json(payload), encoding="utf-8")

    manifest = {
        **manifest_core,
        "oracle_commit": git.commit_hash,
        "oracle_commit_dirty": git.is_dirty,
        "frozen_at_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_path": "src/schemas/final_trace.schema.json",
        "grounding_doc": "docs/ORACLE_GROUNDING.md",
    }
    MANIFEST_PATH.write_text(_pretty_json(manifest), encoding="utf-8")

    print(f"Froze {manifest_core['n_eval_cases']} eval + {manifest_core['n_dev_cases']} dev cases")
    print(f"  seed:              {seed}")
    print(f"  oracle_commit:     {git.commit_hash}{' (DIRTY)' if git.is_dirty else ''}")
    print(f"  dataset_sha256:    {manifest_core['dataset_sha256']}")
    print(f"  case_files_sha256: {manifest_core['case_files_sha256']}")
    print(f"  manifest:          {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return 0


# ---------------------------------------------------------------------------
# Verify: regenerate and compare against on-disk freeze.
# ---------------------------------------------------------------------------


def verify() -> int:
    if not MANIFEST_PATH.exists():
        print(f"ERROR: no manifest at {MANIFEST_PATH}; run freeze first.", file=sys.stderr)
        return 2

    on_disk_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    seed = on_disk_manifest["seed"]

    manifest_core, case_files = _build(seed)

    problems: list[str] = []

    if manifest_core["dataset_sha256"] != on_disk_manifest["dataset_sha256"]:
        problems.append(
            f"dataset_sha256 differs: regenerated {manifest_core['dataset_sha256']!r} "
            f"vs frozen {on_disk_manifest['dataset_sha256']!r}"
        )
    if manifest_core["case_files_sha256"] != on_disk_manifest["case_files_sha256"]:
        problems.append(
            f"case_files_sha256 differs: regenerated {manifest_core['case_files_sha256']!r} "
            f"vs frozen {on_disk_manifest['case_files_sha256']!r}"
        )

    # Per-file byte check too (catches any drift the hashes don't isolate).
    for rel, payload in case_files.items():
        path = DATA_DIR / rel
        if not path.exists():
            problems.append(f"missing file: {rel}")
            continue
        if path.read_text(encoding="utf-8") != _pretty_json(payload):
            problems.append(f"content differs: {rel}")

    if problems:
        print("VERIFY FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("VERIFY OK")
    print(f"  seed:              {seed}")
    print(f"  oracle_commit:     {on_disk_manifest['oracle_commit']}")
    print(f"  dataset_sha256:    {manifest_core['dataset_sha256']}")
    print(f"  case_files_sha256: {manifest_core['case_files_sha256']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"RNG seed for generator.generate_dataset (default {DEFAULT_SEED})")
    p.add_argument("--verify", action="store_true",
                   help="regenerate and compare against the on-disk freeze; do not write")
    p.add_argument("--allow-dirty", action="store_true",
                   help="freeze even if git working tree is dirty (never for official freezes)")
    args = p.parse_args(argv)

    if args.verify:
        return verify()
    return freeze(seed=args.seed, allow_dirty=args.allow_dirty)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # stdout was closed (e.g. piped into `head`); exit cleanly.
        sys.exit(0)