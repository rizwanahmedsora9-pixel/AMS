"""suppliers — split from misc.py."""
from ._common import *  # noqa

@bp.route('/pay_supplier', methods=['GET'])
@login_required
def pay_supplier_page():
    return redirect(url_for('accounts.supplier_payments'))


@bp.route('/supplier_ledger/<int:id>')
@login_required
def supplier_ledger(id):
    """Supplier ledger using the same projection/filter contract as clients."""
    supplier = Supplier.query.get_or_404(id)
    requested_supplier = (request.args.get('supplier_search') or '').strip()
    if requested_supplier:
        alternate = get_supplier_by_input(requested_supplier)
        if alternate and alternate.id != supplier.id:
            preserved = request.args.to_dict()
            preserved.pop('supplier_search', None)
            return redirect(url_for('supplier_ledger', id=alternate.id, **preserved))
    filters = {
        'start_date': (request.args.get('start_date') or request.args.get('date_from') or '').strip(),
        'end_date': (request.args.get('end_date') or request.args.get('date_to') or '').strip(),
        'type_filter': (request.args.get('type') or request.args.get('transaction_type') or '').strip(),
        'query': (request.args.get('q') or request.args.get('search') or '').strip(),
        'amount_min': (request.args.get('amount_min') or '').strip(),
        'amount_max': (request.args.get('amount_max') or '').strip(),
        'account_filter': (request.args.get('account') or '').strip(),
        'status_filter': (request.args.get('status') or '').strip(),
    }
    from app.services.financial_ledgers import (
        build_supplier_financial_ledger,
        ledger_page_closing,
    )
    ledger = build_supplier_financial_ledger(supplier, **filters)
    selected = ledger['filtered_rows']
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    per_page = min(max(request.args.get('per_page', 25, type=int) or 25, 10), 100)
    total = len(selected)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_rows = selected[(page - 1) * per_page: page * per_page]
    # The closing balance is a property of the ledger, never of the visible
    # page: paginate the detail rows but keep the summary card on the final
    # closing balance of the whole ledger.
    closing = ledger_page_closing(ledger, filters)
    suppliers = Supplier.query.filter(Supplier.is_active == True).order_by(Supplier.name.asc(), Supplier.id.asc()).all()
    return render_template(
        'financial_ledger.html',
        entity=supplier,
        entity_type='supplier',
        ledger=ledger,
        rows=page_rows,
        all_rows=ledger['rows'],
        obligations=[],
        filters=filters,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        ledger_closing=closing['final'],
        filtered_closing=closing['filtered'],
        filters_active=closing['filters_active'],
        selector_entities=suppliers,
        current_payable=max(0.0, closing['final']),
        back_url=url_for('suppliers'),
        opening_url=url_for('supplier_opening_balance', id=supplier.id),
        today_date=pk_today().strftime('%Y-%m-%d'),
    )


def _parse_ledger_date(raw, label):
    """Parse an optional ``YYYY-MM-DD`` form value; blank means open-ended."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f'{label} is not a valid date.')


@bp.route('/download_supplier_ledger/<int:id>')
@login_required
def download_supplier_ledger(id):
    """Download a supplier ledger as PDF or CSV.

    ``scope=full`` (default) exports every movement of the ledger.
    ``scope=range`` exports only movements between ``start_date`` and
    ``end_date`` (either may be left blank for an open-ended range).  The
    balance brought forward into the range is included so the statement
    always reconciles.
    """
    supplier = Supplier.query.get_or_404(id)
    action = (request.args.get('action') or 'download').lower()
    scope = (request.args.get('scope') or 'full').strip().lower()

    start = end = None
    if scope == 'range':
        try:
            start = _parse_ledger_date(request.args.get('start_date'), 'From date')
            end = _parse_ledger_date(request.args.get('end_date'), 'To date')
        except ValueError as exc:
            response = make_response(str(exc), 400)
            response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            return response
        if start and end and start > end:
            response = make_response('From date cannot be after To date.', 400)
            response.headers['Content-Type'] = 'text/plain; charset=utf-8'
            return response

    filters = {
        'start_date': start.isoformat() if start else '',
        'end_date': end.isoformat() if end else '',
        'type_filter': '',
        'query': '',
        'amount_min': '',
        'amount_max': '',
        'account_filter': '',
        'status_filter': '',
    }
    from app.services.financial_ledgers import build_supplier_financial_ledger
    ledger = build_supplier_financial_ledger(supplier, **filters)
    filtered_rows = ledger['filtered_rows']

    if action == 'pdf':
        from app.services.supplier_ledger_pdf import (
            period_label, render_supplier_ledger_pdf,
        )
        generated_at = pk_now()
        # The first row of a range is the carried-forward balance, so
        # removing its own movement gives the balance before the range.
        if filtered_rows:
            first = filtered_rows[0]
            brought_forward = float(first.get('balance') or 0) - float(first.get('credit') or 0) + float(first.get('debit') or 0)
            closing = float(filtered_rows[-1].get('balance') or 0)
        else:
            brought_forward = closing = 0.0
        total_debit = sum(float(row.get('debit') or 0) for row in filtered_rows)
        total_credit = sum(float(row.get('credit') or 0) for row in filtered_rows)
        pdf_bytes = render_supplier_ledger_pdf(
            supplier=supplier,
            rows=filtered_rows,
            settings=Settings.query.first(),
            period=period_label(start, end) if scope == 'range' else period_label(None, None),
            balance_brought_forward=brought_forward,
            total_debit=total_debit,
            total_credit=total_credit,
            closing_balance=closing,
            generated_at=generated_at,
            statement_no=f"SL-{supplier.id}-{generated_at.strftime('%Y%m%d')}",
        )
        response = Response(pdf_bytes, mimetype='application/pdf')
        response.headers['Content-Disposition'] = f'attachment; filename={_download_filename("SUPPLIERLEDGER", "pdf")}'
        _disable_response_cache(response)
        return response

    if action != 'print':
        import csv
        from io import StringIO
        out = StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['Date', 'Type', 'Reference', 'Description', 'Debit', 'Credit', 'Balance', 'Account', 'Notes'])
        for row in filtered_rows:
            dt = row.get('date')
            writer.writerow([
                dt.strftime('%d-%m-%Y') if dt and dt != datetime.min else '',
                row.get('type', ''), row.get('reference', ''), row.get('description', ''),
                f"{row.get('debit', 0):.2f}", f"{row.get('credit', 0):.2f}",
                f"{row.get('balance', 0):.2f}", row.get('account', ''), row.get('note', ''),
            ])
        # UTF-8 BOM so Excel opens non-ASCII names correctly.
        response = Response('\ufeff' + out.getvalue(), mimetype='text/csv; charset=utf-8')
        response.headers['Content-Disposition'] = f'attachment; filename={_download_filename("SUPPLIERLEDGER", "csv")}'
        _disable_response_cache(response)
        return response
    rendered = render_template(
        'supplier_ledger_print.html',
        supplier=supplier, ledger=filtered_rows,
        final_balance=ledger['closing_balance'], total_bill=ledger['total_credit'],
        total_paid=ledger['total_debit'], generated_at=pk_now(), auto_print=True
    )
    response = make_response(rendered)
    response.headers['Content-Disposition'] = 'inline; filename=supplier-ledger.html'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/download_supplier_payment/<int:payment_id>')
@login_required
def download_supplier_payment(payment_id):
    payment = SupplierPayment.query.get_or_404(payment_id)
    supplier = db.session.get(Supplier, payment.supplier_id)
    supplier_name = supplier.name if supplier else 'Supplier'

    bill_view = SimpleNamespace(
        manual_bill_no=f"PAY-{payment.id}",
        auto_bill_no='',
        invoice_no='',
        date_posted=payment.date_posted,
        client_name=supplier_name,
        supplier=supplier_name,
        amount=payment.amount or 0,
        paid_amount=0,
        method=payment.method or '',
        bank_name=payment.bank_name or '',
        account_name=payment.account_name or '',
        account_no=payment.account_no or '',
        note=payment.note or ''
    )

    action = (request.args.get('action') or 'download').lower()
    disposition = 'inline' if action == 'print' else 'attachment'

    rendered = render_template(
        'view_bill.html',
        bill=bill_view,
        type='Payment',
        items=[],
        client=None,
        client_balance=0,
        previous_balance=0,
        recent_deliveries=[],
        material_ledger_recent=[],
        material_stock_summary=[],
        auto_print=(action == 'print')
    )
    if action == 'download':
        pdf_response = _try_render_weasy_pdf(
            rendered,
            _download_filename('SUPPLIERPAYMENT', 'pdf'),
            disposition=disposition
        )
        if pdf_response:
            return pdf_response

    response = make_response(rendered)
    response.headers['Content-Disposition'] = f'{disposition}; filename={_download_filename("SUPPLIERPAYMENT", "html")}'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/add_supplier_payment', methods=['POST'])
@login_required
def add_supplier_payment():
    """Compatibility endpoint delegating to the canonical Accounts service."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    supplier_id = request.form.get('supplier_id')
    try:
        from app.services.payments_crud import save_supplier_payment
        payment, _ = save_supplier_payment(
            supplier_id=supplier_id,
            amount=request.form.get('amount', 0),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            idempotency_key=request.form.get('idempotency_key'),
            actor=current_user,
        )
        db.session.commit()
        flash('Supplier payment recorded.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment create failed')
        flash('Unable to record supplier payment: the payment could not be saved. Please check the details and try again.', 'danger')
    return_to = (request.form.get('return_to') or '').strip().lower()
    if return_to == 'payments':
        return redirect(url_for('accounts.supplier_payments'))
    if str(supplier_id or '').isdigit():
        return redirect(url_for('supplier_ledger', id=int(supplier_id)))
    return redirect(url_for('suppliers'))


@bp.route('/edit_supplier_payment/<int:id>', methods=['POST'])
@login_required
def edit_supplier_payment(id):
    """Compatibility endpoint using exactly the same service as Create/Edit."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import save_supplier_payment
        save_supplier_payment(
            payment_id=id,
            supplier_id=request.form.get('supplier_id') or payment.supplier_id,
            amount=request.form.get('amount', payment.amount),
            method=request.form.get('method', payment.method or 'Cash'),
            payment_account_id=request.form.get('payment_account_id') or payment.payment_account_id,
            manual_bill_no=request.form.get('manual_bill_no', payment.manual_bill_no or ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', payment.note or ''),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        db.session.commit()
        flash('Supplier payment updated. All balances were recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment edit failed')
        flash('Unable to update supplier payment: the payment could not be updated. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))


@bp.route('/delete_supplier_payment/<int:id>', methods=['POST'])
@login_required
def delete_supplier_payment(id):
    """Compatibility soft-delete; never hard-deletes accounting history."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_supplier_payment as do_delete
        if do_delete(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment deleted and accounting effects reversed.', 'success')
        else:
            flash('Supplier payment is already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment delete failed')
        flash('Unable to delete supplier payment: the payment could not be deleted. Please try again.', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))


@bp.route('/restore_supplier_payment/<int:id>', methods=['POST'])
@login_required
def restore_supplier_payment(id):
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_supplier_payment as do_restore
        if do_restore(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment restored and balances re-applied.', 'success')
        else:
            flash('Supplier payment is already active.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment restore failed')
        flash('Unable to restore supplier payment: the payment could not be restored. Please try again.', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))
