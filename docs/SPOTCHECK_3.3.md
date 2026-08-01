# SPOTCHECK_3.3.md

**Verification record for paper §3.3 ("Ground truth and dataset").**
Re-executed 2026-08-01 against the frozen dataset at tag `part1-frozen`
(`oracle_commit e2d2bdd`, `dataset_sha256 3dc683ec…`, seed 42).

This document reconstructs and supersedes the 2026-07-28 spot-check record, which
was produced but never committed. **It is not a transcript of that run.** The
re-execution used an independently written derivation and reports what it found,
including two divergences the earlier record did not note. Where this document
and any recollection of the 2026-07-28 run disagree, this document governs,
because it is the one whose method is written down and repeatable.

---

## 1. What §3.3 requires

Paper §3.3 states that a stratified subset of cases — at least one per scenario
family, with additional coverage of reverse-charge eligibility, exemption
handling, B2B/B2C transitions, and mixed goods/services classification — is
manually spot-checked after generation, and that the check verifies *oracle
conformance to the bounded rule specification*, not model behaviour.

The check performed here is a strict superset: **every** case in both splits was
re-derived, not a stratified subset.

---

## 2. Method: clean-room re-derivation

A second implementation of the five subtask resolvers was written directly from
`docs/ORACLE_GROUNDING.md` §1 (bounded rule set), §2 (subtask logic and the
precedence rule) and §7 (dataset specification). Constraints:

- it imports **nothing** from `src/oracle/` — no `rules.py`, no `labeler.py`,
  no `validator.py`, no shared constants;
- the rate table, category vocabulary, jurisdiction routing and precedence
  ordering were transcribed from the prose of the grounding document, not from
  code;
- the only repo artefacts read are the frozen case JSON files.

Each stored `oracle_trace` was then compared field-by-field against the
re-derivation, at four levels: the per-case `jur` record, the per-line `cls`,
`rat`, `exm`, `rch` records, the per-line `final.lines` aggregation block, and
`final.total_vat_amount` / `final.jurisdiction`.

This tests whether the engine implements the document. It does not test whether
the document is a correct model of EU VAT law; that is out of scope for the
bounded harness and is disclosed in paper §10.1.

---

## 3. Scope covered

| Split | Cases | Line determinations | Multi-line cases |
|---|---:|---:|---:|
| eval | 40 | 54 | 10 |
| dev  |  8 | 11 |  2 |
| **total** | **48** | **65** | **12** |

Evaluation-set coverage of the decision space:

| Dimension | Distribution (eval) |
|---|---|
| `jur_path` | domestic 15, b2c_cross_border 14, intra_community_b2b 11 |
| jurisdiction | DE 13, ES 9, FR 9, IE 9 |
| category | GEN_SERVICE 14, EXEMPT_SUPPLY 11, GEN_GOODS 11, RED_GOODS 11, RED_SERVICE 7 |
| `rate_band` | standard 25, reduced 18, n/a (exempt) 11 |
| `outcome` | standard_charge 31, reverse_charge 12, exempt 11 |

All four scenario families, all four jurisdictions, all five categories, all
three `jur_path` values and all three terminal outcomes are exercised in the
evaluation split.

---

## 4. Result

**63 of 65 line determinations and all 48 case-level records agree exactly.**
All divergences reduce to a single field on a single branch.

| Level | Fields compared | Divergent |
|---|---|---|
| `jur` (per case) | jur_path, jurisdiction | 0 / 48 |
| `cls` (per line) | category | 0 / 65 |
| `rat` (per line) | rate, rate_band | 0 / 65 |
| `exm` (per line) | exempt | 0 / 65 |
| `rch` (per line) | outcome, reverse_charge, non_charging_reason, vat_amount | 0 / 65 |
| `rch` (per line) | **liable_party** | **11 / 65** |
| `final.lines` | full aggregation block | 11 / 65 (same field) |
| `final.total_vat_amount` | total | 0 / 48 |
| `final.jurisdiction` | jurisdiction | 0 / 48 |

Arithmetic, rounding, rate selection, jurisdiction routing, exemption detection,
reverse-charge routing and case-level aggregation all reproduce exactly.

### 4.1 Finding 1 — `liable_party` is under-specified for the exempt branch

`ORACLE_GROUNDING.md` §2.1 (RCH) enumerates four branches and states
`liable party` for exactly three of them: domestic → supplier, intra-community
B2B → customer, B2C cross-border → supplier. The **exempt** bullet specifies
`reverse_charge = false` and a non-charging reason, but is silent on
`liable_party`.

The engine emits `liable_party: "none"` for exempt lines. The clean-room
implementation, following the document alone, derived `"supplier"` as the
default. All 11 divergences are exactly this, on the 11 EXEMPT_SUPPLY lines.

Assessment: **the engine is right and the document is incomplete.** `"none"` is
in the schema enum (`["supplier", "customer", "none"]`), is semantically
correct — nobody is liable where nothing is charged — and is applied
consistently across all 11 lines. But it is not derivable from §2.1, and a
second implementer working from the grounding document alone will not produce
it. This is a specification gap, not a logic defect.

Confirmed by construction: re-running the clean-room script with its
`EXEMPT_LIABLE_PARTY` constant set to `"none"` drops the mismatch count from 22
to **0** across all 48 cases and 65 line determinations. This single field is
the entire divergence between the document and the engine.

It is not cosmetic. Paper §6.1 lists *liable party* among the scored
final-answer fields, and the worker prompts are assembled from the same
subtask instruction text that §2.1 governs. If the exempt branch does not state
the expected value, agents are being scored on a field the prompt does not
determine. **Action: add `liable party = none` to the exempt bullet in §2.1
before the sweep.** No code change, no re-freeze — the dataset already carries
the correct value.

### 4.2 Finding 2 — the EXEMPT-over-REVERSE_CHARGE precedence edge is unexercised

Zero lines in the corpus are `EXEMPT_SUPPLY` inside an `intra_community_b2b`
case — in the evaluation split *and* in the development split. The precedence
rule in §2.1 ("EXEMPT dominates REVERSE-CHARGE dominates STANDARD-CHARGE") is
therefore never tested by the dataset at its only contested edge.

This confirms and extends the finding recorded on 2026-07-28, which covered the
evaluation split only.

A constructed probe was run against the real engine to confirm the logic is
present:

```
case: supplier DE, customer FR, B2B, customer VAT-registered
  L1  EXEMPT_SUPPLY  → outcome=exempt          rc=False  liable=none      RC.EXEMPT.NO_CHARGE
  L2  GEN_GOODS      → outcome=reverse_charge  rc=True   liable=customer  RC.B2B.INTRA_EU
validate_trace: PASS
```

Exemption dominates, and the sibling line on the same case still routes to
reverse charge, so the precedence is applied per line rather than per case.
**The implementation is correct; the sampling is incomplete.** This remains a
disclosure item for §10.2, not a fix.

### 4.3 Finding 3 — the development split contains no exempt line

The eight development cases used for S0 prompt tuning (§4.5) contain **zero**
`EXEMPT_SUPPLY` lines and zero `exempt` outcomes. Category coverage is
GEN_GOODS 3, RED_GOODS 4, GEN_SERVICE 2, RED_SERVICE 2.

Exempt lines are 11 of 54 evaluation determinations (20%) and 11 of 40
evaluation cases involve one. S0 is therefore tuned without ever seeing the
branch that produces a fifth of the evaluation outcome distribution — including
the `rate: null` / `rate_band: null` shape and the `liable_party: "none"` value
from Finding 1, which no dev case can teach it.

This asymmetrically disadvantages S0, which is the paper's designated *strong*
comparator. It is not a defect in the frozen data — the eval set is untouched —
but it is a threat to the fairness claim in §4.5 and should be either disclosed
in §10.2 alongside the S0 tuning limitation, or handled by regenerating the dev
split with exempt coverage before tuning begins. Note the regeneration cost
precisely: `freeze_dataset.py` computes both manifest hashes over eval **and**
dev, so a dev refresh requires a re-freeze and changes `dataset_sha256` and
`case_files_sha256` — the values cited in the README and paper §6.7. The eval
case files and the `part1-frozen` tag are untouched, but the frozen-hash claim
must be re-issued and logged. Disclosure is therefore the schedule-safe default;
regeneration is the methodologically cleaner option if the integrity-chain
update is acceptable.

### 4.4 Finding 4 — JUR is resolved from the first line item (harmless)

`ORACLE_GROUNDING.md` §2.2 says that where a mixed case carries both goods and
service lines, JUR is computed per line "where the kind affects it".
`labeler.py` instead calls `resolve_jur(case, case.line_items[0].true_category)`
once per case.

Under the bounded rules this can never differ. Cross-border B2B routes goods to
the customer country by the intra-community rule and services to the customer
country by the general place-of-supply rule — the same answer. Domestic and B2C
cross-border ignore kind entirely. All 48 cases were checked for a non-unique
case-level jurisdiction under per-line derivation; none occurred.

Recorded for accuracy of the documentation, not as a defect. If the rule set is
ever widened (special schemes, distance selling, `XX`), this shortcut becomes
load-bearing and must be revisited.

---

## 5. Rule-reference key integrity

Each emitted `rule_reference` was checked against the semantic branch the
clean-room derivation assigned to that record. The mapping is single-valued —
one key per branch, no branch emitting two keys:

| Branch | Key |
|---|---|
| CLS (all) | `CLS.ASSIGNED` |
| JUR domestic | `JUR.DOMESTIC` |
| JUR intra_community_b2b | `JUR.INTRA_EU_B2B` |
| JUR b2c_cross_border | `JUR.B2C_CROSS_BORDER` |
| RAT standard | `RATE.STANDARD` |
| RAT reduced | `RATE.REDUCED` |
| RAT n/a | `RATE.NA_EXEMPT` |
| EXM exempt=false | `EXM.NONE` |
| EXM exempt=true | `EXM.EXEMPT_SUPPLY` |
| RCH standard_charge, domestic | `RC.DOMESTIC.SUPPLIER_CHARGES` |
| RCH standard_charge, b2c | `RC.B2C.SUPPLIER_CHARGES` |
| RCH reverse_charge, intra-EU B2B | `RC.B2B.INTRA_EU` |
| RCH exempt (any path) | `RC.EXEMPT.NO_CHARGE` |

`RC.EXEMPT.NO_CHARGE` is the one key shared across two paths (domestic-exempt
and b2c-exempt). That is correct rather than a collision: exemption collapses
the path distinction by the precedence rule, so both paths *should* cite the
same rule. The third path — exempt inside intra-community B2B — does not appear
in the corpus, per Finding 2; the probe in §4.2 confirms it also emits
`RC.EXEMPT.NO_CHARGE`.

---

## 6. What this establishes

- The frozen dataset's oracle labels conform to `ORACLE_GROUNDING.md` §1–§2 on
  every field except `liable_party` on the exempt branch, where the document is
  silent and the engine is correct.
- Arithmetic, rounding and aggregation are exact across 65 line determinations
  and 48 case totals.
- Rule-reference keys are single-valued per semantic branch.
- Two sampling gaps exist: the EXEMPT-over-REVERSE_CHARGE precedence edge
  (whole corpus) and exempt coverage in the development split.

## 7. What this does not establish

- That the bounded rule set models real VAT law. Out of scope; see §10.1.
- That the generator's descriptions are non-trivially classifiable. The CLS
  signal is near-free by construction and is frozen as such; see
  `HARNESS_GROUNDING_1_SURFACE.md` §1.3.
- Anything about agent behaviour. No model was invoked.

---

## 8. Actions arising

| # | Action | Blocking? |
|---|---|---|
| 1 | Add `liable party = none` to the exempt bullet, `ORACLE_GROUNDING.md` §2.1 | Applied alongside this record (same commit) |
| 2 | Disclose the precedence coverage gap in paper §10.2 | Yes — before submission |
| 3 | Decide on Finding 3: disclose in §10.2, or regenerate the dev split with exempt coverage | Yes — before S0 tuning |
| 4 | Optionally align §2.2's per-line JUR wording with the implementation | No |

None of these require re-freezing the evaluation set. The `part1-frozen` tag,
`dataset_sha256` and `case_files_sha256` remain valid.

---

*Reproduce: the clean-room script is `scripts/spotcheck_cleanroom.py`; run it
from the repository root with the frozen dataset present. It exits non-zero if
any field diverges. The §4.1 divergence was found with the script's
document-derived reading (`EXEMPT_LIABLE_PARTY = "supplier"`); the committed
script sets the constant to `"none"`, matching the amended §2.1 (action 1),
and runs green against the frozen dataset. Reverting the constant to
`"supplier"` reproduces the 22 mismatches.*
