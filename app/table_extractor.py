"""
Structured table extraction from PDF financial statements.

Uses pymupdf4llm (the core engine behind the Marker framework) for converting
PDF pages to markdown with proper table structure preserved. This replaces
the fragile regex-on-raw-text approach that lost all table layout information.

pymupdf4llm leverages PyMuPDF's layout analysis and table detection engine
to output well-structured markdown tables, which are then parsed for
financial data extraction.
"""

import logging
import re

import fitz
import pymupdf4llm

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Number parsing utilities
# -------------------------------------------------------------------

def _parse_number(s) -> float | None:
    """Parse a number from a table cell, handling commas, parens, dashes."""
    if s is None:
        return None
    s = str(s).strip()
    if s in ['-', '', 'None', 'nan', '\u2014', '\u2013', 'Nil', 'nil', '- ', '\u2012']:
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
# Markdown table extraction via pymupdf4llm
# -------------------------------------------------------------------

def _extract_markdown_tables(pdf_path: str, page_indices: list[int]) -> list[list[list[str]]]:
    """
    Extract tables from PDF pages by converting to markdown via pymupdf4llm,
    then parsing the markdown table syntax.

    Tries multiple table detection strategies for robustness.
    """
    all_tables = []

    for strategy in ['lines_strict', 'lines', 'text']:
        try:
            md = pymupdf4llm.to_markdown(
                pdf_path,
                pages=page_indices,
                table_strategy=strategy,
            )
        except Exception as e:
            logger.warning(f"pymupdf4llm strategy '{strategy}' failed: {e}")
            continue

        tables = _parse_markdown_tables(md)
        if tables:
            logger.info(f"Strategy '{strategy}' found {len(tables)} tables "
                        f"({sum(len(t) for t in tables)} total rows)")
            all_tables.extend(tables)
            break  # Use first successful strategy

    return all_tables


def _parse_markdown_tables(md_text: str) -> list[list[list[str]]]:
    """
    Parse markdown text to extract all tables.
    Each table is a list of rows, each row is a list of cell strings.
    """
    tables = []
    current_table = []
    in_table = False

    for line in md_text.split('\n'):
        line = line.strip()
        if line.startswith('|') and line.endswith('|'):
            # Skip separator rows (|---|---|...)
            inner = line[1:-1]  # strip outer pipes
            if all(c in '-: |' for c in inner):
                continue
            cells = [c.strip() for c in line.split('|')[1:-1]]
            current_table.append(cells)
            in_table = True
        else:
            if in_table and current_table:
                if len(current_table) >= 2:  # Meaningful table (header + data)
                    tables.append(current_table)
                current_table = []
                in_table = False

    # Don't forget the last table
    if current_table and len(current_table) >= 2:
        tables.append(current_table)

    return tables


def _extract_full_page_markdown(pdf_path: str, page_indices: list[int]) -> str:
    """Get the full markdown text for pages (used for company name detection etc.)."""
    try:
        return pymupdf4llm.to_markdown(
            pdf_path,
            pages=page_indices,
            table_strategy='lines_strict',
        )
    except Exception:
        return ''


# -------------------------------------------------------------------
# Column identification
# -------------------------------------------------------------------

def _identify_value_columns(table: list[list[str]]) -> tuple[int, int, int]:
    """
    Identify label column and two value columns from a table.
    Returns (label_col, current_year_col, previous_year_col).

    In Indian financial statements: current year is the first value
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

    # Collect columns with enough numeric data (exclude first column - labels)
    value_candidates = [
        (c, cnt) for c, cnt in enumerate(numeric_counts)
        if cnt >= min_threshold and c > 0
    ]
    value_candidates.sort(key=lambda x: x[0])

    if len(value_candidates) >= 2:
        # Second-to-last = current year, last = previous year
        curr_col = value_candidates[-2][0]
        prev_col = value_candidates[-1][0]
    elif len(value_candidates) == 1:
        curr_col = value_candidates[0][0]
        prev_col = -1
    else:
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

    # For EPS, match "Basic" or "Diluted" at start
    if item_name in ('Basic EPS', 'Diluted EPS'):
        keyword = 'basic' if item_name == 'Basic EPS' else 'diluted'
        return label_lower.startswith(keyword)

    return any(pattern in label_lower for pattern in patterns)


def _find_best_pnl_table(tables: list[list[list[str]]], min_keyword_matches: int = 3) -> list[list[str]] | None:
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
    Extract P&L data using pymupdf4llm markdown table extraction.

    Args:
        pdf_path: Path to the PDF file
        pnl_page: 0-indexed page number of the P&L statement

    Returns:
        Dict with keys: company, currency, items, note_refs
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    doc.close()

    # P&L often spans 2 pages
    pages_to_try = [pnl_page]
    if pnl_page + 1 < total_pages:
        pages_to_try.append(pnl_page + 1)

    tables = _extract_markdown_tables(pdf_path, pages_to_try)
    logger.info(f"pymupdf4llm found {len(tables)} tables on P&L pages {pages_to_try}")

    if not tables:
        raise ValueError(f"No tables found on P&L pages {pages_to_try}")

    # Find the P&L table
    pnl_table = _find_best_pnl_table(tables)
    if pnl_table is None:
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

            curr_val = _parse_number(row[curr_col]) if 0 <= curr_col < len(row) else None
            prev_val = _parse_number(row[prev_col]) if 0 <= prev_col < len(row) else None

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
                break

    # Detect company name
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
    Extract note breakup using pymupdf4llm markdown table extraction.

    Args:
        pdf_path: Path to PDF
        note_page: 0-indexed page number of the note
        note_number: The note reference number (e.g., '27')

    Returns:
        Tuple of (list of note items, total item or None)
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    doc.close()

    pages_to_try = [note_page]
    if note_page + 1 < total_pages:
        pages_to_try.append(note_page + 1)

    tables = _extract_markdown_tables(pdf_path, pages_to_try)
    logger.info(f"pymupdf4llm found {len(tables)} tables on note pages {pages_to_try}")

    if not tables:
        return [], None

    # Find the note table
    note_table = None
    for table in tables:
        text = ' '.join(str(cell or '').lower() for row in table for cell in row)
        if 'other expenses' in text or f'{note_number}.' in text or 'expense' in text:
            note_table = table
            break

    if note_table is None:
        note_table = max(tables, key=len)

    # Identify columns
    label_col, curr_col, prev_col = _identify_value_columns(note_table)

    # Extract items
    note_items = []
    for row in note_table:
        if len(row) <= label_col:
            continue
        label = str(row[label_col] or '').strip()

        if not label:
            continue
        label_lower = label.lower()
        if any(skip in label_lower for skip in [
            'for the year', 'march 31', 'particulars', 'in \u20b9',
            'in rs', 'in inr', f'{note_number}.',
        ]):
            continue

        curr_val = _parse_number(row[curr_col]) if 0 <= curr_col < len(row) else None
        prev_val = _parse_number(row[prev_col]) if 0 <= prev_col < len(row) else None

        if curr_val is not None:
            note_items.append({
                'label': label,
                'current': curr_val,
                'previous': prev_val if prev_val is not None else 0.0,
            })

    # Identify total row
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
# Page identification
# -------------------------------------------------------------------

def find_standalone_pages(pdf_path: str) -> tuple[dict, int]:
    """
    Identify pages containing standalone financial statements.
    Uses pymupdf4llm for text extraction with better layout preservation.

    Falls back to matching without "standalone" for single-entity reports
    that have no consolidated section.
    """
    from app.extractor import _has_consolidated_section, _has_pnl_title, _is_likely_toc_page

    doc = fitz.open(pdf_path)
    pages = {}
    total = doc.page_count

    # Pass 1: explicitly labelled "standalone" pages
    for i in range(total):
        text = doc[i].get_text()
        if _is_likely_toc_page(text):
            continue
        lower = text.lower()

        if _has_pnl_title(lower) and 'standalone' in lower and 'pnl' not in pages:
            pages['pnl'] = i
        if 'balance sheet' in lower and 'standalone' in lower and 'bs' not in pages:
            pages['bs'] = i
        if 'cash flow' in lower and 'standalone' in lower and 'cf' not in pages:
            pages['cf'] = i

    # Pass 2: single-entity fallback
    if 'pnl' not in pages and not _has_consolidated_section(doc):
        for i in range(total):
            text = doc[i].get_text()
            if _is_likely_toc_page(text):
                continue
            lower = text.lower()
            if _has_pnl_title(lower) and 'pnl' not in pages:
                pages['pnl'] = i
            if 'balance sheet' in lower and 'bs' not in pages:
                if len(text) > 200:
                    pages['bs'] = i
            if 'cash flow' in lower and 'cf' not in pages:
                if len(text) > 200:
                    pages['cf'] = i

    doc.close()
    return pages, total


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _detect_company_name(pdf_path: str, page_idx: int) -> str:
    """Detect company name from the top of a page."""
    doc = fitz.open(pdf_path)
    if page_idx >= doc.page_count:
        doc.close()
        return 'Unknown Company'
    text = doc[page_idx].get_text()
    doc.close()

    for line in text.split('\n')[:15]:
        line = line.strip()
        if 'Limited' in line or 'Ltd' in line:
            name = line.split('\u2014')[0].split('\u2013')[0].strip()
            name = re.sub(r'\d+$', '', name).strip()
            if len(name) > 5:
                return name

    return 'Unknown Company'
