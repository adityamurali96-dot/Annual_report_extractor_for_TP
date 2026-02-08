"""
Regex/pattern-based extraction logic.
Used as a fallback and for validation alongside Claude API extraction.
"""

import re
import fitz

from app.pdf_utils import parse_number, is_note_ref, is_value_line


# -------------------------------------------------------------------
# Stage 1: Find standalone financial statement pages
# -------------------------------------------------------------------

def find_standalone_pages(pdf_path: str) -> tuple[dict, int]:
    """Identify pages containing standalone financial statements."""
    doc = fitz.open(pdf_path)
    pages = {}
    for i in range(doc.page_count):
        text = doc[i].get_text()
        lower = text.lower()
        # P&L
        if 'statement of profit and loss' in lower and 'standalone' in lower:
            if 'pnl' not in pages:
                pages['pnl'] = i
        # Balance Sheet
        if 'balance sheet' in lower and 'standalone' in lower:
            if 'bs' not in pages:
                pages['bs'] = i
        # Cash Flow
        if 'cash flow' in lower and 'standalone' in lower:
            if 'cf' not in pages:
                pages['cf'] = i
    total = doc.page_count
    doc.close()
    return pages, total


# -------------------------------------------------------------------
# Stage 2A: P&L Extraction (regex-based)
# -------------------------------------------------------------------

PNL_TARGETS = [
    ('Revenue from operations', ['Revenue from operations']),
    ('Other income', ['Other income']),
    ('Total income', ['Total income']),
    ('Employee benefits expense', ['Employee benefits expense']),
    ('Cost of professionals', ['Cost of professionals']),
    ('Finance costs', ['Finance costs']),
    ('Depreciation and amortisation', ['Depreciation and amortisation', 'Depreciation and amortization']),
    ('Other expenses', ['Other expenses']),
    ('Total expenses', ['Total expenses']),
    ('Profit before tax', ['Profit before exceptional', 'Profit before tax']),
    ('Current tax', ['Current tax']),
    ('Deferred tax', ['Deferred tax']),
    ('Total tax expense', ['Total tax expense']),
    ('Profit for the year', ['Profit for the year', 'Profit for the period']),
    ('Total comprehensive income', ['Total comprehensive income']),
    ('Basic EPS', ['Basic (In', 'Basic (in', 'Basic earning']),
    ('Diluted EPS', ['Diluted (In', 'Diluted (in', 'Diluted earning']),
]


def extract_pnl_regex(pdf_path: str, page_idx: int) -> dict:
    """Extract P&L data using regex/pattern matching."""
    doc = fitz.open(pdf_path)
    text = doc[page_idx].get_text()
    # Also try the next page in case P&L spans two pages
    next_text = ""
    if page_idx + 1 < doc.page_count:
        next_text = doc[page_idx + 1].get_text()
    doc.close()

    lines = [l.strip() for l in text.split('\n')]
    if next_text:
        lines.extend([l.strip() for l in next_text.split('\n')])

    extracted = {}
    note_refs = {}

    for item_name, patterns in PNL_TARGETS:
        for i, line in enumerate(lines):
            if not any(p.lower() in line.lower() for p in patterns):
                continue
            vals = []
            note_ref = None
            for j in range(i + 1, min(i + 8, len(lines))):
                candidate = lines[j]
                if is_note_ref(candidate) and note_ref is None:
                    note_ref = candidate
                    continue
                if is_value_line(candidate):
                    vals.append(parse_number(candidate))
                    if len(vals) == 2:
                        break
                elif vals:
                    break

            if len(vals) >= 2:
                extracted[item_name] = {'current': vals[0], 'previous': vals[1]}
            elif len(vals) == 1:
                extracted[item_name] = {'current': vals[0], 'previous': 0.0}
            if note_ref:
                note_refs[item_name] = note_ref
            break

    # Detect company name
    company = 'Unknown Company'
    for l in lines[:10]:
        if 'Limited' in l or 'Ltd' in l:
            company = l.split('—')[0].split('–')[0].strip()
            break

    return {
        'company': company,
        'currency': 'INR Million',
        'items': extracted,
        'note_refs': note_refs,
    }


# -------------------------------------------------------------------
# Stage 2B: Note Finder & Extractor
# -------------------------------------------------------------------

def find_note_page(pdf_path: str, note_number: str, search_start_page: int,
                   search_keyword: str = "Other expenses") -> tuple[int | None, int | None]:
    """
    Find the PDF page containing a specific note number.

    Uses multiple search strategies in order of specificity:
      1. Note number + keyword on the same line (e.g., "27. Other expenses")
      2. Note number at start of line with keyword within nearby lines (±4)
      3. Broader search for the note number heading on any notes page
    """
    doc = fitz.open(pdf_path)
    keyword_lower = search_keyword.lower()
    note_esc = re.escape(note_number)

    # ------- Strategy 1: note number + keyword on the SAME line -------
    # Handles: "27. Other expenses", "27 - Other expenses",
    #          "27) Other expenses", "Note 27: Other expenses"
    same_line_patterns = [
        re.compile(rf'^\s*{note_esc}\s*[.\-–—:)]\s*.*' + keyword_lower, re.IGNORECASE),
        re.compile(rf'^\s*{note_esc}\s+.*' + keyword_lower, re.IGNORECASE),
        re.compile(rf'(?:note\s+){note_esc}\s*[.\-–—:)]\s*.*' + keyword_lower, re.IGNORECASE),
        re.compile(keyword_lower + rf'.*\b{note_esc}\b', re.IGNORECASE),
    ]

    for i in range(search_start_page, doc.page_count):
        text = doc[i].get_text()
        lines = [l.strip() for l in text.split('\n')]
        for j, line in enumerate(lines):
            for pat in same_line_patterns:
                if pat.search(line):
                    doc.close()
                    return i, j

    # ------- Strategy 2: note number at line start, keyword nearby (±4 lines) -------
    note_start_pattern = re.compile(
        rf'^\s*{note_esc}\s*[.\-–—:)]\s', re.IGNORECASE
    )

    for i in range(search_start_page, doc.page_count):
        text = doc[i].get_text()
        lines = [l.strip() for l in text.split('\n')]
        for j, line in enumerate(lines):
            if note_start_pattern.search(line):
                ctx_start = max(0, j - 2)
                ctx_end = min(len(lines), j + 6)
                context = ' '.join(lines[ctx_start:ctx_end]).lower()
                if keyword_lower in context or 'expense' in context:
                    doc.close()
                    return i, j

    # ------- Strategy 3: page-level search (note number + keyword anywhere) -------
    for i in range(search_start_page, doc.page_count):
        text = doc[i].get_text()
        lower_text = text.lower()
        if keyword_lower not in lower_text:
            continue
        # Look for note heading pattern anywhere on the page
        heading_match = re.search(
            rf'(?:^|\n)\s*(?:note\s*)?{note_esc}\s*[.\-–—:)]\s',
            text, re.IGNORECASE | re.MULTILINE,
        )
        if heading_match:
            lines = [l.strip() for l in text.split('\n')]
            for j, line in enumerate(lines):
                if re.search(rf'(?:note\s*)?{note_esc}\s*[.\-–—:)]', line, re.IGNORECASE):
                    doc.close()
                    return i, j

    doc.close()
    return None, None


def extract_note_breakup(pdf_path: str, page_idx: int, start_line: int,
                         note_number: str) -> tuple[list[dict], dict | None]:
    """Extract line items from a note breakup table."""
    doc = fitz.open(pdf_path)
    text = doc[page_idx].get_text()
    doc.close()
    lines = [l.strip() for l in text.split('\n')]

    data_start = start_line + 1
    while data_start < len(lines):
        l = lines[data_start].lower()
        if 'for the year' in l or 'march' in l or 'in ₹' in l or 'in rs' in l or l == '':
            data_start += 1
        else:
            break

    note_items = []
    current_label = None
    parent_label = None
    i = data_start
    pending_values = []

    while i < len(lines):
        line = lines[i]

        if line and line[0].isdigit() and '.' in line[:4] and any(c.isalpha() for c in line[5:]):
            match = re.match(r'(\d+)\.', line)
            if match and match.group(1) != str(note_number):
                break

        if line.startswith('*') and len(line) > 5 and any(c.isalpha() for c in line):
            break

        if is_value_line(line) or line == '-':
            val = parse_number(line) if line != '-' else 0.0
            if current_label is not None:
                existing = next((x for x in note_items if x['label'] == current_label), None)
                if existing and existing.get('previous') is None:
                    existing['previous'] = val
                    current_label = None
                elif existing is None:
                    note_items.append({'label': current_label, 'current': val, 'previous': None})
            else:
                pending_values.append(val)
        else:
            if line:
                pending_values = []
                if line.startswith('- '):
                    current_label = f"{parent_label} - {line[2:]}" if parent_label else line[2:]
                else:
                    current_label = line
                    parent_label = line
        i += 1

    result = []
    for item in note_items:
        if item.get('previous') is None:
            item['previous'] = 0.0
        result.append(item)

    total_item = None
    if len(pending_values) >= 2:
        total_item = {'label': 'Total', 'current': pending_values[0], 'previous': pending_values[1]}
    elif result:
        total_item = result[-1]

    return result, total_item


# -------------------------------------------------------------------
# Stage 3: Compute Metrics
# -------------------------------------------------------------------

def compute_metrics(pnl: dict) -> dict:
    """Calculate financial metrics from P&L data."""
    items = pnl['items']
    metrics = {}
    for period in ['current', 'previous']:
        rev = items.get('Revenue from operations', {}).get(period, 0) or 0
        oi = items.get('Other income', {}).get(period, 0) or 0
        emp = items.get('Employee benefits expense', {}).get(period, 0) or 0
        cop = items.get('Cost of professionals', {}).get(period, 0) or 0
        dep = items.get('Depreciation and amortisation', {}).get(period, 0) or 0
        oe = items.get('Other expenses', {}).get(period, 0) or 0
        fc = items.get('Finance costs', {}).get(period, 0) or 0
        tax = items.get('Total tax expense', {}).get(period, 0) or 0
        pat = items.get('Profit for the year', {}).get(period, 0) or 0
        pbt = items.get('Profit before tax', {}).get(period, 0) or 0

        opex = emp + cop + dep + oe
        op_profit = rev - opex
        ebitda = op_profit + dep

        metrics[period] = {
            'Revenue from Operations': rev,
            'Other Income': oi,
            'Total Income': rev + oi,
            'Employee Benefits Expense': emp,
            'Cost of Professionals': cop,
            'Depreciation & Amortisation': dep,
            'Other Expenses': oe,
            'Total Operating Expenses': opex,
            'Operating Profit (EBIT)': op_profit,
            'Finance Costs': fc,
            'Profit Before Tax': pbt,
            'Total Tax Expense': tax,
            'Profit After Tax': pat,
            'EBITDA': ebitda,
            'Operating Margin (%)': (op_profit / rev * 100) if rev else 0,
            'EBITDA Margin (%)': (ebitda / rev * 100) if rev else 0,
            'PBT Margin (%)': (pbt / rev * 100) if rev else 0,
            'PAT Margin (%)': (pat / rev * 100) if rev else 0,
        }
    return metrics
