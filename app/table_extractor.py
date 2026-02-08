"""
Structured table extraction from PDF financial statements.

Uses pdfplumber for reliable table detection and extraction, replacing
the fragile regex-on-raw-text approach. pdfplumber understands table layout
(borders, text alignment) and returns structured row/column data.

Optionally uses Docling for enhanced document understanding when available.
"""

import logging
import re
import pdfplumber

logger = logging.getLogger(__name__)

# Optional Docling support (heavy dependency - PyTorch etc.)
try:
    from docling.document_converter import DocumentConverter
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False


# -------------------------------------------------------------------
# Number parsing utilities
# -------------------------------------------------------------------

def _parse_number(s) -> float | None:
    """Parse a number from a table cell, handling commas, parens, dashes."""
    if s is None:
        return None
    s = str(s).strip()
    if s in ['-', '', 'None', 'nan', '\u2014', '\u2013', 'Nil', 'nil', '- ']:
        return 0.0
    s = s.replace(',', '').replace(' ', '').replace('\u00a0', '')
    neg = s.startswith('(') and s.endswith(')')
    if neg:
        s = s[1:-1]
    try:
        val = float(s)
        return -val if neg else val
    except ValueError:
        return None


def _is_note_ref(s: str) -> bool:
    """Check if string is a note reference like '24' or '26.1'."""
    s = str(s).strip()
    return bool(re.match(r'^\d{1,2}(\.\d)?$', s))


# -------------------------------------------------------------------
# Table extraction from PDF pages
# -------------------------------------------------------------------

def _extract_tables_from_pages(pdf_path: str, page_indices: list[int]) -> list[list[list]]:
    """Extract all tables from specified PDF pages using pdfplumber."""
    all_tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx in page_indices:
            if page_idx >= len(pdf.pages):
                continue
            page = pdf.pages[page_idx]

            # Strategy 1: Line-based detection (bordered tables)
            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            })
            if tables:
                all_tables.extend(tables)
                continue

            # Strategy 2: Text-based detection (uses text alignment)
            tables = page.extract_tables({
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "snap_x_tolerance": 5,
                "snap_y_tolerance": 5,
                "join_x_tolerance": 5,
                "join_y_tolerance": 5,
            })
            if tables:
                all_tables.extend(tables)
                continue

            # Strategy 3: Minimal settings - just try to find any table
            tables = page.extract_tables()
            if tables:
                all_tables.extend(tables)

    return all_tables


def _extract_text_lines_with_positions(pdf_path: str, page_idx: int) -> list[dict]:
    """
    Extract text with positional data for position-based table reconstruction.
    Returns words grouped by line (y-position).
    """
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx >= len(pdf.pages):
            return []
        page = pdf.pages[page_idx]
        words = page.extract_words(
            x_tolerance=3,
            y_tolerance=3,
            keep_blank_chars=True,
        )
    return words


def _reconstruct_table_from_words(words: list[dict], page_width: float = 612) -> list[list[str]]:
    """
    Reconstruct table structure from positioned words when extract_tables() fails.
    Groups words into rows by y-position, then into columns by x-position.
    """
    if not words:
        return []

    # Group words by line (y-position with tolerance)
    y_tolerance = 3
    lines = []
    current_line = [words[0]]
    for w in words[1:]:
        if abs(w['top'] - current_line[0]['top']) <= y_tolerance:
            current_line.append(w)
        else:
            lines.append(sorted(current_line, key=lambda x: x['x0']))
            current_line = [w]
    if current_line:
        lines.append(sorted(current_line, key=lambda x: x['x0']))

    # Determine column boundaries from all words
    # For financial tables: typically label (left), note ref (middle), values (right)
    all_x = [w['x0'] for line in lines for w in line]
    if not all_x:
        return []

    # Use page_width to determine column regions
    # Heuristic: label column is 0-50% of width, values are in the right 50%
    mid_x = page_width * 0.45
    right_mid = page_width * 0.7

    table = []
    for line_words in lines:
        label_parts = []
        mid_val = None
        right_val = None
        for w in line_words:
            if w['x0'] < mid_x:
                label_parts.append(w['text'])
            elif w['x0'] < right_mid:
                mid_val = w['text']
            else:
                right_val = w['text']

        label = ' '.join(label_parts).strip()
        if label or mid_val or right_val:
            table.append([label, mid_val or '', right_val or ''])

    return table


# -------------------------------------------------------------------
# Column identification
# -------------------------------------------------------------------

def _identify_value_columns(table: list[list]) -> tuple[int, int, int]:
    """
    Identify label column and two value columns from a table.
    Returns (label_col, current_year_col, previous_year_col).

    In Indian financial statements: current year is typically the first value
    column (left), previous year is the second (right).
    """
    if not table or len(table) < 2:
        return 0, -1, -1

    ncols = max(len(row) for row in table)

    # Count numeric values per column (skip first row = header)
    numeric_counts = [0] * ncols
    for row in table[1:]:
        for c in range(min(len(row), ncols)):
            cell = str(row[c] or '').strip()
            if not cell or cell in ['None', 'nan']:
                continue
            val = _parse_number(cell)
            if val is not None and not _is_note_ref(cell):
                numeric_counts[c] += 1

    min_threshold = max(1, len(table) * 0.15)

    # Collect columns with enough numeric data
    value_candidates = [
        (c, cnt) for c, cnt in enumerate(numeric_counts)
        if cnt >= min_threshold and c > 0  # exclude first column (labels)
    ]

    # Sort by column position (left to right)
    value_candidates.sort(key=lambda x: x[0])

    if len(value_candidates) >= 2:
        # Current year = first (leftmost) value column
        # Previous year = second value column
        curr_col = value_candidates[-2][0]
        prev_col = value_candidates[-1][0]
    elif len(value_candidates) == 1:
        curr_col = value_candidates[0][0]
        prev_col = -1
    else:
        # Fallback: use last two columns
        curr_col = ncols - 2 if ncols >= 3 else ncols - 1
        prev_col = ncols - 1 if ncols >= 3 else -1

    return 0, curr_col, prev_col


# -------------------------------------------------------------------
# P&L extraction
# -------------------------------------------------------------------

PNL_ITEMS = {
    'Revenue from operations': ['revenue from operations', 'revenue from operation',
                                 'income from operations'],
    'Other income': ['other income'],
    'Total income': ['total income', 'total revenue'],
    'Employee benefits expense': ['employee benefits expense', 'employee benefit expense',
                                   'employee cost'],
    'Cost of professionals': ['cost of professionals', 'subcontracting expense',
                               'cost of services'],
    'Finance costs': ['finance costs', 'finance cost', 'interest expense'],
    'Depreciation and amortisation': ['depreciation and amortisation',
                                       'depreciation and amortization',
                                       'depreciation & amortisation',
                                       'depreciation & amortization'],
    'Other expenses': ['other expenses', 'other expense'],
    'Total expenses': ['total expenses', 'total expense'],
    'Profit before tax': ['profit before exceptional', 'profit before tax',
                           'profit / (loss) before tax',
                           'profit/(loss) before tax'],
    'Current tax': ['current tax'],
    'Deferred tax': ['deferred tax'],
    'Total tax expense': ['total tax expense', 'tax expense'],
    'Profit for the year': ['profit for the year', 'profit for the period',
                             'profit / (loss) for the year',
                             'profit/(loss) for the year',
                             'net profit for the year'],
    'Total comprehensive income': ['total comprehensive income',
                                    'total comprehensive income / (loss)'],
    'Basic EPS': ['basic'],
    'Diluted EPS': ['diluted'],
}


def _match_pnl_item(label: str, item_name: str, patterns: list[str]) -> bool:
    """Check if a table row label matches a target P&L item."""
    label_lower = label.lower().strip()
    if not label_lower:
        return False

    # For EPS, need special handling - match "Basic" or "Diluted" but not sub-items
    if item_name in ('Basic EPS', 'Diluted EPS'):
        keyword = 'basic' if item_name == 'Basic EPS' else 'diluted'
        return label_lower.startswith(keyword) and 'eps' not in label_lower.split()[0]

    for pattern in patterns:
        if pattern in label_lower:
            return True
    return False


def _find_best_pnl_table(tables: list[list[list]], min_keyword_matches: int = 3) -> list[list] | None:
    """Find the table that best matches a P&L statement."""
    pnl_keywords = [
        'revenue from operations', 'profit before tax', 'profit for the year',
        'total income', 'other expenses', 'employee benefits', 'total expenses',
        'depreciation', 'finance cost',
    ]

    best_table = None
    best_score = 0

    for table in tables:
        if len(table) < 3:
            continue
        # Flatten all text in the table
        text = ' '.join(
            str(cell or '').lower()
            for row in table
            for cell in row
        )
        score = sum(1 for kw in pnl_keywords if kw in text)
        if score > best_score:
            best_score = score
            best_table = table

    return best_table if best_score >= min_keyword_matches else None


def extract_pnl_from_tables(pdf_path: str, pnl_page: int) -> dict:
    """
    Extract P&L data using pdfplumber table extraction.

    Args:
        pdf_path: Path to the PDF file
        pnl_page: 0-indexed page number of the P&L statement

    Returns:
        Dict with keys: company, currency, items, note_refs
    """
    # Extract tables from P&L page and next page (P&L often spans 2 pages)
    pages_to_try = [pnl_page]
    if pnl_page + 1 < _get_page_count(pdf_path):
        pages_to_try.append(pnl_page + 1)

    tables = _extract_tables_from_pages(pdf_path, pages_to_try)
    logger.info(f"pdfplumber found {len(tables)} tables on P&L pages {pages_to_try}")

    # If no tables found, try word-based reconstruction
    if not tables:
        logger.info("No tables via extract_tables(), trying word-based reconstruction")
        for page_idx in pages_to_try:
            words = _extract_text_lines_with_positions(pdf_path, page_idx)
            if words:
                page_width = _get_page_width(pdf_path, page_idx)
                reconstructed = _reconstruct_table_from_words(words, page_width)
                if len(reconstructed) > 5:
                    tables.append(reconstructed)

    if not tables:
        raise ValueError("No tables could be extracted from P&L pages")

    # Find the P&L table
    pnl_table = _find_best_pnl_table(tables)
    if pnl_table is None:
        # Use the largest table as fallback
        pnl_table = max(tables, key=len)
        logger.warning("Could not identify P&L table by keywords, using largest table")

    logger.info(f"P&L table: {len(pnl_table)} rows x {max(len(r) for r in pnl_table)} cols")

    # Identify value columns
    label_col, curr_col, prev_col = _identify_value_columns(pnl_table)
    logger.info(f"Column mapping: label={label_col}, current={curr_col}, previous={prev_col}")

    # Extract line items
    extracted = {}
    note_refs = {}

    for item_name, patterns in PNL_ITEMS.items():
        for row in pnl_table:
            if len(row) <= max(label_col, curr_col):
                continue
            label = str(row[label_col] or '').strip()
            if not _match_pnl_item(label, item_name, patterns):
                continue

            curr_val = _parse_number(row[curr_col]) if curr_col >= 0 and curr_col < len(row) else None
            prev_val = _parse_number(row[prev_col]) if prev_col >= 0 and prev_col < len(row) else None

            if curr_val is not None:
                extracted[item_name] = {
                    'current': curr_val,
                    'previous': prev_val if prev_val is not None else 0.0,
                }

                # Check for note reference in intermediate columns
                for c in range(label_col + 1, curr_col):
                    if c < len(row):
                        cell = str(row[c] or '').strip()
                        if _is_note_ref(cell):
                            note_refs[item_name] = cell
                            break
                break  # Found this item, move to next

    # Detect company name from first page text
    company = _detect_company_name(pdf_path, pnl_page)

    logger.info(f"Extracted {len(extracted)} P&L items: {list(extracted.keys())}")
    if len(extracted) < 5:
        logger.warning(f"Only {len(extracted)} items extracted - extraction may be incomplete")

    return {
        'company': company,
        'currency': 'INR Million',
        'items': extracted,
        'note_refs': note_refs,
    }


# -------------------------------------------------------------------
# Note breakup extraction
# -------------------------------------------------------------------

def extract_note_from_tables(pdf_path: str, note_page: int,
                              note_number: str) -> tuple[list[dict], dict | None]:
    """
    Extract note breakup using pdfplumber table extraction.

    Args:
        pdf_path: Path to PDF
        note_page: 0-indexed page number of the note
        note_number: The note reference number (e.g., '27')

    Returns:
        Tuple of (list of note items, total item or None)
    """
    # Extract from note page and potentially next page
    pages_to_try = [note_page]
    if note_page + 1 < _get_page_count(pdf_path):
        pages_to_try.append(note_page + 1)

    tables = _extract_tables_from_pages(pdf_path, pages_to_try)
    logger.info(f"pdfplumber found {len(tables)} tables on note pages {pages_to_try}")

    # If no tables found, try word-based reconstruction
    if not tables:
        logger.info("No note tables via extract_tables(), trying word-based reconstruction")
        for page_idx in pages_to_try:
            words = _extract_text_lines_with_positions(pdf_path, page_idx)
            if words:
                page_width = _get_page_width(pdf_path, page_idx)
                reconstructed = _reconstruct_table_from_words(words, page_width)
                if len(reconstructed) > 2:
                    tables.append(reconstructed)

    if not tables:
        return [], None

    # Find the note table - look for one containing "other expenses" or the note number
    note_table = None
    for table in tables:
        text = ' '.join(str(cell or '').lower() for row in table for cell in row)
        if 'other expenses' in text or f'{note_number}.' in text or 'expense' in text:
            note_table = table
            break

    if note_table is None:
        # Use the largest table
        note_table = max(tables, key=len)

    # Identify columns
    label_col, curr_col, prev_col = _identify_value_columns(note_table)

    # Extract items
    note_items = []
    for row in note_table:
        if len(row) <= label_col:
            continue
        label = str(row[label_col] or '').strip()

        # Skip header rows, empty rows, and note headings
        if not label:
            continue
        label_lower = label.lower()
        if any(skip in label_lower for skip in [
            'for the year', 'march 31', 'particulars', 'in \u20b9',
            'in rs', 'in inr', 'note', f'{note_number}.',
        ]):
            continue

        curr_val = _parse_number(row[curr_col]) if curr_col >= 0 and curr_col < len(row) else None
        prev_val = _parse_number(row[prev_col]) if prev_col >= 0 and prev_col < len(row) else None

        if curr_val is not None:
            note_items.append({
                'label': label,
                'current': curr_val,
                'previous': prev_val if prev_val is not None else 0.0,
            })

    # Identify total row (usually last row, or one labeled "Total")
    total_item = None
    for item in reversed(note_items):
        if 'total' in item['label'].lower():
            total_item = item
            break
    if total_item is None and note_items:
        total_item = note_items[-1]

    logger.info(f"Extracted {len(note_items)} note items")
    return note_items, total_item


# -------------------------------------------------------------------
# Page identification (enhanced with pdfplumber)
# -------------------------------------------------------------------

def find_standalone_pages(pdf_path: str) -> tuple[dict, int]:
    """
    Identify pages containing standalone financial statements.
    Uses pdfplumber for text extraction (better layout preservation than PyMuPDF).
    """
    pages = {}
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            lower = text.lower()

            if 'statement of profit and loss' in lower and 'standalone' in lower:
                if 'pnl' not in pages:
                    pages['pnl'] = i
            if 'balance sheet' in lower and 'standalone' in lower:
                if 'bs' not in pages:
                    pages['bs'] = i
            if 'cash flow' in lower and 'standalone' in lower:
                if 'cf' not in pages:
                    pages['cf'] = i

    return pages, total


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _get_page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def _get_page_width(pdf_path: str, page_idx: int) -> float:
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx < len(pdf.pages):
            return pdf.pages[page_idx].width
    return 612  # default letter width


def _detect_company_name(pdf_path: str, page_idx: int) -> str:
    """Detect company name from the top of a page."""
    with pdfplumber.open(pdf_path) as pdf:
        if page_idx >= len(pdf.pages):
            return 'Unknown Company'
        text = pdf.pages[page_idx].extract_text() or ''

    for line in text.split('\n')[:15]:
        line = line.strip()
        if 'Limited' in line or 'Ltd' in line:
            # Clean up: remove page numbers, extra formatting
            name = line.split('\u2014')[0].split('\u2013')[0].strip()
            name = re.sub(r'\d+$', '', name).strip()
            if len(name) > 5:
                return name

    return 'Unknown Company'
