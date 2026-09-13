# AMS — Ahmed Management System

A Flask + SQLAlchemy (SQLite) ERP for a cement / building-materials business: direct sales and
bookings, stock and GRN, dispatch, delivery persons and rentals, clients / suppliers, chart of
accounts, cash flow and reconciliation, financial ledgers, reports and PDF exports, Excel
import / export, plus admin, permissions and data-maintenance tooling.

The app currently runs on a **PythonAnywhere free account** — `wsgi.py` is the entry point there,
`main.py` is the local development server. Deployment targets are centralised in `config.py`
(see [`DEPLOYMENT.md`](DEPLOYMENT.md)).

## Stack

| Area | Technology |
|---|---|
| Web framework | Flask 3, Flask-Login, Flask-SQLAlchemy |
| ORM / database | SQLAlchemy 2, SQLite (`instance/ahmed_cement_v44_fresh.db`, schema **v4.4**) |
| Data processing | pandas, numpy (<2), openpyxl |
| PDF | WeasyPrint (primary) with a pure-Python ReportLab fallback (`app/services/pdf_fallback.py`) |
| Front end | Jinja2 templates, Bootstrap 5.3, Bootstrap Icons, Flatpickr (vendored in `static/vendor`) |
| Tests | pytest (`pytest.ini`) |

## Repository layout

```text
main.py, wsgi.py        Entrypoints — dev server / PythonAnywhere WSGI
config.py               Single source of truth for deployment targets (never holds secrets)
app/                    Application factory (__init__.py), services, HTTP blueprints, migrations
  app/services/         Business logic: accounting, billing, cash flow, ledgers, imports, PDFs…
  app/blueprints/       ledgers, masters, misc, ops, reports, sales, system
blueprints/             Auto-registered blueprint packages (accounts, inventory, import_export…)
models/                 SQLAlchemy models split by domain (sales, cash, stock, parties, rentals…)
templates/              81 Jinja templates (layout.html + per-module pages)
static/                 JS/CSS and vendored front-end libraries
utils/                  Module loader and shared helpers
tools/                  Maintenance, audit, migration and health scripts
tests/                  pytest suite (backend + frontend tests)
deploy/                 Webhook deployer and health check that run inside the live app
full_db_sync/           Full-database SQLite sync/export utilities
docs/                   Migration, data-load and sync documentation
instance/               Runtime data: SQLite DB, secret_key, logs, import reports, backups
dummy_data/             Sample dataset generator and verification scripts
references-images/      UI reference screenshots
```

`utils/module_loader.py` discovers blueprints at startup, so new modules placed in the blueprint
directories are registered automatically.

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py            # serves on 0.0.0.0:5000
```

WeasyPrint needs the system libraries `pango`, `cairo` and `gdk-pixbuf`. When they are missing
(common on shared hosts) PDF downloads automatically fall back to ReportLab.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Dev server port |
| `AMS_DEBUG` | unset | Set to `1` to enable the Werkzeug debugger in `main.py` |
| `APP_DB_PATH` | `instance/ahmed_cement_v44_fresh.db` | SQLite database location |
| `AMS_SCHEMA_VERSION` | `v44` | Runtime schema (`v44` is the only supported version) |
| `SECRET_KEY` | generated into `instance/secret_key` | Flask session signing key |
| `AMS_HTTPS` | unset | Set to `1` for secure/`SameSite=None` session cookies |
| `MAX_UPLOAD_MB` | `256` | Upload size limit |
| `SQLITE_JOURNAL_MODE` | auto | `DELETE` is forced on PythonAnywhere (no POSIX shm for WAL) |

## Tests

```bash
pytest
```

## Deployment

`config.py` declares the GitHub source and the PythonAnywhere target; the deployer in
`deploy/deployer.py` runs inside the live app (protect `instance/` → sync code → restore
`instance/` → install requirements if changed → import-validate → reload WSGI → health check).

Secrets are read from the environment only — `AMS_WEBHOOK_TOKEN` and `PYTHONANYWHERE_API_TOKEN`.
Full instructions: [`DEPLOYMENT.md`](DEPLOYMENT.md).

> **Note:** `DEPLOYMENT.md` describes a GitHub Actions workflow at `.github/workflows/deploy.yml`.
> That workflow file is not present in this repository yet.

## Documentation

- [`DEPLOYMENT.md`](DEPLOYMENT.md) — deployment pipeline and one-time setup
- [`docs/`](docs) — auto migrations, data-load verification, full-DB SQLite sync, legacy mapping
- [`AUDIT_REPORT.md`](AUDIT_REPORT.md), [`QA_FULL_AUDIT.md`](QA_FULL_AUDIT.md),
  [`ORPHAN_SCENARIO_AUDIT.md`](ORPHAN_SCENARIO_AUDIT.md) — audit findings and QA runs
- [`CONTINUATION_SUMMARY.md`](CONTINUATION_SUMMARY.md) — latest fix-session notes

## License

No license has been added to this repository yet.
