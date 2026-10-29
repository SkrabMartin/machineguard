# MachineGuard

**Industrial Predictive Maintenance & Decision Intelligence Platform**

MachineGuard is a Python-centered engineering platform for analyzing industrial telemetry and supporting maintenance decisions. The project aims to combine sensor data quality checks, anomaly detection, carefully evaluated failure-risk modeling, remaining useful life (RUL) research, historical replay, and maintenance scenario analysis. It is an engineering and data science project, not a validated industrial safety system.

## Problem and objectives

Industrial telemetry can contain gaps, duplicate measurements, changing operating states, and uncertain fault records. A useful maintenance tool must preserve data provenance, explain why an alert appeared, and distinguish a deviation score from a calibrated failure probability.

MachineGuard's objectives are to create reproducible data and model workflows, evaluate alerts against available event evidence, quantify uncertainty where the data supports it, and present transparent maintenance scenarios for human review. The platform is designed to run locally without paid cloud services.

## Capabilities and status

| Capability | Status | Scope |
|---|---|---|
| Python package, source contracts, and provenance | **Implemented** | Typed contracts for MetroPT-3 and classic C-MAPSS, SHA-256 file fingerprints, source-manifest serialization, and a small CLI. |
| MetroPT CSV/Parquet ingestion and data quality | **Implemented** | Strict source-schema checks, bounded monthly Parquet writes, quality summaries, source hashes, and idempotent versions. |
| Causal MetroPT features and descriptive audit | **Implemented** | Fourteen trailing, timestamp-available features, state and gap summaries, a versioned feature dataset, and chronological anomaly splits. |
| Statistical anomaly baselines | **Implemented, experimental** | Train-only state-aware physical guardrails and robust MAD scores, three-row alert persistence, and descriptive report-window evaluation. |
| Multivariate anomaly comparison | **Implemented, experimental** | Two state-conditional Isolation Forests, train-only scaling, fixed contamination, event-window sensitivity, and comparison with Phase 4 baselines. |
| Failure-risk feasibility audit | **Implemented** | Source-bound coverage audit for four candidate horizons; current verdict is **risk unavailable**. |
| Failure-risk modeling | **Unavailable, conditional future work** | Only if event labels support a defined horizon and held-out calibration. |
| C-MAPSS RUL benchmark | **Implemented, experimental** | Four separate simulated-engine subsets, strict ZIP parser, engine-group validation, median-lifetime baseline, Ridge regression, and terminal-cycle evaluation. |
| Local experiment registry | **Implemented, research only** | Verifies Phase 4, 5, and 7 artifacts; catalogs input/code hashes and metrics; compares compatible runs; reproduces or reports blockers. No model promotion. |
| Deterministic local replay | **Implemented, experimental** | Verified MetroPT event-time replay, bounded lateness, duplicate quarantine, checkpoint/resume, optional Phase 4 scoring, and batch parity inspection. |
| Hypothetical maintenance scenarios | **Implemented, experimental** | Strict assumption-only JSON, decimal cost/sensitivity arithmetic, bounded exact crew allocation, and an explicit unavailable model-probability gate. |
| API and dashboard | **Planned** | No network interface or GUI exists yet. |
| Research artifacts | **Local only** | Fitted statistics and model comparisons are reproducible from verified inputs; no executable model artifact is published. |

Phases 1–10 are complete. The CLI can ingest MetroPT-shaped CSV or Parquet data, verify derived versions, build trailing features, fit and score exploratory anomaly experiments, audit fault-report evidence, run a separate C-MAPSS cycle-based RUL benchmark, catalog/reproduce local research experiments, replay MetroPT telemetry, and evaluate explicitly hypothetical maintenance scenarios. The Phase 6 gate found no defensible basis for a calibrated MetroPT failure probability; no risk model was trained. There is no GUI. No dataset bytes or trained models are included.

## Architecture overview

The implemented ingest keeps raw sources outside the repository, validates them against the MetroPT contract, records quality findings, and writes fingerprinted monthly Parquet partitions through a staging directory. A completed version is published with a manifest and output hashes. Original timestamps are preserved as timezone-unknown source time. The feature builder uses current and past observations only, records lookback and minimum sample rules, and materializes a source-bound chronological train/validation/test split with one-hour purge gaps. The statistical and multivariate anomaly experiments fit only on the training split and score fixed validation and test periods. The risk audit examines telemetry coverage around uncertain report anchors and records missing evidence, without creating labels or probabilities. A separate C-MAPSS pipeline parses simulated engine trajectories and evaluates RUL in operational cycles, with no transfer claim to MetroPT. The local replay engine reuses causal features and statistical scoring, preserves event-time order, and checkpoints state without a broker. The separate scenario evaluator accepts only hypothetical probability and cost assumptions; model-driven economics still require qualified calibrated horizon probabilities. A future API and dashboard will expose evidence, provenance, and uncertainty.

## Technology overview

The implemented stack uses Python 3.12+, uv for locking, Hatchling for builds, PyArrow 26 for bounded Parquet I/O, standard-library streaming statistics and rolling state, NumPy 2 and scikit-learn 1.9 for the anomaly comparison and RUL baseline, pytest, Ruff, and mypy. Python's standard CSV reader performs strict MetroPT parsing; the standard ZIP reader parses C-MAPSS text directly. The local experiment catalog and replay checkpoints use standard-library JSON and SHA-256; MLflow and a message broker are deferred. Polars, FastAPI, React, TypeScript, Plotly, PostgreSQL, Docker, and other tools remain candidates for later phases when their requirements are established.

## Repository structure

The following is the **target local development workspace architecture**, not a listing of files already implemented. `PHASES.md`, `docs/`, and `experiments/` are internal materials maintained locally and intentionally excluded from the public GitHub repository. Other uncreated directories represent future work.

```text
machineguard/
├── README.md
├── PHASES.md
├── pyproject.toml
├── uv.lock
├── .gitignore
├── .env.example
├── docs/
│   ├── PROJECT_SPECIFICATION.md
│   ├── ARCHITECTURE.md
│   ├── TECH_STACK.md
│   ├── DATASETS.md
│   ├── ML_METHODOLOGY.md
│   ├── DECISIONS.md
│   ├── CHANGELOG.md
│   ├── CURRENT_STATE.md
│   └── completion_reports/
├── configs/
├── data/
│   ├── README.md
│   └── fixtures/
├── src/
│   └── machineguard/
│       ├── core/
│       ├── ingestion/
│       ├── quality/
│       ├── features/
│       ├── anomaly/
│       ├── risk/
│       ├── prognostics/
│       ├── replay/
│       ├── optimization/
│       ├── evaluation/
│       ├── storage/
│       └── api/
├── experiments/
├── scripts/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── frontend/
└── .github/
    └── workflows/
```

`src/machineguard/` now contains core contracts plus MetroPT ingestion, quality, storage, feature, anomaly, event-evaluation, risk-feasibility, separate C-MAPSS prognostics, experiment registry, replay, and hypothetical optimization modules; `tests/` contains unit and integration verification; `data/fixtures/` may contain small, clearly labeled synthetic test data; `configs/` contains safe policies and a synthetic maintenance scenario; `scripts/` is planned; `frontend/` is a future interface; `.github/workflows/` contains CI. Raw datasets, generated models, and internal experiment records are not committed by default.

The **currently published implementation** contains `README.md`, `LICENSE`, `.gitignore`, `pyproject.toml`, `uv.lock`, safe `configs/` policies, `.github/workflows/ci.yml`, `src/machineguard/` (CLI, core, ingestion, quality, storage, features, anomaly, event evaluation, risk feasibility, prognostics, local experiment registry, replay, and hypothetical optimization), and `tests/unit/` plus `tests/integration/`. Private planning files remain available in the local workspace only. The target tree above includes future modules that do not yet exist.

## Installation, development, and testing

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or 3.13, then run:

```bash
uv sync --locked --dev
uv run --locked machineguard --help
uv run --locked machineguard contracts metropt-3-uci
uv run --locked machineguard contracts nasa-cmapss-classic
uv run --locked machineguard sha256 /path/to/local/file
```

`contracts` prints the observed source layout and unresolved timezone/label policy. `sha256` reads a local file and prints its fingerprint; it does not upload data.

To ingest a **locally obtained MetroPT-3 source CSV** (or an equivalent raw-layout Parquet file), record its original HTTPS source URL, license status, and actual UTC retrieval time. Keep both the source and manifest under ignored `data/external/`:

```bash
mkdir -p data/external
uv run --locked machineguard source-manifest data/external/MetroPT3.csv \
  --source-url 'https://archive.ics.uci.edu/static/public/791/metropt+3+dataset.zip' \
  --license-status 'CC BY 4.0 per UCI listing' \
  --retrieved-at-utc 'REPLACE_WITH_ACTUAL_ISO_UTC_TIME' \
  > data/external/metropt-source.manifest.json
uv run --locked machineguard ingest-metropt data/external/MetroPT3.csv \
  --source-manifest data/external/metropt-source.manifest.json \
  --output-root data/processed
uv run --locked machineguard inspect-dataset data/processed/metropt-3-uci/VERSION_ID
```

Replace the timestamp and version ID with actual values; `ingest-metropt` prints the version path. The importer rejects malformed rows and source-hash mismatches, reports gaps/duplicates/plausibility warnings without silently repairing data, and leaves the original file untouched. Parquet input must use the exact observed MetroPT raw column names and compatible types. The output is not UTC-normalized because the source timezone is unknown. Derived data stays under ignored `data/processed/`.

To build and verify Phase 3 features from a canonical MetroPT version:

```bash
uv run --locked machineguard build-features data/processed/metropt-3-uci/VERSION_ID \
  --output-root data/processed/features
uv run --locked machineguard inspect-features data/processed/features/metropt-3-uci/FEATURE_VERSION_ID
```

The default feature specification and split configuration are bound to the verified UCI CSV source (`ab41beab7b3046f4e657e4c7`). For a different source version, supply a reviewed `--split` JSON matching its version and source SHA-256; `--spec` and `--report-windows` can also be supplied explicitly. The output contains monthly Parquet parts, a manifest, and `eda.json`. A feature value is available after the current sample arrives. The split is for descriptive and anomaly work only; source fault reports are contextual intervals, not row-level labels. Derived data remains outside Git.

The verified source produced 445,298 training rows (February 1–April 1), 514,945 validation rows (April 1 01:00–June 15), 556,335 test rows (June 15 01:00–September 1 04:00), and 370 purge rows. Report windows fall 0/3/1 across train/validation/test. All times are source-local with an unresolved timezone; the four report windows are too few to establish calibrated failure risk.

To fit the exploratory Phase 4 baselines on the verified feature version and inspect the local artifact:

```bash
uv run --locked machineguard fit-anomaly-baselines \
  data/processed/features/metropt-3-uci/FEATURE_VERSION_ID \
  --output-root data/processed/baselines
uv run --locked machineguard inspect-anomaly-baselines \
  data/processed/baselines/metropt-3-uci/BASELINE_VERSION_ID
```

The default baseline policy is bound to feature version `946d147b94b2f288cfda6c37`; supply a reviewed `--policy` JSON for another version. The local artifact records training medians/MADs, assumed physical limits, fixed thresholds, signal contributions, and validation/test alert summaries. A three-row persistence rule resets on state changes or sampling gaps. Report-window coincidences and off-report alert rates are descriptive proxies because the source has no precise row-level fault labels. No alert score is a failure probability. On the verified source, the fixed robust rule produced about 57 and 81 off-report episodes per observed day in validation and test, dominated by motor current; this is an exploratory negative result, not a useful deployed detector. The physical guardrails produced fewer episodes but coincided with none of the four reported intervals.

To reproduce the Phase 5 state-conditional Isolation Forest comparison locally:

```bash
uv run --locked machineguard fit-isolation-forest \
  data/processed/features/metropt-3-uci/FEATURE_VERSION_ID \
  --baseline-version data/processed/baselines/metropt-3-uci/BASELINE_VERSION_ID \
  --output-root data/processed/models
uv run --locked machineguard inspect-isolation-forest \
  data/processed/models/metropt-3-uci/MODEL_VERSION_ID
```

The default policy binds the verified Phase 3 feature version and Phase 4 baseline version. The command refits two forests from train rows with a fixed seed and compares validation/test alerts using the Phase 4 persistence and observed-time rules. It records model metadata and evaluation locally; no executable model object is serialized or committed. The predeclared 1% training contamination is an alert-threshold assumption, not fault prevalence. The model's signed decision score is not a failure probability.

To reproduce the Phase 6 **no-model** evidence gate on the verified feature version:

```bash
uv run --locked machineguard audit-risk-feasibility \
  data/processed/features/metropt-3-uci/FEATURE_VERSION_ID \
  --output data/processed/risk/metropt-risk-feasibility.json
uv run --locked machineguard inspect-risk-audit \
  data/processed/risk/metropt-risk-feasibility.json \
  --feature-version data/processed/features/metropt-3-uci/FEATURE_VERSION_ID
```

The default policy binds feature version `946d147b94b2f288cfda6c37` and audits 1-hour, 6-hour, 24-hour, and 7-day hypothetical report-anchor coverage. The output stays local and ignored. Four source intervals fall across 0/3/1 anomaly train/validation/test folds. Onset and intervention chronology, event independence, and verified negative periods remain unresolved. The existing anomaly split has only a one-hour purge and cannot serve as a risk split. Coverage counts are not target labels, prevalence, or calibration metrics. The resulting risk status is `risk_unavailable` for every candidate horizon.

To reproduce the separate Phase 7 C-MAPSS benchmark, obtain NASA PCoE entry 6 and keep the extracted inner `CMAPSSData.zip` under ignored `data/external/`. The default policy requires its verified SHA-256; a different archive requires a reviewed policy and provenance record.

```bash
uv run --locked machineguard sha256 data/external/CMAPSSData.zip
uv run --locked machineguard run-cmapss-benchmark data/external/CMAPSSData.zip \
  --output-root data/processed/phase7_rul
uv run --locked machineguard inspect-cmapss-benchmark \
  data/processed/phase7_rul/nasa-cmapss-classic/VERSION_ID
```

The fixed experiment splits NASA training engines into fit and validation groups, evaluates each validation engine at a deterministic observed prefix, and opens publisher test truth only after fitting and validation. It compares a median-lifetime baseline with train-standardized Ridge regression; the target is **uncapped remaining operational cycles**. A full local run on the verified archive produced these publisher-test results (one prediction per engine):

| Subset | Engines | Baseline MAE / RMSE (cycles) | Ridge MAE / RMSE (cycles) |
|---|---:|---:|---:|
| FD001 | 100 | 30.91 / 40.00 | 21.96 / 27.12 |
| FD002 | 259 | 33.56 / 41.51 | 31.44 / 37.83 |
| FD003 | 100 | 37.28 / 47.90 | 37.08 / 47.99 |
| FD004 | 248 | 50.68 / 61.62 | 42.01 / 51.91 |

These are single-protocol results on simulated aircraft engines. FD003 Ridge has slightly worse RMSE than the baseline. The report includes validation and true-RUL error slices; it does not estimate uncertainty or establish compressor performance. The dataset and generated report remain local.

To register and reproduce a local C-MAPSS run, use the version directory printed by the benchmark command. The registry stores metadata under ignored `data/processed/`; it does not copy the archive or report into Git. `register-experiment` also supports `anomaly-baseline` and `isolation-forest` with their verified feature, policy, report-window, and baseline paths; see `--help` for required options.

```bash
uv run --locked machineguard register-experiment cmapss-rul \
  data/processed/phase7_rul/nasa-cmapss-classic/VERSION_ID \
  --registry-root data/processed/experiment_registry \
  --archive data/external/CMAPSSData.zip \
  --policy configs/cmapss_rul_benchmark.json
uv run --locked machineguard list-experiments \
  --registry-root data/processed/experiment_registry
uv run --locked machineguard compare-experiments RUN_ID_1 RUN_ID_2 \
  --registry-root data/processed/experiment_registry
uv run --locked machineguard reproduce-experiment RUN_ID_1 \
  --registry-root data/processed/experiment_registry \
  --output-root data/processed/reproduced_experiments
```

Reproduction checks source/config hashes, the dependency lock, and builder identity before rebuilding. It reports missing bytes or incompatible code as `blocked`; existing Phase 4/5 artifacts require their original builder versions. Comparisons reject different source evidence, targets, feature versions, or evaluation protocols. Every record stays `research_only`; test-set results never trigger promotion.

To replay a verified MetroPT canonical version locally, provide its source-bound chronological split. An optional Phase 4 baseline artifact adds descriptive anomaly scores. `--speed 0` runs without deliberate delay; a positive speed factor divides event-time gaps by that factor. `--max-arrivals` produces a verified, resumable prefix with measured local throughput. Omit it to finish the full source. The output directory is ignored by Git.

```bash
uv run --locked machineguard replay-metropt CANONICAL_VERSION_DIR \
  --output-root data/processed/replay \
  --split configs/metropt_anomaly_split.json \
  --baseline-version BASELINE_VERSION_DIR \
  --baseline-policy configs/metropt_anomaly_baseline.json \
  --max-arrivals 200000
uv run --locked machineguard inspect-replay REPLAY_DIR \
  --feature-version FEATURE_VERSION_DIR
```

Use the exact canonical manifest from which the feature version was built; two local copies can share a version ID while differing in manifest bytes. The replay reports late events and sampling gaps, checkpoints state, and compares its decisions against verified batch features and scores. It does not offer real-time service guarantees or failure probabilities.

To inspect a **synthetic, assumption-only** maintenance scenario:

```bash
uv run --locked machineguard simulate-maintenance configs/maintenance_scenario.synthetic.json
```

The JSON report repeats every assumption, includes an input SHA-256, compares hypothetical defer and maintain costs, shows conservative bounds across low/high assumptions, and allocates at most ten assets into at most six crew slots. A scenario must use `scenario_kind: hypothetical_assumptions`; model-derived probabilities and anomaly scores are rejected. Amounts are decimal strings with up to six fractional digits. The result requires human review and is not a measured savings estimate or a work order. MetroPT has no qualified failure-probability input for model-driven economics.

For development verification:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

The automated tests use explicitly synthetic local rows and contract examples. A local full-source import was also verified; its results are not a predictive-model quality or industrial performance claim.

## Dataset references

- [MetroPT-3, UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/791/metropt%2B3%2B): verified industrial compressor telemetry source for local ingestion, feature engineering, anomaly research, and the failure-risk feasibility audit. Its page lists CC BY 4.0 and sparse fault-report intervals rather than row-level failure labels. The source archive is not redistributed here.
- [NASA Prognostics Center of Excellence repository](https://www.nasa.gov/intelligent-systems-division/discovery-and-systems-health/pcoe/pcoe-data-set-repository/): provides the classic C-MAPSS turbofan degradation benchmark used for separate cycle-based RUL research. The classic archive and embedded README were inspected locally; redistribution terms remain unresolved, so no archive is included.

No third-party dataset or model artifact is included in this repository.

## Known limitations

The MetroPT ingest pipeline handles the observed layout only; it does not infer a timezone or repair bad rows. Its plausibility bounds are heuristic warnings, not certified operating limits. Statistical and multivariate anomaly experiments exist, but no calibrated risk model, API, UI, or industrial deployment exists. The Phase 5 Isolation Forest reduced the off-report alert proxy to about 4.53 validation and 10.09 test episodes per observed day, but this remains too noisy for maintenance use; it coincided with only two of four reported intervals. The Phase 3 split contains only one reported fault interval in the held-out test period, so it cannot establish reliable failure-probability performance. MetroPT-3 has approximately 10-second observed intervals in its source CSV and sparse fault reports, which may prevent credible failure-probability estimation. The separate C-MAPSS benchmark is simulated aircraft-engine data and cannot establish compressor RUL performance; it does not report uncertainty intervals. Maintenance scenario cost figures are entirely assumed; slot timing does not modify risk, and the box bounds are not statistical confidence intervals.

## License

Original MachineGuard project content is licensed under the [MIT License](LICENSE). This license does **not** grant rights to third-party datasets, models, libraries, or other external material; those remain subject to their own terms and attribution requirements.
