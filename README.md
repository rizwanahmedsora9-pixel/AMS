# AMS — Ahmed Management System

Flask + SQLAlchemy (SQLite) ERP for a cement / building-materials business:
direct sales and bookings, stock and GRN, dispatch, delivery persons and
rentals, clients / suppliers, chart of accounts, cash flow and reconciliation,
financial ledgers, reports and PDF exports, Excel import / export, plus admin,
permissions and data-maintenance tooling.

The app is built to run on a **PythonAnywhere** account. `wsgi.py` is the
production entry point; `main.py` is the local development server. Deployment
targets live in `config.py` (no secrets in Git).

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
main.py, wsgi.py        Dev server / PythonAnywhere WSGI
config.py               Deployment targets (never holds secrets)
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
deploy/                 Webhook deployer and health check (runs inside the live app)
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
| `AMS_WEBHOOK_TOKEN` | **required on the server** | Deploy webhook auth — no hardcoded fallback |
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
| Hardcoded webhook token / wrong GitHub repo in `main.py` | Removed. `main.py` is only the local server. Deploy lives in `config.py` + `deploy/` + `app/deploy_routes.py`. Token is env-only |
| CSRF | Session CSRF on **every** mutating endpoint (`app/hooks.py`); webhook is the only skip (HMAC / shared token) |
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

Everything about *where* code comes from and *where* it deploys is in
**`config.py`**. After one-time setup:

```text
edit → git add → git commit → git push
```

Flow: GitHub push → `POST /git-auto-pull` → `deploy/deployer.py` inside the
live app (protect `instance/` → git sync → restore `instance/` → pip if
needed → import-validate → reload WSGI → `/health`).

Secrets are environment-only: `AMS_WEBHOOK_TOKEN`, `PYTHONANYWHERE_API_TOKEN`.

### One-time server setup

1. GitHub → Settings → Secrets: `AMS_WEBHOOK_TOKEN` (long random string) and
   `PYTHONANYWHERE_API_TOKEN` (from the PA account API page).
2. Confirm `config.py` (or `AMS_*` env overrides) match the **actual** GitHub
   repo and PythonAnywhere user/domain. Committed defaults still name the
   older `rehmanahmedca-source/AMSCOPY9` checkout — override them for this
   fork (`rizwanahmedsora9-pixel/AMS`) rather than editing blindly.
3. On PythonAnywhere: clone, `mkvirtualenv --python=/usr/bin/python3.11 ams-venv`,
   `pip install -r requirements.txt`, create a Manual Python 3.11 web app.
4. WSGI file must set the token (a bash `export` does **not** reach the web app):

   ```python
   import os, sys
   path = "/home/<pa-user>/<project>"
   if path not in sys.path:
       sys.path.insert(0, path)
   os.environ["AMS_WEBHOOK_TOKEN"] = "PASTE_THE_SAME_LONG_RANDOM_TOKEN"
   os.environ["VIRTUAL_ENV"] = "/home/<pa-user>/.virtualenvs/ams-venv"
   from wsgi import app as application  # noqa
   ```

5. Reload. `https://<domain>/health` should return JSON with `"status": "healthy"`.
6. Optional GitHub webhook: payload URL `https://<domain>/git-auto-pull`,
   secret = the same `AMS_WEBHOOK_TOKEN`, push events only.

There is **no** `.github/workflows/deploy.yml` in this checkout yet. Until it
exists, trigger deploys with the GitHub webhook (or `POST /git-auto-pull` with
the token). If the running code is older than the repo, 400/403 is normal —
sync once by hand (`git fetch && git reset --hard origin/<branch>`), reload,
then redeliver. `instance/*.db` is git-ignored.

### Safety

- Live data is snapshotted out of `instance/` before `git reset` and copied
  back. A pre-deploy DB copy goes to `instance/backups/`.
- Failed fetch / requirements / import → no reload; the working app stays up.
- Code rollback: `python deploy/deploy.py --rollback` (or `--to-commit <sha>`).
- Database rollback is manual: restore a file from `instance/backups/`.

Local checks:

```bash
python config.py                 # control panel + validity
python deploy/deploy.py --show
python deploy/deploy.py --check  # also requires the webhook secret
python deploy/deploy.py --health
```

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

1. **`config.py` defaults** still point at `AMSCOPY9` /
   `rehmanahmedca-source`. This clone is `rizwanahmedsora9-pixel/AMS`. Set
   `AMS_GITHUB_*` / `AMS_PA_*` on the server, or update the defaults when this
   repo is the real deploy source.
2. **GitHub Actions deploy workflow** is documented historically but not in
   the tree. Add `.github/workflows/deploy.yml` if push-to-deploy should run
   from Actions instead of only the webhook.
3. **`User.password_plain`** still exists for legacy rows. Successful login
   upgrades to `password_hash` and clears plaintext. Rotate any account that
   has never logged in since the hash-only era.
4. **`_WIPE_BACKUP_ENABLED` is `False`.** Granular wipe in Settings does not
   auto-file a DB copy (the deploy path still does). Take a snapshot before
   a wipe.
5. **Sale POST idempotency** is tested, but unkeyed double-submit uniqueness
   is still a product choice (payload hash + key). Do not rely on the browser
   alone.
6. **Scale:** list pages that embed every client in the combobox grow with
   client count; `/api/clients/search` exists for a lazy picker if payloads
   get large.

Do not restore the deleted audit markdown. If a new defect is found, file it
against **this** code (repro + pytest), not against August 2026 notes.

---

## License

No license has been added to this repository yet.
