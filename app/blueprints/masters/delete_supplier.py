from ._common import *  # noqa
from app.services.void_rebuild import hard_delete_supplier as do_hard_delete_supplier


@bp.route('/delete_supplier/<int:id>', methods=['POST'])
@login_required
def delete_supplier(id, force_hard_delete=False):
    """Archive or permanently hard-delete a supplier.

    By default (or action='archive'), archives/suspends the supplier so
    historical GRNs and payments are preserved.
    When action='hard_delete' or force_hard_delete=True, completely purges
    the supplier and their associated entries so the user can start fresh.
    """
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))

    supplier = db.session.get(Supplier, id)
    if not supplier:
        flash('Supplier not found.', 'warning')
        return redirect(url_for('suppliers'))

    action = request.form.get('action') or request.form.get('mode') or ('hard_delete' if request.form.get('hard_delete') == '1' else 'archive')
    is_hard_delete = force_hard_delete or action == 'hard_delete'

    if is_hard_delete:
        supplier_name = supplier.name
        try:
            res = do_hard_delete_supplier(supplier, actor=current_user)
            db.session.commit()
            flash(
                f"Supplier '{supplier_name}' and all associated entries "
                f"({res.get('grns_deleted', 0)} GRNs, {res.get('payments_deleted', 0)} payments) "
                f"were permanently deleted. You can now enter their data fresh.",
                'success'
            )
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logging.exception('Supplier hard delete failed')
            flash('Unable to permanently delete supplier. Please try again.', 'danger')
        return redirect(url_for('suppliers'))

    # Archive / suspend (preserves historical ledger & transactions)
    from utils.accounting_audit import record_accounting_audit
    before = {'id': supplier.id, 'name': supplier.name, 'is_active': bool(supplier.is_active)}
    supplier.is_active = False
    record_accounting_audit(
        current_user, action='Suspend', entity_type='Supplier', entity_id=supplier.id,
        before=before, after={**before, 'is_active': False},
        party_before_id=supplier.id, party_after_id=supplier.id,
        reason='Supplier archived; historical GRNs/payments preserved', module='suppliers',
    )
    db.session.commit()
    flash('Supplier suspended; historical GRNs and payments were preserved.', 'warning')
    return redirect(url_for('suppliers'))


@bp.route('/hard_delete_supplier/<int:id>', methods=['POST'])
@login_required
def hard_delete_supplier(id):
    """Dedicated endpoint to permanently delete a supplier and all associated data."""
    return delete_supplier(id, force_hard_delete=True)


@bp.route('/transfer_supplier/<int:id>', methods=['POST'])
@login_required
def transfer_supplier(id):
    """Move all GRNs/payments of a (wrong) supplier to the correct supplier."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))
    supplier = db.session.get(Supplier, id)
    if not supplier:
        flash('Supplier not found.', 'warning')
        return redirect(url_for('suppliers'))
    target_id = request.form.get('target_supplier_id') or request.form.get('target_id')
    if not target_id:
        flash('Target supplier is required.', 'danger')
        return redirect(url_for('suppliers'))
    try:
        from app.services.void_rebuild import transfer_supplier as do_transfer_supplier
        res = do_transfer_supplier(supplier, int(target_id), actor=current_user)
        db.session.commit()
        flash(
            f"Transferred {res.get('grns_moved', 0)} GRN(s) and {res.get('payments_moved', 0)} payment(s) "
            f"from '{res.get('source_supplier_name')}' to '{res.get('target_supplier_name')}'. "
            f"Inventory and existing sales were preserved.",
            'success'
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier transfer failed')
        flash('Unable to transfer supplier data. Please try again.', 'danger')
    return redirect(url_for('suppliers'))


@bp.route('/shift_grn_supplier/<int:id>', methods=['POST'])
@login_required
def shift_grn_supplier(id):
    """Move a single GRN's supplier attribution to the correct supplier."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('grn'))
    return_to = (request.form.get('return_to') or '').strip() or url_for('grn')

    def _back():
        # Only same-site paths are honoured (no open redirects).
        return return_to if return_to.startswith('/') and not return_to.startswith('//') else url_for('grn')

    grn = db.session.get(GRN, id)
    if not grn:
        flash('GRN not found.', 'warning')
        return redirect(_back())
    target_id = request.form.get('target_supplier_id') or request.form.get('target_id')
    if not target_id:
        flash('Target supplier is required.', 'danger')
        return redirect(_back())
    try:
        from app.services.void_rebuild import shift_grn_to_supplier as do_shift_grn_supplier
        res = do_shift_grn_supplier(grn, int(target_id), actor=current_user)
        db.session.commit()
        if res.get('moved'):
            flash(f"GRN #{grn.id} shifted from '{res.get('from_supplier')}' to '{res.get('to_supplier')}'. Inventory and sales preserved.", 'success')
        else:
            flash('GRN already belongs to the selected supplier.', 'info')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('GRN supplier shift failed')
        flash('Unable to shift GRN supplier. Please try again.', 'danger')
    return redirect(_back())
