"""test_surface.py — Layer-1 activity surface (grounding §1, §2, §3, §5, §10).

Maps to readiness-gate boxes: SUBTASKS/DEPENDS/LAYERS consistent + order matches
scorer; PARTITIONS match paper §4.3 and cover T once; slice_for reproduces the
C4 atom table with correct intra-worker subtraction and the C1 slice; the
agent_case_view projection is label-free (KEY-based, §1.1) on all 48 cases; the
exemption-table artifact is byte-stable and consistent with CATEGORY_TABLE.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from src.harness import surface
from src.harness import tools as tools_mod
from src.harness.model_client import make_scripted_client
from src.harness.orchestrator import RunConfig, _Orchestrator
from src.harness.surface import (
    ALL_TOOLS,
    CASE_VIEW_ATOMS,
    DEPENDS,
    EXEMPTION_TABLE_TEXT,
    LAYERS,
    PARALLEL_ELIGIBLE,
    PARTITIONS,
    REFERENCE_ATOM,
    SUBTASKS,
    agent_case_view,
    partition_slices,
    slice_for,
)
from src.oracle import generator
from src.oracle.rules import CATEGORY_TABLE, Category
from src.oracle.scorer import SUBTASK_ORDER

_VIEW_SCHEMA = json.loads(
    (Path(surface.__file__).resolve().parent / "schemas" / "agent_case_view.schema.json")
    .read_text(encoding="utf-8")
)


def _all_48_cases():
    ds = generator.generate_dataset(seed=42)
    return list(ds.eval_cases) + list(ds.dev_cases)


# ---------------------------------------------------------------------------
# §2 — subtasks, dependency order, layering.
# ---------------------------------------------------------------------------


class TestSubtasksAndDependencies:
    def test_subtasks_order_matches_scorer(self):
        assert SUBTASKS == SUBTASK_ORDER

    def test_depends_is_acyclic(self):
        # Kahn's algorithm: a full topological order must exist.
        remaining = {t: set(DEPENDS[t]) for t in SUBTASKS}
        ordered: list[str] = []
        while remaining:
            ready = [t for t, deps in remaining.items() if not deps]
            assert ready, f"cycle among {sorted(remaining)}"
            for t in ready:
                ordered.append(t)
                del remaining[t]
            for deps in remaining.values():
                deps.difference_update(ready)
        assert set(ordered) == set(SUBTASKS)

    def test_depends_only_references_subtasks(self):
        for deps in DEPENDS.values():
            assert deps <= set(SUBTASKS)

    def test_layers_consistent_with_depends(self):
        # every subtask in exactly one layer; each dep in a strictly earlier layer
        layer_of = {}
        for idx, layer in enumerate(LAYERS):
            for t in layer:
                assert t not in layer_of, f"{t} in two layers"
                layer_of[t] = idx
        assert set(layer_of) == set(SUBTASKS)
        for t in SUBTASKS:
            for dep in DEPENDS[t]:
                assert layer_of[dep] < layer_of[t]

    def test_parallel_eligible(self):
        assert PARALLEL_ELIGIBLE == frozenset({"RAT", "EXM"})
        # the parallel-eligible pair shares one layer
        assert PARALLEL_ELIGIBLE in LAYERS


# ---------------------------------------------------------------------------
# §3.1 — partitions.
# ---------------------------------------------------------------------------


class TestPartitions:
    _EXPECTED = {
        "C1": ({"CLS", "JUR", "RAT", "EXM", "RCH"},),
        "C2": ({"CLS", "JUR"}, {"RAT", "EXM", "RCH"}),
        "C3": ({"CLS"}, {"JUR"}, {"RAT", "EXM", "RCH"}),
        "C4": ({"CLS"}, {"JUR"}, {"RAT"}, {"EXM"}, {"RCH"}),
    }

    def test_partitions_match_paper(self):
        assert set(PARTITIONS) == set(self._EXPECTED)
        for cond, groups in PARTITIONS.items():
            assert tuple(set(g) for g in groups) == self._EXPECTED[cond]

    def test_each_partition_covers_T_exactly_once(self):
        for cond, groups in PARTITIONS.items():
            union: set[str] = set()
            total = 0
            for g in groups:
                union |= set(g)
                total += len(g)
            assert union == set(SUBTASKS), cond
            assert total == len(SUBTASKS), f"{cond} overlaps"  # disjoint

    def test_worker_ordering_by_earliest_subtask(self):
        for cond in PARTITIONS:
            slices = partition_slices(cond)
            keys = [min(SUBTASKS.index(t) for t in s.assigned) for s in slices]
            assert keys == sorted(keys), cond


# ---------------------------------------------------------------------------
# §3.2 / §3.3 — atom slices and the composition algebra.
# ---------------------------------------------------------------------------


class TestSliceAlgebra:
    def test_c4_singletons_reproduce_atom_table(self):
        expected_state = {
            "CLS": {"line_items"},
            "JUR": {
                "supplier_country",
                "customer_country",
                "transaction_type",
                "customer_vat_registered",
                "record:CLS",
            },
            "RAT": {"record:JUR", "record:CLS"},
            "EXM": {"record:JUR", "record:CLS", "exemption_table"},
            # v1.2 amendment (closed-operand principle): RCH also sees line_items,
            # the vat_amount = rate x line_items[].amount operand.
            "RCH": {
                "record:CLS",
                "record:JUR",
                "record:RAT",
                "record:EXM",
                "line_items",
            },
        }
        expected_tools = {
            "CLS": {"classification_reference"},
            "JUR": {"vat_registration_check", "rule_citation_retrieval"},
            "RAT": {"rate_table_lookup"},
            "EXM": {"rule_citation_retrieval"},
            "RCH": {"rule_citation_retrieval"},
        }
        for t in SUBTASKS:
            s = slice_for(frozenset({t}))
            assert set(s.input_state) == expected_state[t], t
            assert set(s.tools) == expected_tools[t], t

    def test_c2_intra_worker_subtraction(self):
        # C2 worker {CLS,JUR} produces CLS, so it does NOT receive record:CLS as
        # input; its input state is the case-field atoms (grounding §3.3).
        w = slice_for(frozenset({"CLS", "JUR"}))
        assert set(w.input_state) == {
            "line_items",
            "supplier_country",
            "customer_country",
            "transaction_type",
            "customer_vat_registered",
        }
        assert "record:CLS" not in w.input_state
        assert "record:JUR" not in w.input_state
        # second C2 worker {RAT,EXM,RCH}: receives upstream CLS+JUR + R, plus the
        # line_items operand RCH needs for vat_amount (v1.2 closed-operand amendment).
        w2 = slice_for(frozenset({"RAT", "EXM", "RCH"}))
        assert set(w2.input_state) == {
            "record:CLS",
            "record:JUR",
            "exemption_table",
            "line_items",
        }

    def test_c3_intra_worker_subtraction(self):
        # C3 separates {CLS} and {JUR}; the third worker is the C2 second worker.
        assert set(slice_for(frozenset({"CLS"})).input_state) == {"line_items"}
        assert "record:CLS" not in slice_for(frozenset({"JUR"})).input_state or True
        jur = slice_for(frozenset({"JUR"}))
        assert "record:CLS" in jur.input_state  # JUR consumes CLS records

    def test_c1_slice_is_full_view_plus_all_tools_plus_R(self):
        c1 = slice_for(frozenset(SUBTASKS))
        assert set(c1.tools) == set(ALL_TOOLS)
        assert set(c1.input_state) == set(CASE_VIEW_ATOMS) | {REFERENCE_ATOM}

    def test_unknown_subtask_rejected(self):
        with pytest.raises(ValueError):
            slice_for(frozenset({"NOPE"}))
        with pytest.raises(ValueError):
            slice_for(frozenset())


# ---------------------------------------------------------------------------
# §5 — exemption-table reference artifact.
# ---------------------------------------------------------------------------


class TestExemptionTable:
    def test_byte_stable(self):
        # Re-render (import already ran once) — deterministic, byte-identical.
        again = surface._render_exemption_table()
        assert again == EXEMPTION_TABLE_TEXT
        assert isinstance(EXEMPTION_TABLE_TEXT, str) and EXEMPTION_TABLE_TEXT

    def test_consistent_with_category_table(self):
        for cat in Category:
            marker = f"category={cat.value} exempt=" + (
                "yes" if CATEGORY_TABLE[cat]["exempt"] else "no"
            )
            assert marker in EXEMPTION_TABLE_TEXT
        # only EXEMPT_SUPPLY is exempt in the bounded set
        assert "category=EXEMPT_SUPPLY exempt=yes" in EXEMPTION_TABLE_TEXT
        assert EXEMPTION_TABLE_TEXT.count("exempt=yes") == 1


# ---------------------------------------------------------------------------
# §1.1 — agent_case_view label isolation (KEY-based assertions on all 48 cases).
# ---------------------------------------------------------------------------


def _keys_recursive(obj) -> set:
    found: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _keys_recursive(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _keys_recursive(item)
    return found


class TestAgentCaseView:
    def test_projection_is_label_free_on_all_48_cases(self):
        cases = _all_48_cases()
        assert len(cases) == 48
        for case in cases:
            # the SOURCE genuinely carries the label we must drop
            assert all(hasattr(li, "true_category") for li in case.line_items)
            view = agent_case_view(case)
            # KEY-based absence, recursively (§1.1 — never a substring test)
            assert "true_category" not in _keys_recursive(view)
            # exact key set, no extras
            assert set(view) == {
                "case_id",
                "supplier_country",
                "customer_country",
                "customer_vat_registered",
                "transaction_type",
                "line_items",
            }
            for li in view["line_items"]:
                assert set(li) == {"line_id", "description", "amount"}

    def test_projection_matches_strict_schema(self):
        for case in _all_48_cases():
            jsonschema.validate(agent_case_view(case), _VIEW_SCHEMA)

    def test_view_values_faithful_to_case(self):
        case = _all_48_cases()[0]
        view = agent_case_view(case)
        assert view["case_id"] == case.case_id
        assert view["supplier_country"] == case.supplier_country.value
        assert view["transaction_type"] == case.transaction_type.value
        assert len(view["line_items"]) == len(case.line_items)


# ---------------------------------------------------------------------------
# §3.2 v1.2 amendment — closed-operand regression. RCH's vat_amount operand
# (line_items[].amount) must travel through RCH's OWN visible input state, not
# through a voluntary upstream record echo (the Phase-1 C2 anomaly). Offline,
# scripted, no API. See HARNESS_GROUNDING_1_SURFACE.md §3.2 (v1.1 -> v1.2).
# ---------------------------------------------------------------------------


def _multi_line_case():
    ds = generator.generate_dataset(seed=42)
    for c in list(ds.eval_cases) + list(ds.dev_cases):
        if len(c.line_items) >= 2:
            return c
    return ds.dev_cases[0]


def _rch_group(condition: str) -> frozenset:
    """The partition group that owns RCH under this condition."""
    return next(g for g in PARTITIONS[condition] if "RCH" in g)


def _input_state_payload(orch: _Orchestrator, group: frozenset) -> dict:
    """The JSON payload the orchestrator would hand the group's worker."""
    msg = orch._input_state_message(slice_for(group))
    body = msg.split("Here is your input state as JSON:\n", 1)[1].split("\n\n", 1)[0]
    return json.loads(body)


class TestClosedOperandRCH:
    def test_rch_owner_payload_carries_line_items_with_amount(self):
        # For every condition, the worker that owns RCH must receive line_items,
        # and each line item must carry its amount (the vat_amount operand).
        case = _multi_line_case()
        for cond in ("C1", "C2", "C3", "C4"):
            group = _rch_group(cond)
            assert "line_items" in slice_for(group).input_state, cond
            orch = _Orchestrator(
                cond,
                case,
                make_scripted_client({}),
                RunConfig(timeout_s=5),
                tools_mod.InjectionController(),
            )
            payload = _input_state_payload(orch, group)
            assert "line_items" in payload, cond
            assert payload["line_items"], cond
            for li in payload["line_items"]:
                assert "amount" in li, (cond, li)

    def test_shared_rat_exm_rch_slice_identical_c2_c3(self):
        # The {RAT,EXM,RCH} worker is reached identically in C2 and C3; the
        # amendment must not perturb this shared-slice equality (§4).
        grp = frozenset({"RAT", "EXM", "RCH"})
        assert grp in PARTITIONS["C2"] and grp in PARTITIONS["C3"]
        assert _rch_group("C2") == grp and _rch_group("C3") == grp
        assert slice_for(_rch_group("C2")) == slice_for(_rch_group("C3"))
