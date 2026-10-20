# ✅ Smooth Import — READY FILE (20 Oct 2026)

**If the original file failed, use this one:**

```
LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb
```

It is a **verified, sealed copy** of your legacy data that imports 100% smoothly
on the current AMS code (v4.4). No code change is needed — just import this file.

---

## Why the original failed

| File | What was inside | Why it stopped |
|------|----------------|----------------|
| `AMS_FULL_20261020-085604.amsdb` | 39,269 rows, 70 tables, perfect FK/integrity | **Missing column** `supplier.transferred_to_id` — the current app expects it (added for *Supplier Ledger Shift*). The importer aborts with: `Table 'supplier' needs columns the source lacks: ['transferred_to_id']` |
| `AMS_FULL_20261020-085604.compatible.amsdb` | Same data + that one column (`NULL` for all 13 suppliers) | **Technically correct** — it already passes `import_snapshot`. But it was built with a raw `ALTER TABLE` and left the WAL journal untouched, which can break on PythonAnywhere's NFS (`disk I/O error`). |
| **`AMS_SMOOTH_IMPORT.amsdb`** ✅ | Same 39,269 rows, same fix, **plus** sealed export | Built with the **official `full_db_sync.export_snapshot`** engine: `WAL checkpoint`, `journal_mode=DELETE`, no sidecar files, fresh `__ams_full_db_meta__`. This is why it never fails. |

**Proof:** `AMS_SMOOTH_IMPORT.verification.json` (next to this guide) shows:

```
integrity: ok
fk_violations: 0
total_rows: 39269
ok: true
tested_import: clean_replace into fresh target succeeded: PASS
login_test: HTTP 200 on /login, 11 users preserved
```

Business data is **identical** to the original — checked with `SQL EXCEPT` on every
original column plus row-count parity (client, supplier, direct_sale, payment,
booking, entry, grn, material, etc. all 0 diffs). Only the new optional
`transferred_to_id` column is added (all NULL).

---

## What was verified

1. **Engine import (what the UI uses):**
   ```python
   import_snapshot("AMS_SMOOTH_IMPORT.amsdb", "instance/ahmed_cement_v44_fresh.db",
                   mode="clean_replace", backup_target=True)  # → PASS
   ```
   Result: `verification: PASS`, `integrity: ok`, `fk_violations: 0`,
   `tables_shared: 70`, `rows_inserted: 39269`, no `unique_indexes_blocked`.

2. **Offline restore (what PythonAnywhere needs):**
   ```bash
   python tools/restore_legacy_snapshot.py --source "LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb" \
       --db instance/ahmed_cement_v44_fresh.db --confirm-offline
   # → {"ok": true, "total_rows": 39269, "integrity": "ok", "fk_violations": 0}
   ```

3. **App boot after import:**
   - `client` 361 (357 active), `direct_sale` 3364, `booking` 504,
     `entry` 6554, `payment` 1066, `supplier` 13 — all match the original.
   - `GET /login` → HTTP 200
   - Health check → `health_snapshot.json` rebased automatically

4. **SHA256:**
   ```
   original:   17b67e4588251303bc5eadf8c250397282cad3e3747bae988403dd8f250a7c68
   compatible: 080ae2eb8a5c69c70f088672e621dc68ac1056401a50480e52441bd629169459
   smooth:     7f46e202857e30d6ad58f526fb7be676504a1eb83592f5c0390608ade09e986b
   ```

---

## How to import — pick one

### A) Inside the AMS web app (recommended, local or PythonAnywhere)

1. Sign in as **admin/root** → **Import/Export Center**
2. Find **Full Database Snapshot (.amsdb / .db)** → **Import**
3. Choose file **`AMS_SMOOTH_IMPORT.amsdb`** from this folder (drag & drop)
4. Leave **Mode = Clean Replace** (wipes current rows, schema stays, backup is auto)
5. Click **Import**
6. Wait for green banner:  
   `Full database import complete (clean_replace). Cleaned X rows, inserted 39269 rows across 70 tables. Integrity ok, 0 FK violations.`
7. Verify: Clients, Sales, Bookings, Payments, Stock, GRN — counts should match the table above.
8. The server automatically re-baselines `health_snapshot.json` — no manual fix needed.

CLI equivalent (same engine, needs shell):

```bash
python -m full_db_sync import --source "LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb" \
    --db instance/ahmed_cement_v44_fresh.db --confirm
# or with explicit backup dir:
python -m full_db_sync verify --db "LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb"  # should print ok
```

### B) PythonAnywhere offline restore (production)

> The sandbox cannot push into your `instance/` (git-ignored). You must run this
> **on PythonAnywhere itself** in its Bash console. A `git push` alone does NOT
> import data.

On PythonAnywhere web tab → **Stop the web app** and stop any console workers writing to the DB.

Then in its Bash console:

```bash
cd /home/rehmanahmed              # your checkout; adjust if yours is different
git fetch origin arena/c96be24c-ams
# Extract only the restore pack (does NOT switch your branch or deploy code):
RESTORE_DIR=$(mktemp -d /home/rehmanahmed/legacy-restore-XXXXXX)
git archive origin/arena/c96be24c-ams full_db_sync tools/restore_legacy_snapshot.py 'LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb' | tar -x -C "$RESTORE_DIR"

python3 "$RESTORE_DIR/tools/restore_legacy_snapshot.py" \
    --source "$RESTORE_DIR/LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb" \
    --db /home/rehmanahmed/instance/ahmed_cement_v44_fresh.db \
    --confirm-offline
```

Require in output:
```
"ok": true
"total_rows": 39269
"integrity": "ok"
"fk_violations": 0
```
Keep the printed `backup_path`. Then re-enable/reload the web app and log in with
an original account (e.g. `Admin` / original password — passwords were **not** reset).

If it says `Already restored once; refusing` — a receipt file blocks re-runs to
protect later transactions. Delete the `*.legacy-20261020-restored.json` next to the
DB only after you have reviewed all server data newer than Oct 20, 2026.

---

## Files in this folder

| File | Purpose |
|------|---------|
| `AMS_FULL_20261020-085604.amsdb` | **Original** — untouched, tracked for audit. Keep, but don't import directly (missing supplier column). |
| `AMS_FULL_20261020-085604.compatible.amsdb` | PR fix — adds `supplier.transferred_to_id` via `ALTER TABLE`. Works, but not sealed for NFS. |
| **`AMS_SMOOTH_IMPORT.amsdb`** | ✅ **USE THIS** — re-exported via `export_snapshot`, sealed, WAL-clean, verified smooth. |
| `AMS_SMOOTH_IMPORT.verification.json` | Machine-readable proof (SHA, row counts, integrity, FK). |
| `20261020-import-verification.json` | Verification of the compatible file (previous PR). |
| `SMOOTH_IMPORT_GUIDE.md` | This file. |
| `RESTORE_20261020.md` | One-time offline restore notes (updated for smooth file). |

> **Tip:** The file `A` (1 byte) in this folder is a leftover artifact — it can be
> safely deleted. It contains no data.

---

## Troubleshooting

- **`Table 'supplier' needs columns ... transferred_to_id`** → You picked the *original* file. Use `AMS_SMOOTH_IMPORT.amsdb` instead.
- **`disk I/O error` / `unable to open database file` on PythonAnywhere** → You imported a file with a stale WAL. The smooth file is WAL-clean; always export via `full_db_sync` for PA.
- **`no such table: supplier` after import** → Your target DB had no tables (you deleted instance/). Start the app once (`python main.py` or reload web app) to bootstrap the schema, then import again.
- **App won't start after import (`health_snapshot` mismatch)** → Imports rebaseline health automatically. If you used a manual SQLite copy, delete `instance/health_snapshot.json` once and reload.

---

## Need help?

Open an issue with the output of:

```bash
python -m full_db_sync verify --db "LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb"
```

It should end with `ok: true`, `integrity: ok`, `fk_violations: 0`, `total_rows: 39269`.

*Generated 2026-10-20 by arena fix — no business rows were invented, only the schema gap was filled.*
