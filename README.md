# AMS — Ahmed Management System

Flask + SQLAlchemy (SQLite) ERP for a cement / building-materials business:
direct sales and bookings, stock and GRN, dispatch, delivery persons and
rentals, clients / suppliers, chart of accounts, cash flow and reconciliation,
financial ledgers, reports and PDF exports, Excel import / export, plus admin,
permissions and data-maintenance tooling.

`wsgi.py` is the production entry point; `main.py` is the local development
server. Deployment automation was removed and is being rebuilt from scratch
(see "Deployment" below).

> **About the old audit files.** This repo used to ship a pile of session
> reports (`AUDIT_REPORT.md`, `QA_FULL_AUDIT.md`, orphan plans, schema-failure
> notes, continuation summaries). Those described **August 2026** — empty DB,
> hardcoded webhook token, CSRF only on accounts, duplicate auto bill numbers,
> Open-Khata receivables that could not be settled, wipe FK crashes. **The
> current app is well ahead of those write-ups.** The findings were fixed in
> code and locked with `tests/test_predator_regressions.py`. The historical
> markdown was deleted so nobody treats a stale P0 list as the live state.

---

## Stack

| Area | Technology |
|---|---|
| Web | Flask 3, Flask-Login, Flask-SQLAlchemy |
| ORM / DB | SQLAlchemy 2, SQLite (`instance/ahmed_cement_v44_fresh.db`, schema **v4.4**) |
| Data | pandas, numpy (&lt;2), openpyxl |
| PDF | WeasyPrint (primary) with ReportLab fallback (`app/services/pdf_fallback.py`) |
| Front end | Jinja2, Bootstrap 5.3, Bootstrap Icons, Flatpickr (vendored in `static/vendor`) |
| Tests | pytest (`pytest.ini`) |

Schema source of truth is the **ORM** (`db.create_all()` + column/index helpers
in `app/services/schema.py`). There is no `v44/SCHEMA_v4_4.sql` file; boot logs
a warning and continues. Versioned SQL in `app/migrations/` (`NNNN_*.sql`) runs
automatically on every start via `app/services/auto_migrate.py`.

---

## Layout

```text
main.py, wsgi.py        Dev server / production WSGI entry point
app/                    Factory, services, HTTP blueprints, SQL migrations
  app/services/         Accounting, billing, cash flow, ledgers, imports, PDFs
  app/blueprints/       ledgers, masters, misc, ops, reports, sales, system
  app/migrations/       Auto-applied NNNN_*.sql files (see that folder's README)
blueprints/             Auto-registered packages (accounts, inventory, import_export…)
models/                 SQLAlchemy models by domain
templates/              Jinja (layout.html + per-module pages)
static/                 JS/CSS and vendored libraries
utils/                  Module loader and shared helpers
tools/                  Maintenance, audit, migration and health scripts
tests/                  pytest (backend + frontend)
full_db_sync/           SQLite .amsdb snapshot export/import (stdlib only)
dummy_data/             Sample workbook generator / verifier
instance/               Runtime: SQLite DB, secret_key, logs, backups (git-ignored)
```

`utils/module_loader.py` discovers blueprints at startup.

---

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py            # 0.0.0.0:5000
```

WeasyPrint needs `pango`, `cairo` and `gdk-pixbuf`. When they are missing
(common on shared hosts) PDF downloads fall back to ReportLab.

Default first-run admin (empty DB): `Admin` / `Admin@fbm12345`. Change it.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Dev server port |
| `AMS_DEBUG` | unset | `1` enables the Werkzeug debugger in `main.py` |
| `APP_DB_PATH` | `instance/ahmed_cement_v44_fresh.db` | SQLite path |
| `AMS_SCHEMA_VERSION` | `v44` | Runtime schema (`v44` only) |
| `SECRET_KEY` | generated into `instance/secret_key` | Session signing |
| `AMS_HTTPS` | unset | `1` for secure / `SameSite=None` cookies |
| `MAX_UPLOAD_MB` | `256` | Upload size limit |
| `SQLITE_JOURNAL_MODE` | auto | `DELETE` is forced on PythonAnywhere (no POSIX shm for WAL) |
| `ALLOW_EMPTY_DB` | `1` | Missing DB is a valid first run |
| `MIGRATIONS_ALLOW_DESTRUCTIVE` | `0` | Permit `DROP`/`DELETE` in SQL migration files |

---

## Tests

```bash
pytest
```

Regression coverage for the old PRED defects (concurrent bill numbers,
future-dated payments, Open-Khata visibility/settlement, CSRF on all mutating
routes, sale idempotency, reconciled-period guard, wipe FK order, SQL-leak
flashes, `check_bill` auto bills, stock/bill/receivable blind spots) lives in
`tests/test_predator_regressions.py`. Schema/import, full-DB snapshots,
auto-migrations, wipe, ledgers/PDF and login have their own modules.

---

## What is already solid (vs the old audits)

Do **not** re-open these as if they were still broken:

| Area | Current behaviour |
|---|---|
| Empty production DB | First-run bootstrap is supported; full data moves via SQLite snapshots |
| Hardcoded webhook token / wrong GitHub repo in `main.py` | Removed. `main.py` is only the local server. All old deploy wiring (`config.py`, `deploy/`, webhook routes) has been deleted and will be rebuilt from scratch |
| CSRF | Session CSRF on **every** mutating endpoint (`app/hooks.py`); no webhook skip remains |
| Duplicate auto bill numbers | `get_next_bill_no` takes the SQLite write lock first |
| Future-dated money | Payments and reconciliation dates in the future are rejected |
| Open Khata | `ensure_open_khata_client()` seeds a real master; receivables show and settle |
| Domain wipe FK order | Sale lines unlink `grn_item_id` before GRN lots are deleted |
| Sale forms | Add/edit fields match the backend; missing-field scare was not confirmed |
| Import FK / blank bill uniqueness | Parent-first insert; unique auto-bill indexes ignore blanks |
| Data transport | Full moves use `.amsdb` SQLite snapshots (`full_db_sync/`), not Excel |
| Schema going forward | Numbered SQL migrations apply on every boot |

Sale, booking, payment, GRN, stock, ledger and wipe paths are exercised by
pytest. The predator truth engine (`tools/predator_truth_engine.py`) can still
be run against a copy of a live DB for an independent raw-SQL check.

---

## Data: load, snapshot, migrate

**Day-to-day full copy** (recommended): Import / Export Center →
**Full Database Snapshot (.db)**. Export produces `AMS_FULL_<ts>.amsdb`.
Import backs up the live file, wipes rows (schema stays), copies source rows
with original IDs, then runs `integrity_check` + `foreign_key_check` +
row-count parity.

CLI (same engine, no Flask required):

```bash
python -m full_db_sync export --db instance/ahmed_cement_v44_fresh.db
python -m full_db_sync verify --db AMS_FULL_xxx.amsdb
python -m full_db_sync import --source AMS_FULL_xxx.amsdb \
    --db instance/ahmed_cement_v44_fresh.db --confirm
```

Details: `full_db_sync/README.md`.

**Dummy dataset:** `dummy_data/README.md` (full XLSX for the legacy full-raw
importer). Prefer `.amsdb` for real full-data moves.

**Old-format `ahmed_cement.db`:** the snapshot importer **refuses** it (missing
v4.4 columns) before touching the target. Convert with the external migrate
tool, then import the resulting v4.4 file. The XLSX pipeline under
`tools/migrate/` is **superseded** (kept for old `ALLEXPORT` workbooks). An
“opening balances only” migration was proposed and **never built** — live
path is full-history SQLite.

**Adding schema later:** drop `app/migrations/0002_short_name.sql` (never edit
an already-applied file). See `app/migrations/README.md`.

---

## Deployment

> **Being rebuilt from scratch.** All previous deployment wiring (the old
> `config.py` control center, the `deploy/` package, the `/git-auto-pull`
> webhook and `/health` probe, the `wrangler.toml`) pointed at a different
> GitHub repo (`rehmanahmedca-source/AMSCOPY9`) and a different
> PythonAnywhere server, with unstable secrets — so it was deleted outright.
> Nothing in this checkout deploys anywhere right now.
>
> The app itself is untouched: `wsgi.py` exposes `application` for any WSGI
> host, and `instance/*.db` stays git-ignored. New deploy docs will land here
> as the replacement pipeline is written.

---

## Tools

Nothing under `tools/` is imported at runtime. Start with:

```bash
python tools/consistency_report.py          # read-only integrity
python tools/health/preflight_check.py      # blockers that stop new sales
python tools/predator_truth_engine.py --db instance/ahmed_cement_v44_fresh.db --check
```

Repair scripts in `tools/repair_controlled/` require `--confirm` and take a
backup first. See `tools/README.md`.

---

## Remaining work (real, not the old P0 list)

These are the gaps that still match the **current** tree:

1. **Deployment pipeline** is being rebuilt from scratch (old wiring
   deleted; see "Deployment" above).
2. **`User.password_plain`** still exists for legacy rows. Successful login
   upgrades to `password_hash` and clears plaintext. Rotate any account that
   has never logged in since the hash-only era.
3. **`_WIPE_BACKUP_ENABLED` is `False`.** Granular wipe in Settings does not
   auto-file a DB copy (no auto-deploy path exists right now). Take a
   snapshot before a wipe.
4. **Sale POST idempotency** is tested, but unkeyed double-submit uniqueness
   is still a product choice (payload hash + key). Do not rely on the browser
   alone.
5. **Scale:** list pages that embed every client in the combobox grow with
   client count; `/api/clients/search` exists for a lazy picker if payloads
   get large.

Do not restore the deleted audit markdown. If a new defect is found, file it
against **this** code (repro + pytest), not against August 2026 notes.

---

## License

No license has been added to this repository yet.
