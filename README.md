# MachineGuard

**Industrial Predictive Maintenance & Decision Intelligence Platform**

MachineGuard is a planned, Python-centered engineering platform for analyzing industrial telemetry and supporting maintenance decisions. The project aims to combine sensor data quality checks, anomaly detection, carefully evaluated failure-risk modeling, remaining useful life (RUL) research, historical replay, and maintenance scenario analysis. It is an engineering and data science project, not a validated industrial safety system.

## Problem and objectives

Industrial telemetry can contain gaps, duplicate measurements, changing operating states, and uncertain fault records. A useful maintenance tool must preserve data provenance, explain why an alert appeared, and distinguish a deviation score from a calibrated failure probability.

MachineGuard's objectives are to create reproducible data and model workflows, evaluate alerts against available event evidence, quantify uncertainty where the data supports it, and present transparent maintenance scenarios for human review. The platform is designed to run locally without paid cloud services.

## Capabilities and status

| Capability | Status | Scope |
|---|---|---|
| Project architecture and scientific constraints | **Implemented** | Planning and source-level dataset review are complete locally. |
| Executable Python package, ingestion, and data quality | **Planned** | CSV/Parquet contracts, provenance, and quality reports. |
| Causal features and anomaly detection | **Planned** | Statistical baselines before multivariate models. |
| Failure-risk modeling | **Planned, conditional** | Only if event labels support a defined horizon and held-out calibration. |
| C-MAPSS RUL benchmark | **Planned** | Separate study of simulated aircraft-engine trajectories. |
| Replay, decision scenarios, API, and dashboard | **Planned** | Added after the underlying contracts and evidence are established. |
| Experimental runtime features | **None** | No application code has been published yet. |

The public repository currently contains documentation and repository setup only. Phase 1 application development has **not** started.

## Architecture overview

The target design is a modular Python application. Immutable source files and fingerprints feed validated, versioned telemetry. Feature calculations use only observations available at their prediction time. Anomaly detection, failure risk, and RUL remain separate tasks with separate evaluation. A deterministic local replay engine will precede distributed streaming. A future API and dashboard will expose evidence, provenance, and uncertainty. Maintenance optimization will require qualified probability inputs for expected-cost calculations.

## Technology overview

The initial development toolchain is planned to use Python 3.12+, uv, pytest, Ruff, and mypy. Polars, PyArrow, Parquet, scikit-learn, FastAPI, React, TypeScript, Plotly, PostgreSQL, Docker, and other tools are candidates for later phases when their requirements are established. None is installed or required by this repository yet.

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

`src/machineguard/` will contain domain and application modules; `tests/` will hold automated verification; `data/fixtures/` may contain small, clearly labeled synthetic test data; `configs/` and `scripts/` will support reproducible runs; `frontend/` and `.github/workflows/` are future interface and CI components. Raw datasets, generated models, and internal experiment records will not be committed by default.

The **actual initial public repository** contains only:

```text
machineguard/
├── README.md
├── LICENSE
└── .gitignore
```

## Installation, development, and testing

There is no installable package or executable application at this stage. No dependency installation is needed to read this repository. Installation commands, development commands, and automated test instructions will be added when the Python package exists. No test or performance result is claimed here.

## Dataset references

- [MetroPT-3, UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/791/metropt%2B3%2B): planned industrial compressor telemetry source. Its page lists CC BY 4.0 and sparse fault-report intervals rather than row-level failure labels. The archive has not been inspected locally.
- [NASA Prognostics Center of Excellence repository](https://www.nasa.gov/intelligent-systems-division/discovery-and-systems-health/pcoe/pcoe-data-set-repository/): lists the classic C-MAPSS turbofan degradation benchmark planned for separate RUL research. Archive availability, embedded documentation, and usage terms require verification before implementation or redistribution.

No third-party dataset or model artifact is included in this repository.

## Known limitations

No data pipeline, model, API, UI, or industrial deployment exists yet. MetroPT-3's publisher metadata contains conflicting sampling-rate statements and sparse fault reports, which may prevent credible failure-probability estimation. C-MAPSS is simulated aircraft-engine data and cannot establish compressor RUL performance. Future cost figures will be identified as assumptions unless supported by observed data.

## License

Original MachineGuard project content is licensed under the [MIT License](LICENSE). This license does **not** grant rights to third-party datasets, models, libraries, or other external material; those remain subject to their own terms and attribution requirements.
