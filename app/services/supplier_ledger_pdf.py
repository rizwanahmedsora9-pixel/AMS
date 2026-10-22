"""Professional A4 PDF statement for a supplier ledger.

The statement is laid out directly with ReportLab rather than converted from
HTML.  That keeps the output identical on every machine: the HTML-to-PDF path
depends on WeasyPrint's native libraries, and its ReportLab fallback flattens
header blocks, summary boxes and right-aligned figures into loose text.

Every figure is passed in by the caller (already computed by the ledger
projection), so this module formats numbers but never recomputes them.
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

PAGE_SIZE_MM = (210, 297)   # A4 portrait
MARGIN_MM = 14
DEFAULT_COMPANY = 'FAZAL BUILDING MATERIALS'

INK = '#111827'
MUTED = '#6b7280'
RULE = '#9ca3af'
HEAD_BG = '#1f2937'
ZEBRA_BG = '#f3f4f6'
TOTAL_BG = '#e5e7eb'


def _fonts():
    """Return (regular, bold, italic) font names, preferring a Unicode TTF."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    bold = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    oblique = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf'
    if all(os.path.exists(p) for p in (regular, bold, oblique)):
        try:
            pdfmetrics.registerFont(TTFont('AMSSans', regular))
            pdfmetrics.registerFont(TTFont('AMSSans-Bold', bold))
            pdfmetrics.registerFont(TTFont('AMSSans-Oblique', oblique))
            return 'AMSSans', 'AMSSans-Bold', 'AMSSans-Oblique'
        except Exception:
            pass
    return 'Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique'


def _money(value):
    return f'{float(value or 0):,.2f}'


def _money_or_dash(value):
    value = float(value or 0)
    return f'{value:,.2f}' if abs(value) >= 0.005 else '-'


def _qty_text(value):
    """Quantity / rate text for the GRN line columns.

    Trailing zeros are trimmed (10 rather than 10.00) so wide quantities stay
    inside their column, while a fractional quantity keeps up to two decimals.
    """
    text = f'{float(value or 0):,.2f}'.rstrip('0').rstrip('.')
    return text or '0'


def _date_text(value):
    if not value or not isinstance(value, datetime) or value.year <= 1:
        return '-'
    return value.strftime('%d-%b-%Y')


def _fmt_day(value):
    return value.strftime('%d %b %Y') if value else ''


def period_label(start, end):
    """Human label for the statement period (``None`` means open-ended)."""
    if not start and not end:
        return 'All transactions (full ledger)'
    left = _fmt_day(start) if start else 'Beginning'
    right = _fmt_day(end) if end else 'Latest'
    return f'{left} to {right}'


def render_supplier_ledger_pdf(*, supplier, rows, settings=None,
                               period, balance_brought_forward=0.0,
                               total_debit=0.0, total_credit=0.0,
                               closing_balance=0.0, generated_at=None,
                               statement_no=''):
    """Return the statement as PDF bytes.

    ``rows`` are the ledger rows already filtered for the period, each with
    ``date``, ``type``, ``reference``, ``description``, ``debit``, ``credit``,
    ``balance``, ``account``, ``note`` and optional ``item_lines``.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import (
        BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph,
        Spacer, Table, TableStyle,
    )

    body, bold, italic = _fonts()
    generated_at = generated_at or datetime.now()
    company = (getattr(settings, 'company_name', None) or DEFAULT_COMPANY) if settings else DEFAULT_COMPANY
    company_address = (getattr(settings, 'company_address', None) or '') if settings else ''
    company_phone = (getattr(settings, 'company_phone', None) or '') if settings else ''

    def st(name, font=body, size=8, leading=None, align=TA_LEFT, color=INK):
        return ParagraphStyle(
            name, fontName=font, fontSize=size, leading=leading or size * 1.3,
            alignment=align, textColor=colors.HexColor(color),
        )

    s_company = st('co', bold, 14, 17)
    s_small = st('small', body, 7.6, 9.6, color=MUTED)
    s_title = st('title', bold, 12, 15, TA_CENTER)
    s_label = st('label', bold, 6.8, 8.4, color=MUTED)
    s_value = st('value', bold, 9.5, 12)
    s_text = st('text', body, 8, 10)
    s_section = st('section', bold, 8.5, 11)
    s_td = st('td', body, 7.2, 9.0)
    s_td_r = st('tdr', body, 7.2, 9.0, TA_RIGHT)
    s_td_c = st('tdc', body, 7.2, 9.0, TA_CENTER)
    s_detail = st('detail', body, 6.6, 8.2, color='#374151')
    s_note = st('note', italic, 6.5, 8.2, color=MUTED)
    s_tot = st('tot', bold, 7.0, 9.2)
    s_tot_r = st('totr', bold, 7.0, 9.2, TA_RIGHT)
    s_sig = st('sig', body, 7.4, 9.2, TA_CENTER, color=MUTED)

    page_w, page_h = A4
    margin = MARGIN_MM * mm
    usable_w = page_w - 2 * margin

    # ---- header -------------------------------------------------------
    left = [Paragraph(escape(company.upper()), s_company)]
    if company_address:
        left.append(Paragraph(escape(company_address), s_small))
    if company_phone:
        left.append(Paragraph(escape(company_phone), s_small))

    meta_rows = [
        [Paragraph('STATEMENT NO.', s_label), Paragraph(escape(statement_no or '-'), s_text)],
        [Paragraph('GENERATED', s_label), Paragraph(escape(generated_at.strftime('%d-%b-%Y %I:%M %p')), s_text)],
        [Paragraph('PERIOD', s_label), Paragraph(escape(period), s_text)],
    ]
    meta = Table(meta_rows, colWidths=[27 * mm, 58 * mm])
    meta.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 1.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5),
    ]))
    title_box = Table(
        [[Paragraph('SUPPLIER LEDGER', s_title)]],
        colWidths=[usable_w * 0.42], rowHeights=[9 * mm],
    )
    title_box.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1.2, colors.HexColor(INK)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    right_col = Table([[title_box], [Spacer(1, 2 * mm)], [meta]], colWidths=[usable_w * 0.42])
    right_col.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    header = Table([[left, right_col]], colWidths=[usable_w * 0.58, usable_w * 0.42])
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, colors.HexColor(INK)),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))

    # ---- supplier + summary ------------------------------------------
    party = [Paragraph('SUPPLIER', s_label), Spacer(1, 1.5 * mm),
             Paragraph(escape((supplier.name or '-').upper()), s_value)]
    if getattr(supplier, 'address', None):
        party.append(Paragraph(escape(supplier.address), s_text))
    if getattr(supplier, 'phone', None):
        party.append(Paragraph(f'Phone: {escape(supplier.phone)}', s_text))

    closing = float(closing_balance or 0)
    if closing > 0.005:
        state_text = 'Payable to supplier'
    elif closing < -0.005:
        state_text = 'Advance paid to supplier'
    else:
        state_text = 'Settled'
    summary_rows = [
        ['Balance brought forward', _money(balance_brought_forward)],
        ['Total bills (credit)', _money(total_credit)],
        ['Total payments (debit)', _money(total_debit)],
        ['Closing balance', _money(closing)],
    ]
    summary = Table(
        [[Paragraph(escape(k), s_td), Paragraph(escape(v), s_td_r)] for k, v in summary_rows],
        colWidths=[47 * mm, 33 * mm],
    )
    summary.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor(INK)),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, colors.HexColor(RULE)),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor(TOTAL_BG)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    summary_block = [summary, Spacer(1, 1.5 * mm), Paragraph(escape(state_text), s_small)]
    party_block = Table([[party, summary_block]], colWidths=[usable_w * 0.5, usable_w * 0.5])
    party_block.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (0, -1), 10),
        ('RIGHTPADDING', (1, 0), (1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))

    # ---- ledger table -------------------------------------------------
    # Quantity, Rate and Amount are first-class columns so a GRN shows what
    # was actually received, not only the value of the bill.  Every GRN item
    # line becomes its own physical row inside the transaction: the movement
    # itself (date, reference, description, account, note, debit, credit and
    # running balance) sits on the first line of the group, and the remaining
    # lines carry the material, quantity, rate and amount.
    from reportlab.pdfbase import pdfmetrics

    # Column plan: minimum width in mm, then the base font size.  The ten
    # columns always add up to the usable page width; the two text columns that
    # must never split a value - reference and particulars - share whatever the
    # numeric columns leave, so a long bill number stays on one line instead of
    # being broken in half by the wrapper.
    col_plan_base = [
        ('date', 'DATE', 17, TA_LEFT, 6.6, False),
        ('ref', 'REFERENCE', 18, TA_LEFT, 6.8, False),
        ('particulars', 'PARTICULARS', 26, TA_LEFT, 7.2, False),
        ('account', 'ACCOUNT', 12, TA_LEFT, 6.6, False),
        ('qty', 'QTY', 10, TA_RIGHT, 6.9, True),
        ('rate', 'RATE', 12, TA_RIGHT, 6.9, True),
        ('amount', 'AMOUNT', 17, TA_RIGHT, 6.9, True),
        ('debit', 'DEBIT', 16, TA_RIGHT, 6.9, True),
        ('credit', 'CREDIT', 16, TA_RIGHT, 6.9, True),
        ('balance', 'BALANCE', 17, TA_RIGHT, 6.9, True),
    ]
    cell_pad = 1.6 * mm            # each side of a ledger cell
    FLOOR_NUM, FLOOR_TEXT = 5.4, 5.6
    usable_mm = usable_w / mm

    prepared = []                  # one entry per ledger movement
    for row in rows:
        ref = row.get('reference') or row.get('bill_no') or ''
        description = row.get('description') or row.get('type') or '-'
        row_type = row.get('type') or ''
        items = [
            item for item in (row.get('item_lines') or [])
            if any(item.get(key) not in (None, '', 0) for key in ('name', 'qty', 'rate', 'amount'))
        ]
        prepared.append({
            'date': _date_text(row.get('date')),
            'ref': escape(ref or '-'),
            'invoice': escape(row['supplier_invoice_no']) if row.get('supplier_invoice_no') else '',
            'description': escape(description),
            'type': row_type if row_type and row_type not in description and row_type != ref else '',
            'note': escape(row['note']) if row.get('note') else '',
            'account': escape(row.get('account') or '-'),
            'items': [(
                escape(item.get('name') or 'Item'),
                _qty_text(item.get('qty')),
                _qty_text(item.get('rate')),
                _qty_text(item.get('amount')),
            ) for item in items],
            'debit': _money_or_dash(row.get('debit')),
            'credit': _money_or_dash(row.get('credit')),
            'balance': _money(row.get('balance')),
        })

    totals_debit = _money(total_debit)
    totals_credit = _money(total_credit)
    totals_closing = _money(closing)

    # Resolve the column widths from the actual statement content.  Every
    # column is at least wide enough for its own heading, and the two columns
    # that must never split a value - reference and particulars - take what the
    # fixed columns leave, so a long bill number prints on one line instead of
    # being broken in half by the wrapper.
    labels = {col[0]: col[1] for col in col_plan_base}
    base_w = {col[0]: col[2] for col in col_plan_base}

    def _needed_mm(text, size):
        return pdfmetrics.stringWidth(text, body, size) / mm + 2 * cell_pad / mm

    def _longest(tokens, size):
        return max((_needed_mm(token, size) for token in tokens), default=0.0)

    widths = {key: max(width, _needed_mm(labels[key], 6.4)) for key, width in base_w.items()}
    if prepared:
        widths['date'] = max(widths['date'], _longest([e['date'] for e in prepared], 6.6))
        widths['account'] = max(
            widths['account'],
            _longest([word for e in prepared for word in e['account'].split()], 6.6),
        )
        widths['ref'] = max(widths['ref'], _longest([e['ref'] for e in prepared], 6.8))

    min_particulars = 26.0
    fixed_mm = sum(width for key, width in widths.items() if key not in ('ref', 'particulars'))
    ref_w = min(widths['ref'], max(18.0, usable_mm - fixed_mm - min_particulars))
    particulars_w = max(min_particulars, usable_mm - fixed_mm - ref_w)
    widths.update({'ref': ref_w, 'particulars': particulars_w})

    col_plan = [(key, labels[key], widths[key], align, base, is_num)
                for key, _label, _width, align, base, is_num in col_plan_base]
    col_keys = [col[0] for col in col_plan]
    col_w_mm = {col[0]: col[2] for col in col_plan}
    col_w = [col[2] * mm for col in col_plan]

    # Text that is allowed to wrap is fitted on its longest word; figures and
    # references are fitted on the whole value so they stay on one line.
    def _tokens(key):
        if key == 'date':
            return [entry['date'] for entry in prepared] or ['01-Jan-2026']
        if key == 'ref':
            return [entry['ref'] for entry in prepared] or ['REFERENCE']
        if key == 'particulars':
            words = []
            for entry in prepared:
                words.extend(entry['description'].split())
                words.extend(entry['type'].split())
                for item in entry['items']:
                    words.extend(item[0].split())
            return words or ['PARTICULARS']
        if key == 'account':
            words = []
            for entry in prepared:
                words.extend(entry['account'].split())
            return words or ['ACCOUNT']
        index = {'qty': 1, 'rate': 2, 'amount': 3}.get(key)
        if index:
            return [item[index] for entry in prepared for item in entry['items']] or ['0.00']
        return [entry[key] for entry in prepared] or ['0.00']

    def _fit(tokens, font, base, avail, floor):
        widest = max((pdfmetrics.stringWidth(token, font, base) for token in tokens), default=0.0)
        if widest <= avail or widest <= 0:
            return base
        return max(floor, base * avail / widest)

    def _style(name, font, size, align, color=INK):
        return ParagraphStyle(
            name, fontName=font, fontSize=size, leading=size * 1.3,
            alignment=align, textColor=colors.HexColor(color),
        )

    cell_style, head_style = {}, {}
    for key, label, width, align, base, is_num in col_plan:
        avail = width * mm - 2 * cell_pad
        floor = FLOOR_NUM if is_num else FLOOR_TEXT
        size = _fit(_tokens(key), body, base, avail, floor)
        cell_style[key] = _style(f'c_{key}', body, size, align)
        head_size = _fit([label], bold, 6.4, avail, FLOOR_TEXT)
        head_style[key] = _style(f'h_{key}', bold, head_size, align, '#ffffff')

    # Description / note lines live in the particulars column.  Fitting them to
    # it keeps ordinary notes on a single line; only genuinely long text wraps.
    particulars_avail = col_w_mm['particulars'] * mm - 2 * cell_pad

    def _fit_text_style(style, texts, floor):
        longest = max((pdfmetrics.stringWidth(text, body, style.fontSize) for text in texts), default=0.0)
        if longest > particulars_avail and longest > 0:
            size = max(floor, style.fontSize * particulars_avail / longest)
            style.fontSize, style.leading = size, size * 1.28

    _fit_text_style(s_detail, [entry['description'] for entry in prepared], 5.6)
    _fit_text_style(s_note, [f"Note: {entry['note']}" for entry in prepared if entry['note']], 5.9)
    _fit_text_style(s_detail, [entry['type'] for entry in prepared if entry['type']], 5.6)

    # Totals row: bold, and fitted to the same numeric columns.
    totals_values = {'debit': totals_debit, 'credit': totals_credit, 'balance': totals_closing}
    tot_size = 7.0
    for key, value in totals_values.items():
        tot_size = min(tot_size, _fit([value], bold, 7.0,
                                      col_w_mm[key] * mm - 2 * cell_pad, FLOOR_NUM))
    s_tot.fontSize, s_tot.leading = tot_size + 0.1, (tot_size + 0.1) * 1.35
    s_tot_r.fontSize, s_tot_r.leading = tot_size, tot_size * 1.35

    head_row = [Paragraph(label, head_style[key]) for key, label, *_ in col_plan]

    def _particulars(entry, item_name=None, first=True):
        """Particulars cell.

        A GRN lists one material per line, so the movement's own description,
        type and note are printed once - under the first material - instead of
        being repeated against every item of the same bill.
        """
        if item_name:
            lines = [Paragraph(item_name, cell_style['particulars'])]
        else:
            lines = [Paragraph(entry['description'], cell_style['particulars'])]
        if not first:
            return lines
        if item_name:
            lines.append(Paragraph(entry['description'], s_detail))
        if entry['invoice']:
            lines.append(Paragraph(f"Inv: {entry['invoice']}", s_detail))
        if entry['type']:
            lines.append(Paragraph(entry['type'], s_detail))
        if entry['note']:
            lines.append(Paragraph(f"Note: {entry['note']}", s_note))
        return lines

    groups = []                    # (rows of one movement, zebra parity)
    for movement_index, entry in enumerate(prepared):
        ref_cell = [Paragraph(entry['ref'], cell_style['ref'])]

        group = []
        if entry['items']:
            for index, (name, qty, rate, amount) in enumerate(entry['items']):
                first = index == 0
                group.append([
                    Paragraph(entry['date'], cell_style['date']) if first else '',
                    ref_cell if first else '',
                    _particulars(entry, name, first=first),
                    Paragraph(entry['account'], cell_style['account']) if first else '',
                    Paragraph(qty, cell_style['qty']),
                    Paragraph(rate, cell_style['rate']),
                    Paragraph(amount, cell_style['amount']),
                    Paragraph(entry['debit'], cell_style['debit']) if first else '',
                    Paragraph(entry['credit'], cell_style['credit']) if first else '',
                    Paragraph(entry['balance'], cell_style['balance']) if first else '',
                ])
        else:
            group.append([
                Paragraph(entry['date'], cell_style['date']),
                ref_cell,
                _particulars(entry),
                Paragraph(entry['account'], cell_style['account']),
                Paragraph('-', s_td_c), Paragraph('-', s_td_c), Paragraph('-', s_td_c),
                Paragraph(entry['debit'], cell_style['debit']),
                Paragraph(entry['credit'], cell_style['credit']),
                Paragraph(entry['balance'], cell_style['balance']),
            ])
        groups.append((group, movement_index % 2 == 1))

    base_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(HEAD_BG)),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('VALIGN', (0, 1), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, colors.HexColor('#d1d5db')),
        ('LEFTPADDING', (0, 0), (-1, -1), cell_pad), ('RIGHTPADDING', (0, 0), (-1, -1), cell_pad),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, 0), 3.5), ('BOTTOMPADDING', (0, 0), (-1, 0), 3.5),
    ]

    def _make_table(groups_slice):
        """Ledger table whose header row repeats on every page it spans."""
        data, zebra = [head_row], []
        for group_rows, parity in groups_slice:
            for cells in group_rows:
                data.append(cells)
                if parity:
                    zebra.append(len(data) - 1)
        table = Table(data, colWidths=col_w, repeatRows=1)
        style = list(base_style)
        for idx in zebra:
            style.append(('BACKGROUND', (0, idx), (-1, idx), colors.HexColor(ZEBRA_BG)))
        table.setStyle(TableStyle(style))
        return table

    # Totals: label spanning the first seven columns, then debit | credit | balance.
    totals = Table(
        [[Paragraph('Totals for period (closing balance)', s_tot),
          Paragraph(totals_debit, s_tot_r),
          Paragraph(totals_credit, s_tot_r),
          Paragraph(totals_closing, s_tot_r)]],
        colWidths=[sum(col_w[:7]), col_w[7], col_w[8], col_w[9]],
    )
    totals.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(TOTAL_BG)),
        ('LINEABOVE', (0, 0), (-1, 0), 1, colors.HexColor(INK)),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor(INK)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), cell_pad), ('RIGHTPADDING', (0, 0), (-1, -1), cell_pad),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    note_line = Paragraph('Qty, Rate and Amount show the GRN line detail; '
                          'Debit, Credit and Balance show the ledger movement.', s_small)

    ledger_section = [Paragraph('TRANSACTIONS', s_section), Spacer(1, 1.5 * mm)]
    if not groups:
        empty_table = Table(
            [head_row,
             [Paragraph('No transactions in this period.', s_td_c)] + [''] * (len(col_keys) - 1)],
            colWidths=col_w, repeatRows=1,
        )
        empty_table.setStyle(TableStyle(list(base_style) + [
            ('SPAN', (0, 1), (-1, 1)),
            ('ALIGN', (0, 1), (-1, 1), 'CENTER'),
        ]))
        ledger_section.extend([empty_table, totals, Spacer(1, 1.5 * mm), note_line])
    else:
        # A transaction is never split by a page break: the physical rows of
        # one GRN (its material lines) always stay together, and a continuation
        # page repeats the column header.
        def _group_height(group_slice):
            return _make_table([group_slice]).wrap(usable_w, page_h)[1]

        head_h = _make_table([]).wrap(usable_w, page_h)[1]
        frame_h = page_h - 2 * margin - 6 * mm
        header_h = header.wrap(usable_w, page_h)[1] + 4 * mm
        party_h = party_block.wrap(usable_w, page_h)[1]
        heading_h = ledger_section[0].wrap(usable_w, page_h)[1] + 1.5 * mm
        tail_h = totals.wrap(usable_w, page_h)[1] + note_line.wrap(usable_w, page_h)[1] + 3 * mm
        first_budget = frame_h - header_h - party_h - heading_h - tail_h
        next_budget = frame_h - tail_h

        def _rows_height(group_rows, parity):
            return _make_table([(group_rows, parity)]).wrap(usable_w, page_h)[1] - head_h

        max_group_h = next_budget - head_h
        # A bill with more material lines than a whole page cannot stay in one
        # piece; only then is the transaction allowed to continue on the next
        # page.
        pieces = []
        for group_rows, parity in groups:
            if _rows_height(group_rows, parity) <= max_group_h:
                pieces.append((group_rows, parity))
                continue
            piece = []
            for cells in group_rows:
                if piece and _rows_height(piece + [cells], parity) > max_group_h:
                    pieces.append((piece, parity))
                    piece = []
                piece.append(cells)
            if piece:
                pieces.append((piece, parity))

        chunks, current, current_h, budget = [], [], heading_h, first_budget
        for piece in pieces:
            piece_h = _rows_height(*piece)
            if current and current_h + piece_h > budget:
                chunks.append(current)
                current, current_h, budget = [], head_h, next_budget
            current.append(piece)
            current_h += piece_h
        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks):
            table = _make_table(chunk)
            if index == len(chunks) - 1:
                # The period totals stay with the final transactions.
                ledger_section.append(
                    KeepTogether([table, totals, Spacer(1, 1.5 * mm), note_line]))
            else:
                ledger_section.append(KeepTogether(table))

    sig = Table(
        [[Paragraph('______________________<br/>Prepared by', s_sig),
          Paragraph('______________________<br/>Authorised sign', s_sig),
          Paragraph('______________________<br/>Company stamp', s_sig)]],
        colWidths=[usable_w / 3.0] * 3,
    )
    sig.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    footer_block = KeepTogether([
        Spacer(1, 12 * mm), sig, Spacer(1, 4 * mm),
        Paragraph('All supplier movements for the selected period are listed above. '
                  'Balances are shown in rupees (Rs.).', s_small),
    ])

    # ---- document -----------------------------------------------------
    buf = io.BytesIO()
    title = f"Supplier Ledger - {supplier.name or ''}".strip()

    class _NumberedCanvas(pdf_canvas.Canvas):
        """Two-pass canvas so each page can print 'Page x of y'."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self._draw_footer(total)
                super().showPage()
            super().save()

        def _draw_footer(self, total):
            self.saveState()
            self.setStrokeColor(colors.HexColor(RULE))
            self.setLineWidth(0.4)
            self.line(margin, margin - 4 * mm, page_w - margin, margin - 4 * mm)
            self.setFont(body, 6.8)
            self.setFillColor(colors.HexColor(MUTED))
            self.drawString(margin, margin - 8 * mm,
                            f'Supplier ledger - {supplier.name or ""} - generated {generated_at.strftime("%d-%b-%Y %I:%M %p")}')
            self.drawRightString(page_w - margin, margin - 8 * mm,
                                 f'Page {self._pageNumber} of {total}')
            self.restoreState()

    doc = BaseDocTemplate(
        buf, pagesize=A4, title=title, author=company,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin + 6 * mm,
    )
    frame = Frame(margin, margin + 6 * mm, usable_w, page_h - 2 * margin - 6 * mm,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id='main')
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])

    story = [header, Spacer(1, 4 * mm), party_block]
    story.extend(ledger_section)
    story.append(footer_block)
    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
