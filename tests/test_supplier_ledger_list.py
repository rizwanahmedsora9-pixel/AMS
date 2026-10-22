"""Supplier Ledger page is a list/table (not supplier cards).

These tests drive the real ``/suppliers`` route against a seeded SQLite
database and verify the three things the page must never lose:

* the authoritative ledger figures (purchase / paid / payable / advance),
* search, status/balance filters and all three sort orders,
* the per-supplier ledger, payment, edit and manage actions.

They also assert the rendered markup uses a table/list with responsive
``data-label`` cells instead of the old card grid.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from models import db, GRN, GRNItem, Supplier, SupplierPayment
from app.services.financial_ledgers import (
    build_supplier_financial_ledger,
    build_supplier_payable_overview,
    build_supplier_payable_summaries,
)


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    return resp


def _seed(app):
    with app.app_context():
        alpha = Supplier(name="Alpha Cement Traders", phone="0300-1111111",
                         address="Ring Road", opening_balance=1000.0, is_active=True)
        beta = Supplier(name="Beta Steel House", phone="0321-2222222",
                        opening_balance=0.0, is_active=True)
        gamma = Supplier(name="Gamma Advance Co", phone="0333-3333333",
                         opening_balance=-500.0, is_active=True)
        delta = Supplier(name="Delta Hardware", phone="0345-4444444",
                         opening_balance=0.0, is_active=True)
        dormant = Supplier(name="Inactive Supplier", phone="", opening_balance=0.0,
                           is_active=False)
        db.session.add_all([alpha, beta, gamma, delta, dormant])
        db.session.commit()
        alpha_id, beta_id, gamma_id, delta_id, dormant_id = (
            alpha.id, beta.id, gamma.id, delta.id, dormant.id,
        )
        # The dormant supplier's ledger was shifted to Alpha (reclaim action).
        dormant.transferred_to_id = alpha_id

        grn_alpha = GRN(
            supplier=alpha.name, supplier_id=alpha_id, manual_bill_no="GRN-A-1",
            date_posted=datetime(2026, 1, 10, 9, 0, 0),
        )
        grn_beta = GRN(
            supplier=beta.name, supplier_id=beta_id, manual_bill_no="GRN-B-1",
            paid_amount=2000.0, payment_type="Cash",
            date_posted=datetime(2026, 1, 12, 9, 0, 0),
        )
        db.session.add_all([grn_alpha, grn_beta])
        db.session.flush()
        db.session.add_all([
            GRNItem(grn_id=grn_alpha.id, mat_name="Cement", qty=10.0, price_at_time=500.0),
            GRNItem(grn_id=grn_beta.id, mat_name="Steel", qty=4.0, price_at_time=500.0),
        ])
        # Alpha paid 1,500 of a 5,000 purchase: payable 4,500 (plus opening 1,000).
        db.session.add(SupplierPayment(
            supplier_id=alpha_id, amount=1500.0, payment_type="Payment",
            method="Cash", manual_bill_no="PAY-A-1",
            date_posted=datetime(2026, 1, 15, 9, 0, 0),
        ))
        # Beta's 2,000 GRN was auto-paid by the linked canonical payment.
        db.session.add(SupplierPayment(
            supplier_id=beta_id, amount=2000.0, payment_type="Payment",
            method="Cash", source_type="GRN", source_id=grn_beta.id,
            date_posted=datetime(2026, 1, 12, 9, 0, 0),
        ))
        db.session.commit()
        return {
            "alpha": alpha_id, "beta": beta_id, "gamma": gamma_id,
            "delta": delta_id, "dormant": dormant_id,
        }


def _tbody(body: str) -> str:
    start = body.index("<tbody>")
    end = body.index("</tbody>", start)
    return body[start:end]


def _row_names(body: str) -> list[str]:
    return [
        name.strip()
        for name in re.findall(
            r'class="supplier-name-link"[^>]*>\s*([^<]+?)\s*</a>',
            _tbody(body),
        )
    ]


def _supplier_row(body: str, supplier_id: int) -> str:
    match = re.search(
        rf'<tr class="[^"]*">\s*<td class="col-name".*?supplier_ledger/{supplier_id}.*?</tr>',
        body,
        re.S,
    )
    assert match, f"supplier row {supplier_id} not found"
    return match.group(0)


def _get(client, app, path):
    with app.app_context():
        resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    return resp.get_data(as_text=True)


@pytest.fixture()
def seeded(app):
    return _seed(app)


def test_supplier_page_renders_a_list_not_supplier_cards(app, client, seeded):
    _login(client)
    body = _get(client, app, "/suppliers")
    tbody = _tbody(body)

    assert 'class="supplier-table"' in body
    assert 'row-cols-1 row-cols-md-2 row-cols-lg-3' not in body
    assert 'card h-100 border-secondary' not in body
    assert "Supplier Ledger" in body

    for heading in (
        "Supplier Name", "Supplier Code", "Phone Number", "Total Purchase",
        "Total Paid", "Outstanding Payable", "Advance Balance",
        "Payment Status", "Actions",
    ):
        assert heading in body, heading

    # Every supplier appears once in the list, including inactive history.
    names = _row_names(body)
    assert len(names) == 5
    assert names == [
        "Alpha Cement Traders", "Beta Steel House", "Delta Hardware",
        "Gamma Advance Co", "Inactive Supplier",
    ]

    # Mobile cells carry their own labels; tablet keeps aligned columns in a
    # horizontally scrollable wrapper.
    assert 'data-label="Supplier Code"' in tbody
    assert "overflow-x: auto" in body
    assert "@media (max-width: 767.98px)" in body


def test_supplier_financial_columns_use_authoritative_ledger_figures(app, client, seeded):
    _login(client)
    body = _get(client, app, "/suppliers")
    alpha_row = _supplier_row(body, seeded["alpha"])
    beta_row = _supplier_row(body, seeded["beta"])
    gamma_row = _supplier_row(body, seeded["gamma"])

    assert "SUP-0001" in alpha_row
    assert "5,000.00" in alpha_row        # purchases
    assert "1,500.00" in alpha_row        # payments
    assert "4,500.00" in alpha_row        # outstanding payable
    assert "Opening 1,000.00" in alpha_row
    assert "Payable" in alpha_row

    assert "SUP-0002" in beta_row
    assert "2,000.00" in beta_row
    assert "Settled" in beta_row

    assert "500.00" in gamma_row
    assert "Advance" in gamma_row

    # The summary projection agrees with the per-supplier ledger to the cent.
    with app.app_context():
        suppliers = Supplier.query.order_by(Supplier.id.asc()).all()
        overview = build_supplier_payable_overview(suppliers)
        summaries = build_supplier_payable_summaries(suppliers)
        assert summaries == {sid: info["balance"] for sid, info in overview.items()}
        for supplier in suppliers:
            ledger = build_supplier_financial_ledger(supplier)
            assert round(ledger["closing_balance"], 2) == overview[supplier.id]["balance"]
            assert overview[supplier.id]["outstanding"] == max(overview[supplier.id]["balance"], 0.0)
            assert overview[supplier.id]["advance"] == max(-overview[supplier.id]["balance"], 0.0)

        alpha = db.session.get(Supplier, seeded["alpha"])
        alpha_overview = overview[alpha.id]
        assert alpha_overview["total_purchase"] == 5000.0
        assert alpha_overview["total_paid"] == 1500.0
        assert alpha_overview["balance"] == 4500.0
        assert alpha_overview["status"] == "Payable"


def test_supplier_search_by_name_code_and_phone(app, client, seeded):
    _login(client)

    by_name = _row_names(_get(client, app, "/suppliers?q=Gamma"))
    assert by_name == ["Gamma Advance Co"]

    by_code = _row_names(_get(client, app, "/suppliers?q=SUP-0002"))
    assert by_code == ["Beta Steel House"]

    by_short_code = _row_names(_get(client, app, "/suppliers?q=SUP-3"))
    assert by_short_code == ["Gamma Advance Co"]

    by_phone = _row_names(_get(client, app, "/suppliers?q=0345-4444444"))
    assert by_phone == ["Delta Hardware"]

    by_phone_digits = _row_names(_get(client, app, "/suppliers?q=0321"))
    assert by_phone_digits == ["Beta Steel House"]

    no_match = _row_names(_get(client, app, "/suppliers?q=does-not-exist"))
    assert no_match == []
    assert "No suppliers match these filters" in _get(client, app, "/suppliers?q=does-not-exist")


def test_supplier_status_and_balance_filters(app, client, seeded):
    _login(client)

    payable = _row_names(_get(client, app, "/suppliers?status=payable"))
    assert payable == ["Alpha Cement Traders"]

    advance = _row_names(_get(client, app, "/suppliers?status=advance"))
    assert advance == ["Gamma Advance Co"]

    inactive = _row_names(_get(client, app, "/suppliers?status=inactive"))
    assert inactive == ["Inactive Supplier"]

    settled = _row_names(_get(client, app, "/suppliers?status=settled"))
    assert settled == ["Beta Steel House", "Delta Hardware", "Inactive Supplier"]

    outstanding_range = _row_names(_get(client, app, "/suppliers?balance_min=1000"))
    assert outstanding_range == ["Alpha Cement Traders"]

    advance_range = _row_names(
        _get(client, app, "/suppliers?balance_min=-600&balance_max=-400")
    )
    assert advance_range == ["Gamma Advance Co"]

    reversed_range = _row_names(
        _get(client, app, "/suppliers?balance_min=5000&balance_max=4000")
    )
    assert reversed_range == ["Alpha Cement Traders"]

    # Invalid numeric input is ignored rather than crashing the page.
    invalid = _row_names(_get(client, app, "/suppliers?balance_min=not-a-number"))
    assert len(invalid) == 5


def test_supplier_sorting_by_name_balance_and_purchase(app, client, seeded):
    _login(client)

    by_name = _row_names(_get(client, app, "/suppliers?sort=name_asc"))
    assert by_name == [
        "Alpha Cement Traders", "Beta Steel House", "Delta Hardware",
        "Gamma Advance Co", "Inactive Supplier",
    ]

    by_name_desc = _row_names(_get(client, app, "/suppliers?sort=name_desc"))
    assert by_name_desc == list(reversed(by_name))

    by_balance = _row_names(_get(client, app, "/suppliers?sort=balance_desc"))
    assert by_balance == [
        "Alpha Cement Traders", "Beta Steel House", "Delta Hardware",
        "Inactive Supplier", "Gamma Advance Co",
    ]

    by_balance_asc = _row_names(_get(client, app, "/suppliers?sort=balance_asc"))
    assert by_balance_asc == [
        "Gamma Advance Co", "Beta Steel House", "Delta Hardware",
        "Inactive Supplier", "Alpha Cement Traders",
    ]

    by_purchase = _row_names(_get(client, app, "/suppliers?sort=purchase_desc"))
    assert by_purchase == [
        "Alpha Cement Traders", "Beta Steel House", "Delta Hardware",
        "Gamma Advance Co", "Inactive Supplier",
    ]

    by_purchase_asc = _row_names(_get(client, app, "/suppliers?sort=purchase_asc"))
    assert by_purchase_asc == [
        "Delta Hardware", "Gamma Advance Co", "Inactive Supplier",
        "Beta Steel House", "Alpha Cement Traders",
    ]

    # Column headers link to the same sort contract.
    body = _get(client, app, "/suppliers")
    assert "sort=name_desc" in body
    assert "sort=balance_desc" in body
    assert "sort=purchase_desc" in body


def test_supplier_row_actions_open_ledger_payments_and_manage_flows(app, client, seeded):
    _login(client)
    body = _get(client, app, "/suppliers")
    alpha_row = _supplier_row(body, seeded["alpha"])

    assert f"/supplier_ledger/{seeded['alpha']}" in alpha_row
    assert "View Ledger" in alpha_row
    assert f"/accounts/payments/suppliers?party_id={seeded['alpha']}" in alpha_row
    assert f'data-bs-target="#editSupplierModal{seeded["alpha"]}"' in alpha_row
    assert f'data-bs-target="#deleteSupplierModal{seeded["alpha"]}"' in alpha_row

    # The manage modal keeps suspend / transfer / full shift / hard delete and
    # exposes the reclaim action only for a shifted ledger.
    assert f'id="editSupplierModal{seeded["alpha"]}"' in body
    assert f'id="deleteSupplierModal{seeded["alpha"]}"' in body
    assert "Shift Entire Ledger" in body
    assert "Transfer Data" in body
    assert "Permanently Delete" in body
    assert "Reclaim shifted ledger" in body
    assert "Shift / Reclaim this ledger" in body

    # The actual ledger route still opens the supplier's full history.
    ledger_page = _get(client, app, f"/supplier_ledger/{seeded['alpha']}")
    assert "Alpha Cement Traders" in ledger_page
    ledger_page_beta = _get(client, app, f"/supplier_ledger/{seeded['beta']}")
    assert "Beta Steel House" in ledger_page_beta


def test_supplier_page_keeps_payment_workflow_reachable(app, client, seeded):
    _login(client)
    body = _get(client, app, "/suppliers")
    assert "Pay Supplier" in body
    assert "Add Supplier" in body
    assert "/add_supplier" in body
    payments_page = _get(
        client, app, f"/accounts/payments/suppliers?party_id={seeded['alpha']}"
    )
    assert "Supplier Payments" in payments_page
    assert "Alpha Cement Traders" in payments_page


def test_empty_supplier_list_still_renders_the_list_and_add_action(app, client):
    _login(client)
    body = _get(client, app, "/suppliers")
    assert 'class="supplier-table"' in body
    assert "No suppliers yet" in body
    assert "Add Supplier" in body
    assert '<div class="supplier-summary"' in body


def test_supplier_page_post_forms_carry_server_rendered_csrf(app, client, seeded):
    _login(client)
    body = _get(client, app, "/suppliers")
    forms = re.findall(r'<form\b[^>]*method="POST"[^>]*>.*?</form>', body, re.S | re.I)
    assert forms, "supplier page should expose its mutating forms"
    for form in forms:
        assert 'name="_csrf_token"' in form
