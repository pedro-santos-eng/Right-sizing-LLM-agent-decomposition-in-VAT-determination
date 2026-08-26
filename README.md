# Right-Sizing LLM-Agent Decomposition in VAT Determination — A Pilot Controlled Sweep

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22083282.svg)](https://doi.org/10.5281/zenodo.22083282) [![arXiv](https://img.shields.io/badge/arXiv-2608.23395-b31b1b.svg)](https://arxiv.org/abs/2608.23395)

**Paper:** [Right-Sizing LLM-Agent Decomposition in VAT Determination: A Pilot Controlled Sweep](https://arxiv.org/abs/2608.23395) — arXiv:2608.23395 (cs.MA; cross-listed cs.AI, cs.SE)

This repository is the complete artifact for a pilot controlled sweep that measures how the
*granularity* of LLM-agent task decomposition affects final-answer accuracy, token cost,
latency, and failure localization on a bounded, synthetic EU-style VAT-determination task. It
ships a deterministic VAT **oracle**, a **frozen synthetic dataset**, the measurement
**harness** (activity surface → orchestration → fault injection → sweep), the **raw run traces**
of every one of the 4,400 runs, and the **analysis pipeline** that regenerates every table in
the paper directly from those traces.

> The workload is synthetic ground truth built for controlled experimentation (four
> jurisdictions, a closed category vocabulary, fixed precedence). It does **not** implement any
> jurisdiction's actual VAT law and is **not** tax advice.

## Directory map

| Path | Purpose |
| --- | --- |
| `src/oracle/` | Deterministic VAT oracle: `rules.py` (bounded rule set + the five subtask resolvers CLS·JUR·RAT·EXM·RCH), `generator.py` (seeded, stratified, dev/eval-disjoint case generation), `labeler.py`, `validator.py`, `scorer.py` |
| `src/harness/` | Experiment harness — Layer 1 activity `surface.py`, Layer 2 `orchestrator.py` + `agents.py` + `model_client.py` + `s0.py`, Layer 3 `injection.py`, plus `tools.py`, `prompts.py`, `runlog.py`, `validation.py` |
| `src/schemas/`, `src/harness/schemas/` | JSON Schemas for the final trace and the agent case view |
| `scripts/` | `sweep.py`/`run_one.py` (runner), `score_runs.py` (raw → `scored.csv`), `analyze.py` (`scored.csv` → tables), `freeze_dataset.py` (dataset/oracle integrity), `generate_injection_plan.py`, `tune_s0prime.py`, `sweep_common.py` |
| `data/` | Frozen dataset: 40 evaluation + 8 development cases with full oracle traces, `MANIFEST.json`, `price_sheet.json`, `injection_plan.json` — **CC-BY-4.0** (`data/LICENSE`) |
| `results/` | Raw run traces (`raw/phase<N>/<mode>/<condition>/<case>/r<repeat>.json`), the scored table (`scored.csv`), and the paper tables (`analysis/*.csv`); see `results/README.md` — **CC-BY-4.0** |
| `docs/` | `ORACLE_GROUNDING.md`, the four `HARNESS_GROUNDING_*` specs, `DEVLOG.md` |
| `tests/` | Determinism, stratification, rules, validation, scoring, and analysis-primitive tests |

**Licensing split.** Code (`src/`, `scripts/`, `tests/`, config) is **Apache-2.0** (top-level
`LICENSE`). Data and traces (`data/`, `results/`) are **CC-BY-4.0** (`data/LICENSE`). This split
is intentional and is the authoritative statement of terms.

## Regenerate every paper table from the committed traces

Everything the paper reports is a deterministic function of the committed raw traces. In a fresh
environment:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# THE one command — raw traces -> scored.csv -> every analysis table:
python -m scripts.score_runs && python -m scripts.analyze
```

`scripts.score_runs` reads `results/raw/**/r*.json`, scores each run against the oracle, and
writes `results/scored.csv` (one row per run). `scripts.analyze` reads that CSV and writes the
paper tables to `results/analysis/`: `main_table.csv`, `error_types.csv`, `s0_family.csv`,
`headline_contrasts.csv`, `supplementary_contrasts.csv`, `injection_cells.csv`,
`injection_deltas.csv`, `falsification.csv`, and `case_level.csv`. (PowerShell: replace `&&`
with `;`.)

This artifact contains **no figure-generation code**; the paper's figures are drawn by hand from
these numerical tables.

## Frozen integrity chain (oracle + dataset)

The dataset is reproducible by construction from a single seed. Verify it with:

```bash
python -m scripts.freeze_dataset --verify        # expected: VERIFY OK
```

which regenerates the dataset from the seed, recomputes both hashes, and compares every frozen
file byte-for-byte. The pinned identifiers (authoritative copy in `data/MANIFEST.json`):

| Field | Value |
| --- | --- |
| Seed | `42` |
| Oracle commit (`oracle_commit`) | `e2d2bdd22b85ea2915e3d719d7c12c6f18eac577` (tag `part1-frozen`) |
| `dataset_sha256` (input cases) | `3dc683ec418666fa2e8823a2ea622bfd90f638254377d83fe95d5247563e599e` |
| `case_files_sha256` (inputs + oracle traces) | `3472544ffcd1434d59427c912ba5c77a8294de0fb675ba7d32f1572c5e410302` |
| Frozen at (UTC) | `2026-05-29T16:53:24Z` |

## Two levels of replicability

This artifact separates two distinct claims:

1. **Re-analysis — deterministic and guaranteed by the released traces.** Running
   `score_runs` + `analyze` over the committed `results/raw/**` reproduces every reported number
   exactly. It touches no network and calls no model. Determinism is fixed by the pinned
   `numpy`/`pandas` in `requirements.txt` and the seeds hard-coded in `scripts/analyze.py`
   (`bootstrap_seed 20260805`, `permutation_seed 20260806`, the §7/§8 case-clustered bootstrap
   `seed 42`; 1,000 resamples each). This is the reproducibility this repository *guarantees*.

2. **Re-execution — depends on an external model, not guaranteed.** Regenerating the raw traces
   themselves means re-running the sweep (`python -m scripts.sweep --phase <N>`), which calls the
   live model **`claude-haiku-4-5-20251001`** through the `anthropic`/`autogen` stack (see
   `pyproject.toml`). There is **no decoding seed**, so re-execution is *not* bit-reproducible and
   depends on that pinned model remaining available and behaving consistently. Treat re-execution
   as an approximate replication, not a deterministic one.

The full frozen measurement environment (all transitive dependencies) is recorded in
`env_server.txt` and `results/env_gex44_phase1.txt`; `requirements.txt` is the minimal subset the
offline analysis pipeline imports.

## Study design in one paragraph

The sweep contrasts a monolithic single-agent baseline (**S0**) against four orchestrated
decompositions of increasing granularity (**C1–C4**) on 40 evaluation cases with R=5 repeats
(1,000 main runs), plus two matched-token S0′ variants (2×200) that hold token budget constant to
separate a decomposition effect from a prompt-budget effect, and a fault-injection battery
(3,000 runs: timeout, outage, hallucination) — **4,400 runs** total. The pre-registered
falsification criteria and the exact statistical family are specified in
`docs/HARNESS_GROUNDING_4_SWEEP.md` and rendered mechanically by `scripts/analyze.py`.

## Requirements

Python ≥ 3.10 (measured on 3.12.3). For the analysis pipeline: `pip install -r requirements.txt`.
For re-execution and the test suite, install the package with its extras:
`pip install -e ".[test,analysis]"`.
