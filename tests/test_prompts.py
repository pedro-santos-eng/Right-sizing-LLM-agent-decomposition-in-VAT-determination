"""test_prompts.py — prompt assembly is a pure function of the slice, contracts
are generated from the frozen schema $defs, and hashes are stable / uniform
across conditions (grounding HARNESS_GROUNDING_2_ORCHESTRATION §4, §10).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.harness.prompts import (
    all_partition_prompt_hashes,
    assemble_prompt,
    output_contract,
    prompt_hashes,
    worker_id,
)
from src.harness.surface import PARTITIONS, slice_for

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPurity:
    def test_identical_slice_identical_prompt(self):
        # The {RAT,EXM,RCH} worker is byte-identical between C2 and C3 (§4).
        grp = frozenset({"RAT", "EXM", "RCH"})
        assert grp in PARTITIONS["C2"] and grp in PARTITIONS["C3"]
        p1 = assemble_prompt(slice_for(grp))
        p2 = assemble_prompt(slice_for(grp))
        assert p1 == p2

    def test_shared_slice_hash_matches_across_conditions(self):
        # No per-condition tuning: the shared slice yields the same hash in C2/C3.
        grp = frozenset({"RAT", "EXM", "RCH"})
        h2 = prompt_hashes("C2")[worker_id("C2", grp)]
        h3 = prompt_hashes("C3")[worker_id("C3", grp)]
        assert h2 == h3

    def test_c1_slice_full_view_prompt(self):
        prompt = assemble_prompt(slice_for(frozenset({"CLS", "JUR", "RAT", "EXM", "RCH"})))
        # C1 owns everything; the prompt names all five subtasks.
        for t in ("CLS", "JUR", "RAT", "EXM", "RCH"):
            assert t in prompt

    def test_exemption_table_only_for_exm_owner(self):
        with_exm = assemble_prompt(slice_for(frozenset({"EXM"})))
        without_exm = assemble_prompt(slice_for(frozenset({"RAT"})))
        assert "EXEMPTION TABLE" in with_exm
        assert "EXEMPTION TABLE" not in without_exm


class TestGeneratedContracts:
    def test_contract_generated_from_schema_enums(self):
        # The RCH contract must carry the schema's outcome enum values verbatim,
        # proving it is GENERATED from $defs, not hand-copied (§4, §10).
        schema = json.loads(
            (_REPO_ROOT / "src" / "schemas" / "final_trace.schema.json").read_text("utf-8")
        )
        outcomes = schema["$defs"]["rch"]["properties"]["decision"]["properties"]["outcome"]["enum"]
        contract = output_contract("RCH")
        for value in outcomes:
            assert json.dumps(value) in contract

    def test_jur_contract_has_jur_path_enum(self):
        contract = output_contract("JUR")
        assert "intra_community_b2b" in contract and "domestic" in contract


class TestHashStabilityAcrossProcesses:
    def test_hashes_stable_in_fresh_interpreter(self):
        probe = (
            "import json;"
            "from src.harness.prompts import all_partition_prompt_hashes;"
            "print(json.dumps(all_partition_prompt_hashes()))"
        )
        env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
        out = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(_REPO_ROOT), env=env,
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        other = json.loads(out.stdout.strip().splitlines()[-1])
        assert other == all_partition_prompt_hashes()
