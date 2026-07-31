"""test_tools.py — the four tools F, logging, and the tool outage seam
(grounding §4, §7.2, §8, §10).

Gate boxes: all four tools deterministic, closed-set-enforcing, structured-error,
logging, zero restated tables (imports from rules.py only); injection seams
present as no-ops, logged, covered by tests.
"""

from __future__ import annotations

import pytest

from src.harness import tools
from src.harness.tools import (
    InjectionController,
    RULE_TEXT,
    SEAM_RATE_TABLE_OUTAGE,
    ToolContext,
    ToolLog,
    classification_reference,
    rate_table_lookup,
    rule_citation_retrieval,
    using_context,
    vat_registration_check,
)
from src.oracle import generator
from src.oracle.rules import CATEGORY_TABLE, RATE_TABLE, RULE_KEYS, Category, Jurisdiction


@pytest.fixture
def ds():
    return generator.generate_dataset(seed=42)


# ---------------------------------------------------------------------------
# §4.1 classification_reference.
# ---------------------------------------------------------------------------


class TestClassificationReference:
    def test_returns_closed_vocab_with_kinds(self):
        result = classification_reference()
        assert set(result) == {"categories"}
        got = {c["category"]: c["kind"] for c in result["categories"]}
        assert set(got) == {c.value for c in Category}
        for cat in Category:
            assert got[cat.value] == CATEGORY_TABLE[cat]["kind"].value

    def test_no_rate_or_exemption_info_leaked(self):
        for c in classification_reference()["categories"]:
            assert set(c) == {"category", "kind"}

    def test_deterministic(self):
        assert classification_reference() == classification_reference()


# ---------------------------------------------------------------------------
# §4.2 vat_registration_check — the only case-keyed tool.
# ---------------------------------------------------------------------------


class TestVatRegistrationCheck:
    def test_known_case_returns_input_field(self, ds):
        for case in list(ds.eval_cases) + list(ds.dev_cases):
            result = vat_registration_check(case.case_id)
            assert set(result) == {"case_id", "customer_vat_registered"}
            assert result["case_id"] == case.case_id
            assert result["customer_vat_registered"] == case.customer_vat_registered

    def test_unknown_case(self):
        assert vat_registration_check("nope_999") == {"error": "UNKNOWN_CASE"}

    def test_registry_holds_only_bare_booleans(self):
        # The cache must be {case_id: bool} exactly — never the input block (which
        # carries the CLS label true_category). Values are bare booleans.
        registry = tools._case_registry()
        assert registry, "registry unexpectedly empty"
        for case_id, value in registry.items():
            assert isinstance(case_id, str)
            assert type(value) is bool  # not a dict, not truthy-coerced


# ---------------------------------------------------------------------------
# §4.3 rate_table_lookup — eight real rows only; exempt/unknown -> NO_SUCH_ENTRY.
# ---------------------------------------------------------------------------


class TestRateTableLookup:
    def test_eight_real_rows(self):
        for jur in Jurisdiction:
            for band in ("standard", "reduced"):
                result = rate_table_lookup(jur.value, band)
                assert set(result) == {"jurisdiction", "band", "rate"}
                assert result["rate"] == RATE_TABLE[jur][band]

    def test_exempt_band_not_served(self):
        assert rate_table_lookup("DE", "exempt") == {"error": "NO_SUCH_ENTRY"}

    def test_null_band_not_served(self):
        assert rate_table_lookup("DE", None) == {"error": "NO_SUCH_ENTRY"}

    def test_unknown_jurisdiction(self):
        assert rate_table_lookup("XX", "standard") == {"error": "NO_SUCH_ENTRY"}

    def test_never_raises_on_junk(self):
        # structured error, never an exception into agent context
        assert "error" in rate_table_lookup("", "")
        assert "error" in rate_table_lookup("de", "STANDARD")  # case-sensitive closed set


# ---------------------------------------------------------------------------
# §4.4 rule_citation_retrieval — closed 13-key set; general statutory text.
# ---------------------------------------------------------------------------


class TestRuleCitationRetrieval:
    def test_rule_text_keys_equal_closed_set(self):
        assert set(RULE_TEXT) == set(RULE_KEYS)
        assert len(RULE_KEYS) == 13

    def test_every_key_returns_text(self):
        for key in RULE_KEYS:
            result = rule_citation_retrieval(key)
            assert set(result) == {"rule_key", "text"}
            assert result["rule_key"] == key
            assert isinstance(result["text"], str) and result["text"].strip()

    def test_unknown_key(self):
        assert rule_citation_retrieval("NOT.A.KEY") == {"error": "UNKNOWN_RULE_KEY"}

    def test_texts_describe_conditions_not_specific_cases(self):
        # No case identifiers should appear in the general rule texts (§4.4).
        for text in RULE_TEXT.values():
            assert "eval_" not in text and "dev_" not in text


# ---------------------------------------------------------------------------
# §7.2 tool logging — every invocation recorded in call order.
# ---------------------------------------------------------------------------


class TestLogging:
    def test_no_logging_without_context(self):
        # default null context: calls succeed and record nothing
        log = ToolLog()
        classification_reference()
        assert log.tool_invocations == []

    def test_logs_every_invocation_in_order(self):
        log = ToolLog()
        with using_context(ToolContext(log=log)):
            classification_reference()
            rate_table_lookup("DE", "standard")
            rule_citation_retrieval("JUR.DOMESTIC")
            vat_registration_check("nope_999")
        names = [inv["tool"] for inv in log.tool_invocations]
        assert names == [
            "classification_reference",
            "rate_table_lookup",
            "rule_citation_retrieval",
            "vat_registration_check",
        ]
        # arguments + results captured
        assert log.tool_invocations[1]["arguments"] == {"jurisdiction": "DE", "band": "standard"}
        assert log.tool_invocations[3]["result"] == {"error": "UNKNOWN_CASE"}

    def test_context_restored_after_block(self):
        log = ToolLog()
        with using_context(ToolContext(log=log)):
            pass
        # back to the null context — no logging
        rate_table_lookup("FR", "reduced")
        assert log.tool_invocations == []


# ---------------------------------------------------------------------------
# §8 injection seams — no-op defaults + the rate_table_lookup outage seam.
# ---------------------------------------------------------------------------


class _OutageOnce(InjectionController):
    """Layer-3-style controller stub for the m=1 transient outage: the first
    query for the armed case fires, subsequent ones recover."""

    def __init__(self, armed_case_id: str):
        self._armed = armed_case_id
        self._fired = False

    def rate_outage(self, case_id):
        if case_id == self._armed and not self._fired:
            self._fired = True
            return True
        return False


class TestInjectionSeams:
    def test_default_controller_is_noop(self):
        c = InjectionController()
        assert c.worker_timeout("eval_001", "RCH") is False
        assert c.hallucinate("eval_001", "CLS", {}) is None
        assert c.rate_outage("eval_001") is False

    def test_rate_outage_default_serves_normally(self):
        # no context / default controller -> normal lookup
        assert rate_table_lookup("DE", "standard")["rate"] == RATE_TABLE[Jurisdiction.DE]["standard"]

    def test_rate_outage_first_call_fails_then_recovers(self):
        log = ToolLog()
        ctx = ToolContext(log=log, active_case_id="eval_007", injection=_OutageOnce("eval_007"))
        with using_context(ctx):
            first = rate_table_lookup("DE", "standard")
            second = rate_table_lookup("DE", "standard")
        assert first == {"error": "TOOL_UNAVAILABLE"}
        assert second["rate"] == RATE_TABLE[Jurisdiction.DE]["standard"]
        # exactly one injection event, logged, marked fired, tagged RAT
        assert len(log.injection_events) == 1
        ev = log.injection_events[0]
        assert ev["seam"] == SEAM_RATE_TABLE_OUTAGE
        assert ev["case_id"] == "eval_007" and ev["subtask"] == "RAT" and ev["fired"] is True
        # both attempts recorded as tool invocations, in order
        assert [i["result"] for i in log.tool_invocations] == [first, second]

    def test_outage_only_fires_for_armed_case(self):
        log = ToolLog()
        ctx = ToolContext(log=log, active_case_id="eval_002", injection=_OutageOnce("eval_007"))
        with using_context(ctx):
            result = rate_table_lookup("DE", "standard")
        assert "rate" in result
        assert log.injection_events == []
