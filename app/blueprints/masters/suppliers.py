from ._common import *  # noqa

# The supplier list is a read-only projection: the authoritative ledger
# numbers come from ``build_supplier_payable_overview`` and are only filtered
# or sorted in memory here.  Nothing in this module writes financial data.
SUPPLIER_SORT_CHOICES = (
    ('name_asc', 'Name (A–Z)'),
    ('name_desc', 'Name (Z–A)'),
    ('balance_desc', 'Balance (high → low)'),
    ('balance_asc', 'Balance (low → high)'),
    ('purchase_desc', 'Purchase (high → low)'),
    ('purchase_asc', 'Purchase (low → high)'),
    ('paid_desc', 'Paid (high → low)'),
)

SUPPLIER_STATUS_CHOICES = (
    ('all', 'All statuses'),
    ('payable', 'Payable'),
    ('settled', 'Settled'),
    ('advance', 'Advance / prepaid'),
    ('inactive', 'Inactive suppliers'),
)


def supplier_display_code(supplier_or_id):
    """Stable, human-readable code derived from the supplier's database id.

    Suppliers have no separate code column in the schema, so the list shows a
    deterministic ``SUP-0001`` style identifier instead of changing the data
    model.  It is also accepted by the page's search box.
    """
    supplier_id = getattr(supplier_or_id, 'id', supplier_or_id)
    try:
        return f"SUP-{int(supplier_id):04d}"
    except (TypeError, ValueError):
        return 'SUP-—'


def _parse_balance_arg(value):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _supplier_matches_search(row, query):
    query = ' '.join(str(query or '').strip().casefold().split())
    if not query:
        return True

    name = str(row.get('name') or '').casefold()
    code = str(row.get('code') or '').casefold()
    phone = str(row.get('phone') or '').casefold()
    if query in name or query in code or query in phone:
        return True

    digits = re.sub(r'\D', '', query)
    if digits:
        phone_digits = re.sub(r'\D', '', phone)
        if len(digits) >= 3 and digits in phone_digits:
            return True
        if len(digits) >= 2 and digits in code:
            return True

    # Accept both "SUP-7" and the bare database id.
    code_query = query
    for prefix in ('sup-', 'sup'):
        if code_query.startswith(prefix):
            code_query = code_query[len(prefix):]
            break
    if code_query.isdigit():
        try:
            return int(code_query) == int(row.get('id') or 0)
        except (TypeError, ValueError):
            return False
    return False


def _supplier_row(supplier, overview):
    info = overview.get(supplier.id) or {}
    opening = float(supplier.opening_balance or 0)
    balance = float(info.get('balance', opening) or 0)
    status = info.get('status')
    if not status:
        status = 'Payable' if balance > 0.005 else ('Advance' if balance < -0.005 else 'Settled')
    return {
        'id': supplier.id,
        'supplier': supplier,
        'name': (supplier.name or '').strip(),
        'code': supplier_display_code(supplier),
        'phone': (supplier.phone or '').strip(),
        'address': (supplier.address or '').strip(),
        'opening_balance': opening,
        'total_purchase': float(info.get('total_purchase') or 0),
        'total_paid': float(info.get('total_paid') or 0),
        'total_credit': float(info.get('total_credit') or 0),
        'balance': balance,
        'outstanding': float(info.get('outstanding', max(balance, 0.0)) or 0),
        'advance': float(info.get('advance', max(-balance, 0.0)) or 0),
        'status': status,
        'is_active': bool(supplier.is_active),
        'transferred_to_id': supplier.transferred_to_id,
        'last_purchase_date': info.get('last_purchase_date'),
        'last_payment_date': info.get('last_payment_date'),
    }


@bp.route('/suppliers')
@login_required
def suppliers():
    suppliers_list = Supplier.query.order_by(Supplier.name.asc(), Supplier.id.asc()).all()
    # Bounded projection (2 queries + per-supplier payment buckets) instead of
    # building a full financial ledger per supplier (which issued a payment
    # lookup per GRN per supplier and got slower with every GRN).
    try:
        overview = build_supplier_payable_overview(suppliers_list)
    except Exception:
        logging.exception('Supplier payable summaries failed; falling back to opening balances')
        overview = {}

    all_rows = [_supplier_row(supplier, overview) for supplier in suppliers_list]

    query = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or 'all').strip().lower()
    valid_statuses = {key for key, _label in SUPPLIER_STATUS_CHOICES}
    if status not in valid_statuses:
        status = 'all'

    balance_min = _parse_balance_arg(request.args.get('balance_min'))
    balance_max = _parse_balance_arg(request.args.get('balance_max'))
    if balance_min is not None and balance_max is not None and balance_min > balance_max:
        balance_min, balance_max = balance_max, balance_min

    sort = (request.args.get('sort') or 'name_asc').strip().lower()
    valid_sorts = {key for key, _label in SUPPLIER_SORT_CHOICES}
    if sort not in valid_sorts:
        sort = 'name_asc'

    rows = [row for row in all_rows if _supplier_matches_search(row, query)]
    if status == 'inactive':
        rows = [row for row in rows if not row['is_active']]
    elif status == 'payable':
        rows = [row for row in rows if row['balance'] > 0.005]
    elif status == 'settled':
        rows = [row for row in rows if abs(row['balance']) <= 0.005]
    elif status == 'advance':
        rows = [row for row in rows if row['balance'] < -0.005]
    if balance_min is not None:
        rows = [row for row in rows if row['balance'] >= balance_min]
    if balance_max is not None:
        rows = [row for row in rows if row['balance'] <= balance_max]

    if sort == 'name_desc':
        rows.sort(key=lambda row: (row['name'].casefold(), row['id']), reverse=True)
    elif sort == 'balance_desc':
        rows.sort(key=lambda row: (-row['balance'], row['name'].casefold(), row['id']))
    elif sort == 'balance_asc':
        rows.sort(key=lambda row: (row['balance'], row['name'].casefold(), row['id']))
    elif sort == 'purchase_desc':
        rows.sort(key=lambda row: (-row['total_purchase'], row['name'].casefold(), row['id']))
    elif sort == 'purchase_asc':
        rows.sort(key=lambda row: (row['total_purchase'], row['name'].casefold(), row['id']))
    elif sort == 'paid_desc':
        rows.sort(key=lambda row: (-row['total_paid'], row['name'].casefold(), row['id']))
    else:
        rows.sort(key=lambda row: (row['name'].casefold(), row['id']))

    summary = {
        'supplier_count': len(rows),
        'total_supplier_count': len(all_rows),
        'total_purchase': round(sum(row['total_purchase'] for row in rows), 2),
        'total_paid': round(sum(row['total_paid'] for row in rows), 2),
        'total_outstanding': round(sum(row['outstanding'] for row in rows), 2),
        'total_advance': round(sum(row['advance'] for row in rows), 2),
        'payable_count': sum(1 for row in rows if row['status'] == 'Payable'),
        'settled_count': sum(1 for row in rows if row['status'] == 'Settled'),
        'advance_count': sum(1 for row in rows if row['status'] == 'Advance'),
        'inactive_count': sum(1 for row in rows if not row['is_active']),
    }
    filters = {
        'q': query,
        'status': status,
        'balance_min': '' if balance_min is None else f'{balance_min:.2f}',
        'balance_max': '' if balance_max is None else f'{balance_max:.2f}',
        'sort': sort,
    }
    supplier_balances = {row['id']: row['balance'] for row in all_rows}

    return render_template(
        'suppliers.html',
        suppliers=suppliers_list,
        supplier_rows=rows,
        supplier_balances=supplier_balances,
        supplier_overview=overview,
        summary=summary,
        filters=filters,
        active_sort=sort,
        sort_choices=SUPPLIER_SORT_CHOICES,
        status_choices=SUPPLIER_STATUS_CHOICES,
        total_count=len(all_rows),
        matched_count=len(rows),
        filters_active=bool(
            query or status != 'all' or balance_min is not None
            or balance_max is not None or sort != 'name_asc'
        ),
    )
