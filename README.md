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
| CSV/Parquet ingestion and data quality | **Planned** | Canonical telemetry, quality reports, and versioned storage. |
| Causal features and anomaly detection | **Planned** | Statistical baselines before multivariate models. |
| Failure-risk modeling | **Planned, conditional** | Only if event labels support a defined horizon and held-out calibration. |
| C-MAPSS RUL benchmark | **Planned** | Separate study of simulated aircraft-engine trajectories. |
| Replay, decision scenarios, API, and dashboard | **Planned** | Added after the underlying contracts and evidence are established. |
| Experimental models or analytics | **None** | No training or inference code exists yet. |

Phase 1, the source-contract and executable-foundation phase, is complete. The CLI does not ingest, transform, or score datasets. No dataset bytes or trained models are included.

## Architecture overview

The target design is a modular Python application. Immutable source files and fingerprints feed validated, versioned telemetry. Feature calculations use only observations available at their prediction time. Anomaly detection, failure risk, and RUL remain separate tasks with separate evaluation. A deterministic local replay engine will precede distributed streaming. A future API and dashboard will expose evidence, provenance, and uncertainty. Maintenance optimization will require qualified probability inputs for expected-cost calculations.

## Technology overview

The implemented foundation uses Python 3.12+, uv for locking, Hatchling for builds, pytest, Ruff, and mypy. Runtime code uses only the Python standard library. Polars, PyArrow, Parquet, scikit-learn, FastAPI, React, TypeScript, Plotly, PostgreSQL, Docker, and other tools are candidates for later phases when their requirements are established. Those later-phase candidates are not runtime dependencies of the current package.

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

`src/machineguard/` will contain domain and application modules; `tests/` will hold automated verification; `data/fixtures/` may contain small, clearly labeled synthetic test data; `configs/` and `scripts/` will support reproducible runs; `frontend/` is a future interface; `.github/workflows/` now contains CI. Raw datasets, generated models, and internal experiment records will not be committed by default.

The **currently published implementation** contains `README.md`, `LICENSE`, `.gitignore`, `pyproject.toml`, `uv.lock`, `configs/split_policy.json`, `.github/workflows/ci.yml`, `src/machineguard/` (CLI and core contracts), and `tests/unit/`. Private planning files remain available in the local workspace only. The target tree above includes future modules that do not yet exist.

## Installation, development, and testing

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or 3.13, then run:

```bash
uv sync --locked --dev
uv run --locked machineguard --help
uv run --locked machineguard contracts metropt-3-uci
uv run --locked machineguard contracts nasa-cmapss-classic
uv run --locked machineguard sha256 /path/to/local/file
```

`contracts` prints the observed source layout and unresolved timezone/label policy. `sha256` reads a local file and prints its fingerprint; it does not upload data. For development verification:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build
```

The tests use explicitly synthetic local bytes and contract examples. They do not assert any predictive-model quality or industrial performance.

## Dataset references

- [MetroPT-3, UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/791/metropt%2B3%2B): planned industrial compressor telemetry source. Its page lists CC BY 4.0 and sparse fault-report intervals rather than row-level failure labels. The Phase 1 source archive was inspected locally but is not redistributed here.
- [NASA Prognostics Center of Excellence repository](https://www.nasa.gov/intelligent-systems-division/discovery-and-systems-health/pcoe/pcoe-data-set-repository/): lists the classic C-MAPSS turbofan degradation benchmark planned for separate RUL research. The classic archive and embedded README were inspected locally; redistribution terms remain unresolved, so no archive is included.

No third-party dataset or model artifact is included in this repository.

## Known limitations

No data pipeline, model, API, UI, or industrial deployment exists yet. MetroPT-3 has approximately 10-second observed intervals in its source CSV and sparse fault reports, which may prevent credible failure-probability estimation. C-MAPSS is simulated aircraft-engine data and cannot establish compressor RUL performance. Future cost figures will be identified as assumptions unless supported by observed data.

## License

Original MachineGuard project content is licensed under the [MIT License](LICENSE). This license does **not** grant rights to third-party datasets, models, libraries, or other external material; those remain subject to their own terms and attribution requirements.
