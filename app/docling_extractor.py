"""
Docling-based extraction for standalone financial statements.

Uses IBM Docling for high-quality table extraction from targeted PDF pages.
Only processes the specific pages identified by Claude API (typically 2 pages),
making extraction fast and focused.
"""

import os
import re
import logging
import tempfile

import fitz

logger = logging.getLogger(__name__)

# Lazy-initialized converter (heavy import + model download on first use)
_converter = None


def _get_converter():
    """Lazy-init the Docling DocumentConverter with table extraction enabled."""
    global _converter
    if _converter is not None:
        return _converter

    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableStructureOptions,
        TableFormerMode,
    )

    pipeline_options = PdfPipelineOptions(
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
        do_ocr=False,
    )

    _converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    return _converter


# -------------------------------------------------------------------
# PDF page subsetting
# -------------------------------------------------------------------

def _create_page_subset_pdf(pdf_path: str, page_indices: list[int]) -> str:
    """Create a temporary PDF containing only the specified pages."""
    src = fitz.open(pdf_path)
    dst = fitz.open()
    for idx in sorted(page_indices):
        if 0 <= idx < src.page_count:
            dst.insert_pdf(src, from_page=idx, to_page=idx)
    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    dst.save(tmp.name)
    dst.close()
    src.close()
    return tmp.name


# -------------------------------------------------------------------
# Number parsing
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
# P&L line item patterns
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
    if item_name in ('Basic EPS', 'Diluted EPS'):
        keyword = 'basic' if item_name == 'Basic EPS' else 'diluted'
        return label_lower.startswith(keyword)
    for pattern in patterns:
        if pattern in label_lower:
            return True
    return False


# -------------------------------------------------------------------
# DataFrame-based table processing
# -------------------------------------------------------------------

def _identify_value_columns_df(df) -> tuple[int, int, int]:
    """
    Identify label column and two value columns from a DataFrame.
    Returns (label_col, current_year_col, previous_year_col).
    """
    ncols = len(df.columns)
    if ncols < 2:
        return 0, -1, -1

    # Count numeric values per column (skip potential header rows)
    numeric_counts = [0] * ncols
    for _, row in df.iterrows():
        for c in range(ncols):
            cell = str(row.iloc[c] or '').strip()
            if not cell or cell in ['None', 'nan', '']:
                continue
            val = _parse_number(cell)
            if val is not None and not _is_note_ref(cell):
                numeric_counts[c] += 1

    min_threshold = max(1, len(df) * 0.15)

    # Collect columns with enough numeric data (exclude first column - labels)
    value_candidates = [
        (c, cnt) for c, cnt in enumerate(numeric_counts)
        if cnt >= min_threshold and c > 0
    ]
    value_candidates.sort(key=lambda x: x[0])

    if len(value_candidates) >= 2:
        curr_col = value_candidates[-2][0]
        prev_col = value_candidates[-1][0]
    elif len(value_candidates) == 1:
        curr_col = value_candidates[0][0]
        prev_col = -1
    else:
        curr_col = ncols - 2 if ncols >= 3 else ncols - 1
        prev_col = ncols - 1 if ncols >= 3 else -1

    return 0, curr_col, prev_col


def _find_best_pnl_table(tables) -> tuple:
    """Find the table that best matches a P&L statement. Returns (table, index)."""
    pnl_keywords = [
        'revenue from operations', 'profit before tax', 'profit for the year',
        'total income', 'other expenses', 'employee benefits', 'total expenses',
        'depreciation', 'finance cost',
    ]

    best_table = None
    best_score = 0
    best_idx = -1

    for i, df in enumerate(tables):
        if len(df) < 3:
            continue
        text = ' '.join(str(v).lower() for v in df.values.flatten() if v is not None)
        score = sum(1 for kw in pnl_keywords if kw in text)
        if score > best_score:
            best_score = score
            best_table = df
            best_idx = i

    return (best_table, best_idx) if best_score >= 3 else (None, -1)


def _extract_pnl_from_df(df) -> tuple[dict, dict]:
    """Extract P&L line items and note refs from a DataFrame table."""
    label_col, curr_col, prev_col = _identify_value_columns_df(df)
    logger.info(f"Column mapping: label={label_col}, current={curr_col}, previous={prev_col}")

    extracted = {}
    note_refs = {}

    for item_name, patterns in PNL_ITEMS.items():
        for _, row in df.iterrows():
            ncols = len(row)
            if ncols <= max(label_col, curr_col):
                continue
            label = str(row.iloc[label_col] or '').strip()
            if not _match_pnl_item(label, item_name, patterns):
                continue

            curr_val = _parse_number(row.iloc[curr_col]) if 0 <= curr_col < ncols else None
            prev_val = _parse_number(row.iloc[prev_col]) if 0 <= prev_col < ncols else None

            if curr_val is not None:
                extracted[item_name] = {
                    'current': curr_val,
                    'previous': prev_val if prev_val is not None else 0.0,
                }
                # Check for note reference in intermediate columns
                for c in range(label_col + 1, curr_col):
                    if c < ncols:
                        cell = str(row.iloc[c] or '').strip()
                        if _is_note_ref(cell):
                            note_refs[item_name] = cell
                            break
                break

    return extracted, note_refs


# -------------------------------------------------------------------
# Public API: P&L extraction
# -------------------------------------------------------------------

def extract_pnl_docling(pdf_path: str, pnl_page: int) -> dict:
    """
    Extract P&L data using Docling from the identified standalone page.

    Args:
        pdf_path: Path to the full annual report PDF
        pnl_page: 0-indexed page number of the standalone P&L

    Returns:
        Dict with keys: company, currency, items, note_refs
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    doc.close()

    # P&L often spans 2 pages
    target_pages = [pnl_page]
    if pnl_page + 1 < total_pages:
        target_pages.append(pnl_page + 1)

    logger.info(f"Docling: extracting tables from pages {target_pages}")

    # Create temp PDF with only the target pages
    temp_pdf = _create_page_subset_pdf(pdf_path, target_pages)

    try:
        converter = _get_converter()
        result = converter.convert(temp_pdf)

        # Get all tables as DataFrames
        tables = []
        for table in result.document.tables:
            try:
                df = table.export_to_dataframe(doc=result.document)
                if df is not None and len(df) >= 2:
                    tables.append(df)
            except Exception as e:
                logger.warning(f"Failed to export table as DataFrame: {e}")

        logger.info(f"Docling extracted {len(tables)} tables from P&L pages")

        if not tables:
            raise ValueError(f"No tables found by Docling on pages {target_pages}")

        # Find the P&L table
        pnl_table, idx = _find_best_pnl_table(tables)
        if pnl_table is None:
            pnl_table = max(tables, key=len)
            logger.warning("Could not identify P&L table by keywords, using largest table")

        logger.info(f"P&L table: {len(pnl_table)} rows x {len(pnl_table.columns)} cols")

        # Extract line items
        extracted, note_refs = _extract_pnl_from_df(pnl_table)

        logger.info(f"Docling extracted {len(extracted)} P&L items: {list(extracted.keys())}")

    finally:
        os.unlink(temp_pdf)

    company = _detect_company_name(pdf_path, pnl_page)

    return {
        'company': company,
        'currency': 'INR Million',
        'items': extracted,
        'note_refs': note_refs,
    }


# -------------------------------------------------------------------
# Public API: Note breakup extraction
# -------------------------------------------------------------------

def extract_note_docling(pdf_path: str, note_page: int,
                          note_number: str) -> tuple[list[dict], dict | None]:
    """
    Extract note breakup using Docling.

    Args:
        pdf_path: Path to PDF
        note_page: 0-indexed page of the note
        note_number: The note reference number (e.g., '27')

    Returns:
        Tuple of (list of note items, total item or None)
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    doc.close()

    target_pages = [note_page]
    if note_page + 1 < total_pages:
        target_pages.append(note_page + 1)

    logger.info(f"Docling: extracting note tables from pages {target_pages}")

    temp_pdf = _create_page_subset_pdf(pdf_path, target_pages)

    try:
        converter = _get_converter()
        result = converter.convert(temp_pdf)

        tables = []
        for table in result.document.tables:
            try:
                df = table.export_to_dataframe(doc=result.document)
                if df is not None and len(df) >= 2:
                    tables.append(df)
            except Exception as e:
                logger.warning(f"Failed to export note table: {e}")

        if not tables:
            return [], None

        # Find the note table (look for "other expenses" or the note number)
        note_table = None
        for df in tables:
            text = ' '.join(str(v).lower() for v in df.values.flatten() if v is not None)
            if 'other expenses' in text or f'{note_number}.' in text or 'expense' in text:
                note_table = df
                break

        if note_table is None:
            note_table = max(tables, key=len)

        # Identify columns
        label_col, curr_col, prev_col = _identify_value_columns_df(note_table)

        # Extract items
        note_items = []
        for _, row in note_table.iterrows():
            ncols = len(row)
            if ncols <= label_col:
                continue
            label = str(row.iloc[label_col] or '').strip()
            if not label:
                continue
            label_lower = label.lower()
            if any(skip in label_lower for skip in [
                'for the year', 'march 31', 'particulars', 'in \u20b9',
                'in rs', 'in inr', f'{note_number}.',
            ]):
                continue

            curr_val = _parse_number(row.iloc[curr_col]) if 0 <= curr_col < ncols else None
            prev_val = _parse_number(row.iloc[prev_col]) if 0 <= prev_col < ncols else None

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

        logger.info(f"Docling extracted {len(note_items)} note items")
        return note_items, total_item

    finally:
        os.unlink(temp_pdf)


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
