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
# Fallback: extract note reference from raw PDF text
# -------------------------------------------------------------------

def _extract_note_ref_from_text(pdf_path: str, pnl_page: int,
                                 item_keyword: str = "other expenses") -> str | None:
    """
    Extract note reference for a P&L item using raw page text as fallback.

    In annual reports, P&L rows are typically formatted as:
        Label   NoteRef   CurrentYear   PreviousYear
    e.g.:
        Other expenses   27   1,234.56   987.65

    This scans the P&L page text for lines containing the keyword and
    extracts a standalone 1-2 digit note reference number.
    """
    doc = fitz.open(pdf_path)

    for page_offset in range(2):
        page_idx = pnl_page + page_offset
        if page_idx >= doc.page_count:
            break
        text = doc[page_idx].get_text()
        lines = text.split('\n')

        for i, line in enumerate(lines):
            line_lower = line.lower().strip()
            if item_keyword not in line_lower:
                continue

            # Method 1: note ref embedded in the same line after the keyword
            # e.g. "Other expenses  27  1,234  987"
            parts = re.split(r'\s{2,}', line.strip())
            for part in parts:
                part = part.strip()
                if re.match(r'^\d{1,2}$', part) and 1 <= int(part) <= 60:
                    doc.close()
                    return part

            # Method 2: note ref is on the immediately following line(s)
            for j in range(i + 1, min(i + 3, len(lines))):
                next_line = lines[j].strip()
                if re.match(r'^\d{1,2}$', next_line) and 1 <= int(next_line) <= 60:
                    doc.close()
                    return next_line

    doc.close()
    return None


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

        # Fallback: extract note reference from raw text if Docling missed it
        if 'Other expenses' in extracted and 'Other expenses' not in note_refs:
            text_note_ref = _extract_note_ref_from_text(pdf_path, pnl_page)
            if text_note_ref:
                note_refs['Other expenses'] = text_note_ref
                logger.info(f"Note ref for 'Other expenses' found via text fallback: "
                            f"{text_note_ref}")

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
# Note table selection (scoring-based)
# -------------------------------------------------------------------

# Common expense line-item keywords found in "Other expenses" notes
_NOTE_EXPENSE_KEYWORDS = [
    'travelling', 'conveyance', 'communication', 'rent', 'lease',
    'insurance', 'professional', 'legal', 'repairs', 'maintenance',
    'power', 'fuel', 'electricity', 'printing', 'stationery',
    'advertisement', 'publicity', 'corporate social', 'csr',
    'miscellaneous', 'rates and taxes', 'outsourced', 'manpower',
    'recruitment', 'training', 'subscription', 'membership',
    'bank charges', 'office', 'software', 'license', 'audit',
    'donation', 'bad debts', 'provision', 'loss on',
]


def _find_best_note_table(tables, note_number: str):
    """
    Score and pick the table most likely to be the "Other expenses" note.
    Returns (best_table, index) or (None, -1).
    """
    note_esc = re.escape(note_number)
    best_table = None
    best_score = -1
    best_idx = -1

    for i, df in enumerate(tables):
        if len(df) < 2:
            continue

        text = ' '.join(
            str(v).lower() for v in df.values.flatten() if v is not None
        )
        score = 0

        # Strong signal: note number heading (e.g. "27." or "note 27")
        if re.search(rf'\b{note_esc}\s*[.\-–—:)]', text):
            score += 4
        # Strong signal: "other expenses" in table
        if 'other expenses' in text:
            score += 4
        # Moderate signal: common expense keywords
        expense_hits = sum(1 for kw in _NOTE_EXPENSE_KEYWORDS if kw in text)
        score += min(expense_hits, 6)
        # Prefer tables with a reasonable number of rows (5-40)
        if 5 <= len(df) <= 40:
            score += 1
        # Weak signal: "total" present (notes usually have a total row)
        if 'total' in text:
            score += 1

        if score > best_score:
            best_score = score
            best_table = df
            best_idx = i

    return (best_table, best_idx) if best_score >= 3 else (None, -1)


# -------------------------------------------------------------------
# Fallback: text-based note extraction
# -------------------------------------------------------------------

def _extract_note_from_text(pdf_path: str, note_page: int,
                             note_number: str,
                             max_pages: int = 3) -> tuple[list[dict], dict | None]:
    """
    Extract note line items from raw PDF text.

    Used as a fallback when Docling table extraction yields no usable data.
    Reads up to *max_pages* starting from *note_page*, locates the note
    heading, skips header/unit rows, and parses label + value pairs until
    the next note heading or a clear section boundary.
    """
    doc = fitz.open(pdf_path)
    all_lines: list[str] = []

    for offset in range(max_pages):
        page_idx = note_page + offset
        if page_idx >= doc.page_count:
            break
        text = doc[page_idx].get_text()
        all_lines.extend(text.split('\n'))
    doc.close()

    note_esc = re.escape(note_number)
    heading_re = re.compile(rf'^\s*{note_esc}\s*[.\-–—:)]\s', re.IGNORECASE)

    # 1. Locate note heading
    note_start = None
    for idx, line in enumerate(all_lines):
        if heading_re.match(line.strip()):
            note_start = idx
            break
    if note_start is None:
        return [], None

    # 2. Skip header / unit rows
    skip_kw = [
        'for the year', 'march 31', 'march 31,', 'particulars',
        '\u20b9', 'in rs', 'in inr', 'in million', 'in lakhs',
        'in crore', 'in thousands', '(audited)', '(unaudited)',
    ]
    i = note_start + 1
    while i < len(all_lines):
        stripped = all_lines[i].strip().lower()
        if not stripped or any(s in stripped for s in skip_kw):
            i += 1
        else:
            break

    # 3. Parse items until next note heading or section boundary
    next_note_re = re.compile(r'^\s*(\d{1,2})\s*[.\-–—:)]\s+[A-Za-z]')
    note_items: list[dict] = []
    current_label: str | None = None

    while i < len(all_lines):
        line = all_lines[i].strip()
        i += 1

        if not line:
            continue

        # Stop at next note heading
        m = next_note_re.match(line)
        if m and m.group(1) != note_number:
            break
        # Stop at footnotes / signatures
        if line.startswith('*') and len(line) > 5 and any(c.isalpha() for c in line):
            break

        # Try to split a single line into label + values
        # e.g. "Travelling and conveyance  123.45  98.76"
        parts = re.split(r'\s{2,}', line)
        if len(parts) >= 3:
            maybe_label = parts[0]
            maybe_vals = [_parse_number(p) for p in parts[1:]]
            nums = [v for v in maybe_vals if v is not None]
            if len(nums) >= 2 and any(c.isalpha() for c in maybe_label):
                note_items.append({
                    'label': maybe_label.strip(),
                    'current': nums[0],
                    'previous': nums[1],
                })
                current_label = None
                continue

        # Pure numeric line → attach to current label
        val = _parse_number(line)
        if val is not None and not any(c.isalpha() for c in line):
            if current_label is not None:
                existing = next(
                    (x for x in note_items if x['label'] == current_label), None
                )
                if existing is None:
                    note_items.append({
                        'label': current_label,
                        'current': val,
                        'previous': None,
                    })
                elif existing.get('previous') is None:
                    existing['previous'] = val
                    current_label = None
            continue

        # Otherwise it's a label line
        if any(c.isalpha() for c in line):
            # Skip if it looks like a sub-heading that repeats the note number
            if re.match(rf'^\s*{note_esc}\s*[.\-–—:)]', line, re.IGNORECASE):
                continue
            current_label = line

    # Ensure every item has a 'previous' value
    for item in note_items:
        if item.get('previous') is None:
            item['previous'] = 0.0

    # Identify total
    total_item = None
    for item in reversed(note_items):
        if 'total' in item['label'].lower():
            total_item = item
            break
    if total_item is None and note_items:
        total_item = note_items[-1]

    return note_items, total_item


# -------------------------------------------------------------------
# Public API: Note breakup extraction
# -------------------------------------------------------------------

def extract_note_docling(pdf_path: str, note_page: int,
                          note_number: str) -> tuple[list[dict], dict | None]:
    """
    Extract note breakup using Docling, with text-based fallback.

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

    # Try up to 3 pages (notes can span across pages)
    target_pages = [note_page]
    for p in (note_page + 1, note_page + 2):
        if p < total_pages:
            target_pages.append(p)

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
            logger.warning("Docling found no tables on note pages, "
                           "falling back to text extraction")
            return _extract_note_from_text(pdf_path, note_page, note_number)

        # Use scoring to pick the best "Other expenses" note table
        note_table, idx = _find_best_note_table(tables, note_number)
        if note_table is None:
            note_table = max(tables, key=len)
            logger.warning("Could not identify note table by scoring, "
                           "using largest table")

        logger.info(f"Note table: {len(note_table)} rows x "
                     f"{len(note_table.columns)} cols (table idx={idx})")

        # Identify columns
        label_col, curr_col, prev_col = _identify_value_columns_df(note_table)

        # Extract items
        note_esc = re.escape(note_number)
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
                'in rs', 'in inr', 'in million', 'in lakhs', 'in crore',
            ]):
                continue
            # Skip the note heading row itself (e.g. "27. Other expenses")
            if re.match(rf'^\s*{note_esc}\s*[.\-–—:)]', label, re.IGNORECASE):
                continue
            # Skip rows that are just the keyword header
            if label_lower in ('other expenses', 'other expense'):
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

        # If Docling extracted very few items, try text fallback as supplement
        if len(note_items) < 3:
            logger.info("Docling extracted few note items, trying text fallback")
            text_items, text_total = _extract_note_from_text(
                pdf_path, note_page, note_number
            )
            if len(text_items) > len(note_items):
                logger.info(f"Text fallback found {len(text_items)} items "
                            f"(vs Docling {len(note_items)}), using text result")
                return text_items, text_total

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
