"""GRN edit form must never print the literal ``None`` into a field.

Every optional column on ``GRN`` is nullable.  The edit form rendered them
with ``{{ edit_grn.x if edit_grn else '' }}``, which only guards against a
missing *record* — when the record exists but the column is ``NULL`` the
expression evaluates to ``None`` and Jinja prints the string ``"None"``.

For ``<input type="text">`` that puts the visible word ``None`` in the box and
it gets saved back on the next submit.  For ``<input type="date">`` it is an
invalid value, so the browser shows an empty box and the stored date is lost on
save.  Dates also need ``%Y-%m-%d``; ``str(date)`` happens to match, but relying
on that is accidental.
"""
from __future__ import annotations

import re
from datetime import date

from models import db, GRN
from tests.test_grn_datetime_override import seed_supplier_material

LOGIN = {"username": "Admin", "password": "Admin@fbm12345"}

# Optional GRN columns that the edit form renders into a value attribute.
GUARDED_FIELDS = (
    "supplier",
    "bank_name",
    "account_name",
    "account_no",
    "supplier_invoice_no",
    "manual_bill_no",
    "bill_date",
    "due_date",
)


def _add_grn(client, supplier_id, supplier, material, **overrides):
    data = {
        "action": "add",
        "supplier": supplier,
        "supplier_id": str(supplier_id),
        "date": "2026-10-07",
        "time": "08:15:45",
        "payment_type": "Credit",
        "mat_name[]": [material],
        "qty[]": ["50"],
        "price[]": ["600"],
    }
    data.update(overrides)
    return client.post("/grn", data=data, follow_redirects=True)


def _grn_id(marker):
    grn = GRN.query.filter(GRN.manual_bill_no.like(f"%{marker}%")).first()
    assert grn is not None, f"GRN {marker!r} was not created"
    return grn.id


def _value_of(html, field):
    """Return the value attribute of the named input, or None if absent."""
    m = re.search(
        r'<input[^>]*\bname="%s"[^>]*\bvalue="([^"]*)"' % re.escape(field), html)
    if m:
        return m.group(1)
    m = re.search(
        r'<input[^>]*\bvalue="([^"]*)"[^>]*\bname="%s"' % re.escape(field), html)
    return m.group(1) if m else None


def test_edit_form_renders_no_literal_none_for_null_columns(app, client):
    """A GRN saved with its optional columns empty must not show "None"."""
    client.post("/login", data=LOGIN, follow_redirects=True)
    sid = seed_supplier_material(app)

    # Deliberately omit manual_bill_no / supplier_invoice_no / dates / note.
    _add_grn(client, sid, "Override Supplier", "Override Cement")

    with app.app_context():
        grn = GRN.query.order_by(GRN.id.desc()).first()
        gid = grn.id
        assert grn.bill_date is None and grn.due_date is None

    html = client.get(f"/edit_grn/{gid}").get_data(as_text=True)

    offenders = re.findall(r'value="None"', html)
    assert not offenders, f'edit form still renders value="None" ({len(offenders)}x)'

    # The textarea body must not contain the word either.
    assert not re.search(
        r'<textarea[^>]*name="note"[^>]*>\s*None\s*</textarea>', html, re.S)

    # Date inputs must be empty, not invalid, when the column is NULL.
    assert _value_of(html, "bill_date") == ""
    assert _value_of(html, "due_date") == ""


def test_edit_form_renders_stored_dates_in_iso_format(app, client):
    """When the dates are set they must come back as YYYY-MM-DD."""
    client.post("/login", data=LOGIN, follow_redirects=True)
    sid = seed_supplier_material(app)
    _add_grn(client, sid, "Override Supplier", "Override Cement",
             manual_bill_no="NULLFIX-1", supplier_invoice_no="SUP-INV-9")

    with app.app_context():
        grn = GRN.query.filter(GRN.manual_bill_no.like("%NULLFIX-1%")).first()
        gid = grn.id
        grn.bill_date = date(2026, 9, 30)
        grn.due_date = date(2026, 11, 15)
        db.session.commit()

    html = client.get(f"/edit_grn/{gid}").get_data(as_text=True)

    assert _value_of(html, "bill_date") == "2026-09-30"
    assert _value_of(html, "due_date") == "2026-11-15"
    assert _value_of(html, "manual_bill_no").endswith("NULLFIX-1")
    assert _value_of(html, "supplier_invoice_no") == "SUP-INV-9"


def test_manual_bill_no_round_trip_does_not_become_none(app, client):
    """The word "None" must not be saved back into the record on re-submit."""
    client.post("/login", data=LOGIN, follow_redirects=True)
    sid = seed_supplier_material(app)
    _add_grn(client, sid, "Override Supplier", "Override Cement",
             manual_bill_no="NULLFIX-2")

    with app.app_context():
        gid = GRN.query.filter(GRN.manual_bill_no.like("%NULLFIX-2%")).first().id

    html = client.get(f"/edit_grn/{gid}").get_data(as_text=True)
    assert "None" not in (_value_of(html, "supplier_invoice_no") or "")

    with app.app_context():
        grn = GRN.query.get(gid)
        assert grn.supplier_invoice_no != "None"
        assert grn.note != "None"


def test_every_guarded_field_is_present_on_the_edit_form(app, client):
    """Guard against a future rename silently dropping a field from the check."""
    client.post("/login", data=LOGIN, follow_redirects=True)
    sid = seed_supplier_material(app)
    _add_grn(client, sid, "Override Supplier", "Override Cement",
             manual_bill_no="NULLFIX-3")

    with app.app_context():
        gid = GRN.query.filter(GRN.manual_bill_no.like("%NULLFIX-3%")).first().id

    html = client.get(f"/edit_grn/{gid}").get_data(as_text=True)
    for field in GUARDED_FIELDS:
        assert _value_of(html, field) is not None, f"field {field!r} not rendered"
