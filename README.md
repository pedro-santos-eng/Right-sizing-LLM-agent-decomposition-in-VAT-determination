# Right-Sizing LLM Agent Decomposition in VAT Determination

Deterministic VAT oracle and frozen synthetic dataset — **Part 1** of the artifact
for the pilot study *"Right-Sizing Agent Decomposition in VAT Determination: A
Pilot Controlled Sweep"* (in preparation).

**Status.** Part 1 (oracle + dataset) is complete and frozen at tag
`part1-frozen`. Part 2 — the multi-agent experiment harness (Layers 1–4:
activity surface, orchestration, injection, sweep/analysis) — is implemented in
this repository; Phase 0 (pre-flight gates) is closed. Measured phases (main
sweep, injection arms, matched-token variants) are pending.

## Contents

| Path | Purpose |
| --- | --- |
| `src/oracle/rules.py` | Bounded EU-style VAT rule set: tables, rule-reference keys, the five subtask resolvers (CLS, JUR, RAT, EXM, RCH), precedence |
| `src/oracle/generator.py` | Seeded synthetic case generation — stratified, dev/eval disjoint by construction |
| `src/oracle/labeler.py` | Composes the rules over a case → full oracle trace (final + intermediate labels) |
| `src/oracle/validator.py` | Structural/consistency checks over any emitted trace |
| `src/oracle/scorer.py` | Emitted trace vs. oracle labels → accuracy, step accuracy, earliest-error subtask |
| `src/schemas/final_trace.schema.json` | Single consolidated JSON Schema; per-record types live as `$defs` |
| `data/` | Frozen dataset: 40 evaluation + 8 development cases, each with its full oracle trace, plus `MANIFEST.json` |
| `scripts/freeze_dataset.py` | Dataset freeze and integrity verification |
| `docs/ORACLE_GROUNDING.md` | Source-of-truth specification for everything above |
| `docs/DEVLOG.md` | Development log, including the freeze record |
| `tests/` | 27 tests covering determinism, dataset stratification, rules, validation, and scoring |

## Pinned integrity chain

The dataset is reproducible by construction. The pinned identifiers are:

| Field | Value |
| --- | --- |
| Seed | `42` |
| Oracle commit (`oracle_commit`) | `e2d2bdd22b85ea2915e3d719d7c12c6f18eac577` — tag `part1-frozen` |
| `dataset_sha256` (input cases) | `3dc683ec418666fa2e8823a2ea622bfd90f638254377d83fe95d5247563e599e` |
| `case_files_sha256` (inputs + oracle traces) | `3472544ffcd1434d59427c912ba5c77a8294de0fb675ba7d32f1572c5e410302` |
| Frozen at (UTC) | `2026-05-29T16:53:24Z` |

The authoritative record is [`data/MANIFEST.json`](data/MANIFEST.json), written by
the freeze script with `oracle_commit_dirty: false`.

## Reproducing the verification

Requirements: Python ≥ 3.12.

```bash
pip install -e ".[test]"

python -m pytest -q                        # expected: 27 passed
python -m scripts.freeze_dataset --verify  # expected: VERIFY OK
```

`--verify` regenerates the full dataset from the manifest's seed, recomputes both
SHA-256 hashes, compares every frozen file byte-by-byte, and exits non-zero on any
drift. Run it before using the dataset for anything official: it is the check that
catches "someone changed `rules.py` and silently broke label reproducibility."

Determinism contract (summary): a single seed fully determines the generated
cases and all labels; generation uses one explicit seeded RNG, no wall clock, no
environment-dependent values, and stable JSON serialization, so re-running with
the same seed is byte-identical. Full statement in
[`docs/ORACLE_GROUNDING.md`](docs/ORACLE_GROUNDING.md), §6.

## Scope

The rule set is a bounded, simplified EU-style VAT model built for controlled
experimentation: four jurisdictions, a closed category vocabulary, and fixed
precedence (exempt > reverse charge > standard). It is synthetic ground truth for
measuring agent behavior — it does not implement any jurisdiction's actual VAT
law and is not tax advice.