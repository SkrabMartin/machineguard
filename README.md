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
| Anomaly detection | **Planned** | Statistical baselines before multivariate models. |
| Failure-risk modeling | **Planned, conditional** | Only if event labels support a defined horizon and held-out calibration. |
| C-MAPSS RUL benchmark | **Planned** | Separate study of simulated aircraft-engine trajectories. |
| Replay, decision scenarios, API, and dashboard | **Planned** | Added after the underlying contracts and evidence are established. |
| Experimental models or analytics | **None** | No training or inference code exists yet. |

Phases 1–3 are complete. The CLI can ingest MetroPT-shaped CSV or Parquet data, verify derived versions, and build trailing features with a descriptive audit. It does not train or score models. No dataset bytes or trained models are included.

## Architecture overview

The implemented ingest keeps raw sources outside the repository, validates them against the MetroPT contract, records quality findings, and writes fingerprinted monthly Parquet partitions through a staging directory. A completed version is published with a manifest and output hashes. Original timestamps are preserved as timezone-unknown source time. The feature builder uses current and past observations only, records lookback and minimum sample rules, and materializes a source-bound chronological train/validation/test split with one-hour purge gaps. Anomaly detection, failure risk, and RUL remain separate tasks with separate evaluation. A deterministic local replay engine will precede distributed streaming. A future API and dashboard will expose evidence, provenance, and uncertainty. Maintenance optimization will require qualified probability inputs for expected-cost calculations.

## Technology overview

The implemented stack uses Python 3.12+, uv for locking, Hatchling for builds, PyArrow 26 for bounded Parquet I/O, standard-library streaming statistics and rolling state, pytest, Ruff, and mypy. Python's standard CSV reader performs strict input parsing. Polars, scikit-learn, FastAPI, React, TypeScript, Plotly, PostgreSQL, Docker, and other tools remain candidates for later phases when their requirements are established.

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

`src/machineguard/` now contains the core contracts plus MetroPT ingestion, quality, storage, and feature modules; `tests/` contains unit and integration verification; `data/fixtures/` may contain small, clearly labeled synthetic test data; `configs/` contains a provisional split policy, a source-bound anomaly split, a feature specification, and reported fault intervals; `scripts/` is planned; `frontend/` is a future interface; `.github/workflows/` now contains CI. Raw datasets, generated models, and internal experiment records will not be committed by default.

The **currently published implementation** contains `README.md`, `LICENSE`, `.gitignore`, `pyproject.toml`, `uv.lock`, `configs/split_policy.json` and three Phase 3 configs, `.github/workflows/ci.yml`, `src/machineguard/` (CLI, core, ingestion, quality, storage, and features), and `tests/unit/` plus `tests/integration/`. Private planning files remain available in the local workspace only. The target tree above includes future modules that do not yet exist.

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

- [MetroPT-3, UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/791/metropt%2B3%2B): planned industrial compressor telemetry source. Its page lists CC BY 4.0 and sparse fault-report intervals rather than row-level failure labels. The Phase 1 source archive was inspected locally but is not redistributed here.
- [NASA Prognostics Center of Excellence repository](https://www.nasa.gov/intelligent-systems-division/discovery-and-systems-health/pcoe/pcoe-data-set-repository/): lists the classic C-MAPSS turbofan degradation benchmark planned for separate RUL research. The classic archive and embedded README were inspected locally; redistribution terms remain unresolved, so no archive is included.

No third-party dataset or model artifact is included in this repository.

## Known limitations

The ingest pipeline handles the observed MetroPT layout only; it does not infer a timezone, repair bad rows, or ingest C-MAPSS trajectories. Its plausibility bounds are heuristic warnings, not certified operating limits. No anomaly or risk model, API, UI, or industrial deployment exists yet. The Phase 3 split contains only one reported fault interval in the held-out test period, so it cannot establish reliable failure-probability performance. MetroPT-3 has approximately 10-second observed intervals in its source CSV and sparse fault reports, which may prevent credible failure-probability estimation. C-MAPSS is simulated aircraft-engine data and cannot establish compressor RUL performance. Future cost figures will be identified as assumptions unless supported by observed data.

## License

Original MachineGuard project content is licensed under the [MIT License](LICENSE). This license does **not** grant rights to third-party datasets, models, libraries, or other external material; those remain subject to their own terms and attribution requirements.
