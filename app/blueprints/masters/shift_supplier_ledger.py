"""Full supplier-ledger shift endpoints.

* ``GET  /supplier_ledger_shift/<id>``            preview + confirmation page
* ``GET  /api/supplier_ledger_shift_preview/<id>``JSON preview (target switch)
* ``POST /shift_supplier_ledger/<id>``            move the whole ledger
* ``POST /shift_supplier_payment/<id>``           move one standalone payment
* ``POST /reclaim_supplier_ledger/<id>``          undo the last shift

All of them require ``can_manage_suppliers`` and every mutation is committed as
one transaction by the service layer, so a failure leaves both suppliers exactly
as they were.
"""
from ._common import *  # noqa

_OPTION_FIELDS = (
    'move_opening_balance',
    'move_legacy_entries',
    'move_cash_flow',
    'relink_accounts',
    'rename_accounts',
    'deactivate_source',
)


_FALSEY = ('', '0', 'false', 'off', 'no', 'none')


def _truthy(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSEY


def _form_options():
    """Checkbox options as submitted (an unticked box simply posts nothing)."""
    return {field: _truthy(request.form.get(field)) for field in _OPTION_FIELDS}


def _requested_options():
    """Options for the preview from query args (defaults stay on)."""
    return {field: _truthy(request.args.get(field), default=True)
            for field in _OPTION_FIELDS if request.args.get(field) is not None}


def _shift_target_from_request():
    """Resolve the receiving supplier from either the picker or the new-name box."""
    from app.services.supplier_ledger_shift import create_target_supplier

    new_name = (request.form.get('new_supplier_name') or '').strip()
    if new_name:
        phone = (request.form.get('new_supplier_phone') or '').strip()
        target, created = create_target_supplier(new_name, phone=phone, actor=current_user)
        return target, created

    raw = request.form.get('target_supplier_id') or request.form.get('target_id')
    if raw in (None, '', '0'):
        return None, False
    try:
        supplier = db.session.get(Supplier, int(raw))
    except (TypeError, ValueError):
        supplier = None
    return supplier, False


def _supplier_options(exclude_id=None):
    query = Supplier.query.order_by(Supplier.name.asc(), Supplier.id.asc())
    rows = query.all()
    return [
        {'id': s.id, 'name': s.name, 'is_active': bool(s.is_active),
         'opening_balance': float(s.opening_balance or 0)}
        for s in rows if (exclude_id is None or s.id != exclude_id)
    ]


@bp.route('/supplier_ledger_shift/<int:id>', methods=['GET'])
@login_required
def supplier_ledger_shift_page(id):
    """Preview everything that will move before the user confirms."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))

    supplier = db.session.get(Supplier, id)
    if not supplier:
        flash('Supplier not found.', 'warning')
        return redirect(url_for('suppliers'))

    from app.services.supplier_ledger_shift import (
        DEFAULT_OPTIONS,
        preview_supplier_ledger_shift,
    )

    options = {**DEFAULT_OPTIONS, **_requested_options()}
    target_id = request.args.get('target_supplier_id') or request.args.get('target_id')
    target = None
    if target_id not in (None, '', '0'):
        try:
            target = db.session.get(Supplier, int(target_id))
        except (TypeError, ValueError):
            target = None

    preview = preview_supplier_ledger_shift(supplier, target, **options)
    return render_template(
        'supplier_ledger_shift.html',
        supplier=supplier,
        target=target,
        suppliers=_supplier_options(exclude_id=supplier.id),
        preview=preview,
        options=options,
        option_fields=_OPTION_FIELDS,
        back_url=url_for('supplier_ledger', id=supplier.id),
    )


@bp.route('/api/supplier_ledger_shift_preview/<int:id>', methods=['GET'])
@login_required
def api_supplier_ledger_shift_preview(id):
    """JSON preview so the picker can refresh counts without a page reload."""
    if not _user_can('can_manage_suppliers'):
        return jsonify({'error': 'Permission denied'}), 403
    supplier = db.session.get(Supplier, id)
    if not supplier:
        return jsonify({'error': 'Supplier not found'}), 404
    from app.services.supplier_ledger_shift import preview_supplier_ledger_shift
    target_id = request.args.get('target_supplier_id') or request.args.get('target_id')
    target = None
    if target_id not in (None, '', '0'):
        try:
            target = db.session.get(Supplier, int(target_id))
        except (TypeError, ValueError):
            target = None
    try:
        preview = preview_supplier_ledger_shift(supplier, target, **_requested_options())
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'ok': True, 'preview': preview})


@bp.route('/shift_supplier_ledger/<int:id>', methods=['POST'])
@login_required
def shift_supplier_ledger(id):
    """Move every ledger entry (GRNs, payments, opening balance, accounts,
    cash-flow rows) of this supplier to the correct / new supplier."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))

    supplier = db.session.get(Supplier, id)
    if not supplier:
        flash('Supplier not found.', 'warning')
        return redirect(url_for('suppliers'))

    options = _form_options()
    if str(request.form.get('confirm') or '').strip().lower() not in ('1', 'on', 'true', 'yes'):
        flash('Tick the confirmation box to shift this supplier ledger.', 'warning')
        return redirect(url_for('supplier_ledger_shift_page', id=supplier.id))

    try:
        target, target_created = _shift_target_from_request()
        if not target:
            flash('Pick the supplier that should receive this ledger, or type a new supplier name.', 'danger')
            return redirect(url_for('supplier_ledger_shift_page', id=supplier.id))

        from app.services.supplier_ledger_shift import shift_supplier_ledger as do_shift
        result = do_shift(supplier, target, actor=current_user, **options)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('supplier_ledger_shift_page', id=supplier.id))
    except Exception:
        db.session.rollback()
        logging.exception('Supplier ledger shift failed')
        flash('Unable to shift this supplier ledger. Nothing was changed — please try again.', 'danger')
        return redirect(url_for('supplier_ledger_shift_page', id=supplier.id))

    flash(
        f"Shifted {result['grns_moved']} GRN(s), {result['payments_moved']} payment(s) "
        f"(Rs {result['payments_total']:,.2f}) and the opening balance "
        f"Rs {result['opening_balance_moved']:,.2f} from '{result['source_name']}' to "
        f"'{result['target_name']}'. Linked accounts: {len(result['accounts_relinked'])}, "
        f"cash-flow rows: {result['cash_flow_entries_moved']}. "
        f"The old supplier ledger is now empty ({result['source_rows_after']} rows).",
        'success',
    )
    if target_created:
        flash(f"Supplier '{result['target_name']}' was created and is now the owner of this ledger.", 'info')
    for warning in (result.get('warnings') or [])[:3]:
        flash(warning, 'warning')
    if len(result.get('warnings') or []) > 3:
        flash(f"{len(result['warnings']) - 3} more entry warning(s) were recorded in the audit trail.", 'warning')

    return redirect(url_for('supplier_ledger', id=result['target_supplier_id']))


@bp.route('/shift_supplier_payment/<int:id>', methods=['POST'])
@login_required
def shift_supplier_payment(id):
    """Move a single supplier payment row to another supplier."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))

    payment = db.session.get(SupplierPayment, id)
    if not payment:
        flash('Supplier payment not found.', 'warning')
        return redirect(url_for('accounts.supplier_payments'))

    target = None
    raw = request.form.get('target_supplier_id') or request.form.get('target_id')
    if raw not in (None, '', '0'):
        try:
            target = db.session.get(Supplier, int(raw))
        except (TypeError, ValueError):
            target = None

    redirect_to = request.form.get('return_to') or url_for('accounts.supplier_payments')
    try:
        from app.services.supplier_ledger_shift import shift_supplier_payment_to_supplier
        result = shift_supplier_payment_to_supplier(payment, target, actor=current_user)
        db.session.commit()
        if result.get('moved'):
            flash(
                f"Payment PAY-{result['payment_id']} moved from '{result['from_supplier']}' "
                f"to '{result['to_supplier']}'. Accounts and the cash/bank balance were updated.",
                'success',
            )
        else:
            flash('This payment already belongs to the selected supplier.', 'info')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logging.exception('Supplier payment shift failed')
        flash('Unable to move this payment. Nothing was changed — please try again.', 'danger')
    return redirect(redirect_to)


@bp.route('/reclaim_supplier_ledger/<int:id>', methods=['POST'])
@login_required
def reclaim_supplier_ledger(id):
    """Undo the last full ledger shift performed on this supplier."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))

    supplier = db.session.get(Supplier, id)
    if not supplier:
        flash('Supplier not found.', 'warning')
        return redirect(url_for('suppliers'))

    try:
        from app.services.supplier_ledger_shift import reclaim_supplier_ledger as do_reclaim
        result = do_reclaim(supplier, actor=current_user)
        db.session.commit()
        flash(
            f"Reclaimed {result['grns_reclaimed']} GRN(s), {result['payments_reclaimed']} payment(s) and "
            f"Rs {result['opening_balance_reclaimed']:,.2f} opening balance from "
            f"'{result['target_supplier_name']}' back to '{result['source_supplier_name']}'.",
            'success',
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logging.exception('Supplier ledger reclaim failed')
        flash('Unable to reclaim this ledger. Nothing was changed — please try again.', 'danger')
    return redirect(url_for('supplier_ledger', id=id))
