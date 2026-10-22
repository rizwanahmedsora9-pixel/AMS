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
    s_th = st('th', bold, 7.2, 8.8, color='#ffffff')
    s_th_r = st('thr', bold, 7.2, 8.8, TA_RIGHT, color='#ffffff')
    s_td = st('td', body, 7.4, 9.2)
    s_td_r = st('tdr', body, 7.4, 9.2, TA_RIGHT)
    s_td_c = st('tdc', body, 7.4, 9.2, TA_CENTER)
    s_detail = st('detail', body, 6.9, 8.4, color='#374151')
    s_note = st('note', italic, 6.8, 8.4, color=MUTED)
    s_tot = st('tot', bold, 7.6, 9.4)
    s_tot_r = st('totr', bold, 7.6, 9.4, TA_RIGHT)
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
    col_w = [19, 25, 64, 22, 17, 17, 18]          # sums to 182 mm == usable width
    col_w = [w * mm for w in col_w]
    head = [
        Paragraph('DATE', s_th), Paragraph('REFERENCE', s_th), Paragraph('PARTICULARS', s_th),
        Paragraph('ACCOUNT', s_th), Paragraph('DEBIT', s_th_r), Paragraph('CREDIT', s_th_r),
        Paragraph('BALANCE', s_th_r),
    ]
    data = [head]
    for row in rows:
        ref = row.get('reference') or row.get('bill_no') or ''
        ref_cell = [Paragraph(escape(ref or '-'), s_td)]
        if row.get('supplier_invoice_no'):
            ref_cell.append(Paragraph(f"Inv: {escape(row['supplier_invoice_no'])}", s_detail))

        desc = [Paragraph(escape(row.get('description') or row.get('type') or '-'), s_td)]
        if row.get('type') and row['type'] not in (row.get('description') or '') and row['type'] != ref:
            desc.append(Paragraph(escape(row['type']), s_detail))
        for item in row.get('item_lines') or []:
            desc.append(Paragraph(
                f"{escape(item.get('name') or 'Item')}: "
                f"{float(item.get('qty') or 0):,.2f} x {float(item.get('rate') or 0):,.2f}"
                f" = {float(item.get('amount') or 0):,.2f}",
                s_detail,
            ))
        if row.get('note'):
            desc.append(Paragraph(f"Note: {escape(row['note'])}", s_note))

        data.append([
            Paragraph(_date_text(row.get('date')), s_td),
            ref_cell,
            desc,
            Paragraph(escape(row.get('account') or '-'), s_detail),
            Paragraph(_money_or_dash(row.get('debit')), s_td_r),
            Paragraph(_money_or_dash(row.get('credit')), s_td_r),
            Paragraph(_money(row.get('balance')), s_td_r),
        ])
    if len(data) == 1:
        data.append([Paragraph('No transactions in this period.', s_td_c), '', '', '', '', '', ''])

    ledger_table = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(HEAD_BG)),
        ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
        ('VALIGN', (0, 1), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, colors.HexColor('#d1d5db')),
        ('LEFTPADDING', (0, 0), (-1, -1), 3.5), ('RIGHTPADDING', (0, 0), (-1, -1), 3.5),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, 0), 3.5), ('BOTTOMPADDING', (0, 0), (-1, 0), 3.5),
    ]
    for idx in range(1, len(data)):
        if idx % 2 == 0:
            style.append(('BACKGROUND', (0, idx), (-1, idx), colors.HexColor(ZEBRA_BG)))
    ledger_table.setStyle(TableStyle(style))

    # Four cells aligned under: particulars-spanning label | debit | credit | balance.
    totals = Table(
        [[Paragraph('Totals for period (closing balance)', s_tot),
          Paragraph(_money(total_debit), s_tot_r),
          Paragraph(_money(total_credit), s_tot_r),
          Paragraph(_money(closing), s_tot_r)]],
        colWidths=[sum(col_w[:4]), col_w[4], col_w[5], col_w[6]],
    )
    totals.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(TOTAL_BG)),
        ('LINEABOVE', (0, 0), (-1, 0), 1, colors.HexColor(INK)),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor(INK)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3.5), ('RIGHTPADDING', (0, 0), (-1, -1), 3.5),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    ledger_section = [
        Paragraph('TRANSACTIONS', s_section), Spacer(1, 1.5 * mm),
        ledger_table, totals,
    ]

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
