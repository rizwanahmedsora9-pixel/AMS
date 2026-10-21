"""READ-ONLY audit: find historical sales affected by the alternate-material
excess bug.

Before the fix, when a booked row with an alternate material was supplied
with MORE than the remaining booking balance, the excess slice was saved
under the ORIGINALLY BOOKED material instead of the actually delivered
alternate.  Symptoms per affected sale:

  * a chargeable DirectSaleItem / OUT Entry carrying the booked material's
    name on the same sale that also has an is_alternate entry for it;
  * stock deducted from the booked material instead of the alternate;
  * the bill shows the booked material for the excess line.

This script only REPORTS candidates.  It never writes.  To correct a flagged
sale, open it in the app and re-save it through the (fixed) edit flow after
reviewing it with the customer's ledger — that rebuilds items, entries,
stock, FIFO allocations and pending bills consistently in one transaction.

Usage:
    python tools/read_only/alternate_material_audit.py [path/to/db.sqlite]

Defaults to instance/ahmed_cement.db (or $APP_DB_PATH when set).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys


def main() -> int:
    db_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("APP_DB_PATH", "instance/ahmed_cement.db")
    )
    if not os.path.exists(db_path):
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # Alternate booking entries (delivered material != booked material).
    alt_rows = con.execute(
        """
        SELECT source_id AS sale_id, material AS delivered, booked_material AS booked,
               qty, bill_no, date
        FROM entry
        WHERE nimbus_no = 'Direct Sale'
          AND type = 'OUT'
          AND COALESCE(is_void, 0) = 0
          AND COALESCE(is_alternate, 0) = 1
          AND COALESCE(booked_material, '') <> ''
          AND TRIM(booked_material) <> TRIM(material)
          AND source_module = 'sales'
        """
    ).fetchall()

    report = []
    for alt in alt_rows:
        sale_id = alt["sale_id"]
        if sale_id is None:
            continue
        booked = (alt["booked"] or "").strip()
        # Chargeable (non-booking) rows on the same sale that still carry the
        # booked material's name — the signature of the pre-fix excess bug.
        suspicious = con.execute(
            """
            SELECT id, material, qty, client_category
            FROM entry
            WHERE nimbus_no = 'Direct Sale'
              AND type = 'OUT'
              AND COALESCE(is_void, 0) = 0
              AND source_module = 'sales'
              AND source_id = ?
              AND COALESCE(is_alternate, 0) = 0
              AND TRIM(material) = TRIM(?)
            """,
            (sale_id, booked),
        ).fetchall()
        if not suspicious:
            continue
        sale = con.execute(
            "SELECT id, client_name, client_code, manual_bill_no, auto_bill_no,"
            "       amount, category, date_posted, COALESCE(is_void,0) AS is_void "
            "FROM direct_sale WHERE id = ?",
            (sale_id,),
        ).fetchone()
        report.append({
            "sale_id": sale_id,
            "sale": dict(sale) if sale else None,
            "alternate_line": dict(alt),
            "suspect_excess_lines": [dict(r) for r in suspicious],
            "suggested_action": (
                "Review with the customer ledger, then re-save via the sale "
                "edit form so the excess line is rebuilt under the delivered "
                "material. Stock, FIFO and pending bills are rebuilt "
                "automatically on save."
            ),
        })

    print(json.dumps({
        "db": db_path,
        "alternate_sales_scanned": len(alt_rows),
        "affected_sales": len(report),
        "details": report,
    }, indent=2, default=str))
    return 0 if not report else 1


if __name__ == "__main__":
    raise SystemExit(main())
