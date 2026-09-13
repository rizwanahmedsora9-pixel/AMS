# AMS — Ahmed Management System

Flask + SQLAlchemy (SQLite) ERP for a cement / building-materials business:
direct sales and bookings, stock and GRN, dispatch, delivery persons and
rentals, clients / suppliers, chart of accounts, cash flow and reconciliation,
financial ledgers, reports and PDF exports, Excel import / export, plus admin,
permissions and data-maintenance tooling.

`wsgi.py` is the production entry point; `main.py` is the local development
server. A signed PythonAnywhere deployment webhook is available through
`wsgi.py` (see "Deployment" below).

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
| Hardcoded webhook token / wrong GitHub repo in `main.py` | Removed. The replacement uses a server-only secret and a signed WSGI webhook; `main.py` stays a local server |
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

The replacement is `deploy_hook.py`, mounted by `wsgi.py` at `/deploy`.
It uses only Python's standard library on Linux/PythonAnywhere. A signed GitHub
push to the configured branch runs `git pull --ff-only origin <branch>` and
touches the PythonAnywhere WSGI file to request a reload. No API token,
GitHub Actions workflow, scheduler, or Flask CSRF exemption is required.

### One-time PythonAnywhere setup

AMS site: https://rehmanahmed.pythonanywhere.com/

First merge this change into the branch you deploy (normally `main`), then
install or update **this** repository on PythonAnywhere. The server checkout's
`origin` must be `rizwanahmedsora9-pixel/AMS`, and its current branch must match
`AMS_DEPLOY_BRANCH`. Do not reuse the other application's checkout or secret.
Configure the Web tab virtualenv and install `requirements.txt` into it.

The production checkout lives directly in `/home/rehmanahmed`, with no `AMS`
subfolder. PythonAnywhere home directories already contain account files, so
`git clone ... .` usually cannot work. For this first installation only, run
in a **PythonAnywhere Bash console**, after the changes are merged:

```bash
cd /home/rehmanahmed
# Stop if this home directory already belongs to a Git checkout.
if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Existing Git checkout: stop and inspect it before continuing."
else
    git init &&
    git remote add origin https://github.com/rizwanahmedsora9-pixel/AMS.git &&
    git fetch origin main &&
    git checkout -b main --track origin/main &&
    printf '\n# Deployment-only home checkout: ignore unrelated untracked account files\n*\n' >> .git/info/exclude
fi
```

Do not force checkout or delete account files if Git reports a collision.
The local exclude rule keeps unrelated home files from blocking deployment;
tracked AMS files are still checked for edits. This is a deployment-only
checkout: new untracked files are ignored, so do development in a separate
checkout. Never run `git clean -fdx` here—it could erase account data.

Use `/home/rehmanahmed` for both Source code and Working directory in the
Web tab, `/home/rehmanahmed/.venv` for the virtualenv, and map `/static/` to
`/home/rehmanahmed/static`. Create the virtualenv with a Python version matching
the Web app, for example:

```bash
cd /home/rehmanahmed
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

1. In a PythonAnywhere Bash console, generate a **new**, server-only secret:

   ```bash
   cd /home/rehmanahmed
   mkdir -p instance
   (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > instance/deploy_secret.txt)
   ```

   The uploaded root-level `deploy_secret.txt` was tracked in Git. Removing it
   does not erase history: consider it exposed and replace it in any other
   webhook that shared it. Never commit the new value or paste it into chat.

2. In **Web → WSGI configuration file**, preserve any required host setup,
   then load AMS like this (assuming the checkout is `/home/rehmanahmed`;
   adjust the repository path if you cloned it elsewhere):

   ```python
   import os
   import sys

   sys.path.insert(0, "/home/rehmanahmed")
   os.environ["AMS_WSGI_FILE"] = "/var/www/rehmanahmed_pythonanywhere_com_wsgi.py"
   os.environ["AMS_DEPLOY_BRANCH"] = "main"
   os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")
   os.environ.setdefault("AMS_HTTPS", "1")
   from wsgi import application
   ```

   Use the exact WSGI filename shown in the Web tab (custom domains can differ).
   Do not point `AMS_WSGI_FILE` at the repository's `wsgi.py`. If your existing
   setup uses a custom `APP_DB_PATH`, preserve it and back up that database.
   The hook does not change, delete, or copy database files.

3. Click the Web tab's **Reload** button once. Open `/deploy/health`; it should
   say the hook is reachable. This is only a reachability check, not proof that
   Git, secrets, or reload permissions are configured correctly.

4. In **rizwanahmedsora9-pixel/AMS → Settings → Webhooks → Add webhook**:
   - Payload URL: `https://rehmanahmed.pythonanywhere.com/deploy`
   - Content type: `application/json`
   - Secret: copy the new server file's contents privately into GitHub.
   - Events: **Just the push event**; keep SSL verification enabled.

5. GitHub's signed ping should return `pong`. After a push to the deployed
   branch, inspect **Recent Deliveries** for `Deployed <commit>; WSGI reload
   requested`, then verify the app and PythonAnywhere logs. These changes are
   currently on the Arena working branch; pushes here do not deploy `main`
   until merged. No live deployment has been performed by adding this code.

### Behaviour and troubleshooting

- Optional `AMS_DEPLOY_SECRET_FILE` overrides `instance/deploy_secret.txt`.
  Set environment variables before importing `wsgi` and reload after changes.
- Wrong branch, deleted branch, and non-push events are ignored. Missing
  secrets return 503; bad signatures return 401; malformed JSON returns 400.
- Local changes, a wrong checkout branch, or a concurrent deploy return 409.
  Resolve the cause manually, then redeliver from GitHub. The hook never
  stashes, resets, or discards server edits. Diverged branches fail safely.
- Git commands have timeouts and cannot ask for interactive credentials.
  For private repos, configure read access on the server separately. GitHub
  may time out before a slow synchronous pull completes: check server logs
  before redelivering. There is no background queue or automatic retry.
- Deployment errors go to PythonAnywhere's error log, not public status
  pages. GET `/deploy`, `/deploy/health`, and `/deploy/status` reveal no paths,
  secrets, or Git logs. Reload failure returns an error even if code pulled.
- Dependencies are **not** installed by the hook. Install changed requirements
  manually and reload. AMS applies its existing migrations on startup; take
  a database snapshot before releases with schema changes. There is no
  automatic rollback or database backup in this hook.
- The webhook is production WSGI-only; `python main.py` does not mount it.

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

1. **Deployment activation** requires PythonAnywhere and GitHub setup and a
   real push/reload check (see "Deployment" above).
2. **`User.password_plain`** still exists for legacy rows. Successful login
   upgrades to `password_hash` and clears plaintext. Rotate any account that
   has never logged in since the hash-only era.
3. **`_WIPE_BACKUP_ENABLED` is `False`.** Granular wipe in Settings does not
   auto-file a DB copy (the deployment hook also makes no backups). Take a
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
