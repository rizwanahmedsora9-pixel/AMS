"""The supplier ledger's Closing balance card must show the FINAL closing balance.

Regression guard for a pagination bug: the card used to render the running
balance of the *last row on the current page*, so opening a multi-page ledger
showed a mid-history figure (and only the final page happened to be right).
The card sits next to the unfiltered Opening balance / Total debit / Total
credit summaries, so it must always show the real ledger closing balance no
matter which page or filters are active.

Expected numbers are recomputed here from the seeded ORM rows instead of being
read back from the same helper the route uses, so a mismatch means the shipped
page is wrong rather than a helper agreeing with itself.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from models import db, GRN, GRNItem, Supplier, SupplierPayment

GRN_COUNT = 30
GRN_AMOUNT = 1000.0
OPENING = 5000.0
PAYMENT = 3000.0
FINAL_CLOSING = OPENING + (GRN_COUNT * GRN_AMOUNT) - PAYMENT  # 32,000.00


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    return resp


def _seed_multipage_supplier(app):
    """One supplier whose ledger spans several pages at per_page=10.

    Opening 5,000 (payable) + 30 GRNs of 1,000 each, then a single 3,000
    payment dated after every GRN.  The final closing is therefore 32,000 while
    the balance at the end of page 1 is only 14,000 — the two must never be
    confused on screen.
    """
    with app.app_context():
        supplier = Supplier(
            name="Closing Balance Traders",
            phone="0300-7777777",
            address="Swabi",
            opening_balance=OPENING,
            # Must predate the movements, otherwise the OPENING row sorts last
            # (the column defaults to "now") and the page balances shift.
            opening_balance_date=datetime(2026, 1, 1, 9, 0, 0),
            is_active=True,
        )
        db.session.add(supplier)
        db.session.commit()
        supplier_id = supplier.id

        for index in range(GRN_COUNT):
            grn = GRN(
                supplier=supplier.name,
                supplier_id=supplier_id,
                manual_bill_no=f"GRN-CB-{index + 1:03d}",
                date_posted=datetime(2026, 3, 1 + index, 9, 0, 0),
            )
            db.session.add(grn)
            db.session.flush()
            db.session.add(GRNItem(
                grn_id=grn.id, mat_name="Cement", qty=10.0, price_at_time=100.0,
            ))
        db.session.add(SupplierPayment(
            supplier_id=supplier_id,
            amount=PAYMENT,
            payment_type="Payment",
            method="Cash",
            manual_bill_no="PAY-CB-001",
            date_posted=datetime(2026, 4, 20, 9, 0, 0),
        ))
        db.session.commit()
        return supplier_id


def _closing_card(html):
    """Return the headline number rendered in the 'Closing balance' card."""
    match = re.search(
        r"Closing balance</div>\s*<div[^>]*>\s*Rs\.\s*([-0-9,.]+)\s*</div>",
        html,
        re.DOTALL,
    )
    assert match, "Closing balance card not found in rendered ledger page"
    return float(match.group(1).replace(",", ""))


def _running_balances(html):
    """Every running-balance cell rendered in the ledger table, in order."""
    return [
        float(value.replace(",", ""))
        for value in re.findall(r'fw-bold ledger-number">([-0-9,]+\.\d{2})</td>', html)
    ]


def test_service_closing_matches_independent_recomputation(app):
    """Fixture sanity: opening + credits - debits = 32,000 across 32 rows."""
    from app.services.financial_ledgers import build_supplier_financial_ledger

    supplier_id = _seed_multipage_supplier(app)
    with app.app_context():
        supplier = db.session.get(Supplier, supplier_id)
        ledger = build_supplier_financial_ledger(supplier)
        assert ledger["closing_balance"] == pytest.approx(FINAL_CLOSING)
        assert ledger["rows"][-1]["balance"] == pytest.approx(FINAL_CLOSING)
        # The ledger must genuinely span several pages at per_page=10.
        assert len(ledger["filtered_rows"]) == GRN_COUNT + 2
        # Page 1 really does end on a different, smaller balance.
        assert ledger["filtered_rows"][9]["balance"] == pytest.approx(14000.0)


@pytest.mark.parametrize("page", [1, 2, 4])
def test_closing_card_shows_final_balance_on_every_page(app, client, page):
    """The card is identical on the first, a middle and the last page."""
    supplier_id = _seed_multipage_supplier(app)
    _login(client)

    resp = client.get(f"/supplier_ledger/{supplier_id}?per_page=10&page={page}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert f"Page {page} of 4" in html, "expected a four-page ledger"
    assert _closing_card(html) == pytest.approx(FINAL_CLOSING)


def test_closing_card_agrees_with_last_row_of_last_page(app, client):
    """Card, table and banner must all agree on the real closing balance."""
    supplier_id = _seed_multipage_supplier(app)
    _login(client)

    first = client.get(f"/supplier_ledger/{supplier_id}?per_page=10&page=1")
    last = client.get(f"/supplier_ledger/{supplier_id}?per_page=10&page=4")
    first_html, last_html = first.get_data(as_text=True), last.get_data(as_text=True)

    card_first, card_last = _closing_card(first_html), _closing_card(last_html)
    assert card_first == card_last == pytest.approx(FINAL_CLOSING)

    # On page 1 the visible rows stop well before the final balance.
    assert _running_balances(first_html)[-1] == pytest.approx(14000.0)
    # On the last page the final visible row *is* the closing balance.
    assert _running_balances(last_html)[-1] == pytest.approx(FINAL_CLOSING)


def test_closing_card_stays_real_when_filters_hide_the_last_row(app, client):
    """A date filter excluding the final movement must not change the card.

    The summary row is an all-time projection (opening balance, total debit and
    total credit are all unfiltered), so the closing card must stay the real
    ledger closing balance instead of the balance of the last *visible* row.
    """
    supplier_id = _seed_multipage_supplier(app)
    _login(client)

    # Cuts off long before the 2026-04-20 payment.  Visible rows are the
    # 5,000 carry-forward plus six 1,000 GRNs, ending at 11,000.
    resp = client.get(
        f"/supplier_ledger/{supplier_id}?start_date=2026-03-01&end_date=2026-03-06"
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _running_balances(html)[-1] == pytest.approx(11000.0)
    assert _closing_card(html) == pytest.approx(FINAL_CLOSING)


def test_closing_card_stays_real_on_a_filtered_multi_page_view(app, client):
    """Filtered *and* paginated: still the final closing balance."""
    supplier_id = _seed_multipage_supplier(app)
    _login(client)

    for page in (1, 3):
        resp = client.get(
            f"/supplier_ledger/{supplier_id}?type=GRN&per_page=10&page={page}"
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f"Page {page} of 3" in html, "expected the GRN-only view to span 3 pages"
        assert _closing_card(html) == pytest.approx(FINAL_CLOSING)
