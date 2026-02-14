"""
Regex/pattern-based extraction logic.
Used as a fallback and for validation alongside Claude API extraction.
"""

import re

import fitz

from app.pdf_utils import is_note_ref, is_value_line, parse_number

# -------------------------------------------------------------------
# Stage 1: Find standalone financial statement pages
# -------------------------------------------------------------------


# P&L title patterns found across different annual reports.
# We use regex so matching is resilient to OCR/newline differences such as:
#   - "Statement of Standalone Profit and Loss"
#   - "Statement of Profit\nand Loss"
#   - "Profit & Loss Account"
#   - "Statement of Profit or Loss"  (IFRS terminology)
#   - "Income and Expenditure Account" (non-profit / older format)
#   - "Statement of Income and Expenses"
_PNL_TITLE_REGEXES = [
    # Standard Indian GAAP / Ind AS titles
    re.compile(r'statement\s+of\s+(?:standalone\s+)?profit\s*(?:and|&)\s*loss'),
    re.compile(r'profit\s*(?:and|&)\s*loss\s+account'),
    re.compile(r'profit\s*(?:and|&)\s*loss\s+statement'),
    # IFRS / international variants
    re.compile(r'statement\s+of\s+profit\s+or\s+loss'),
    re.compile(r'statement\s+of\s+(?:total\s+)?(?:comprehensive\s+)?income'),
    re.compile(r'statement\s+of\s+income\s+and\s+expenses?'),
    re.compile(r'statement\s+of\s+operations'),
    # Older / non-profit / alternate formats
    re.compile(r'income\s+and\s+expenditure\s+(?:account|statement)'),
    re.compile(r'income\s+(?:and|&)\s+expense\s+statement'),
    re.compile(r'revenue\s+(?:account|statement)'),
    # Short-form / OCR-damaged titles
    re.compile(r'(?:standalone\s+)?p\s*(?:&|and)\s*l\s+(?:account|statement)'),
    # Catch-all with "profit" and "loss" close together
    re.compile(r'profit\s*(?:and|&|or)\s*loss'),
]


def _normalise_for_title_match(text: str) -> str:
    """Normalise whitespace and punctuation for robust title matching.

    Handles OCR artefacts, Unicode variants, and formatting differences.
    """
    lower = text.lower()
    # Normalise Unicode dashes, quotes, and special characters
    lower = lower.replace('\u2018', "'").replace('\u2019', "'")
    lower = lower.replace('\u201c', '"').replace('\u201d', '"')
    lower = lower.replace('\u2014', '-').replace('\u2013', '-').replace('\u2012', '-')
    lower = lower.replace('\u00a0', ' ')  # non-breaking space
    # Common OCR artefacts: 'l' <-> '1', 'O' <-> '0', 'S' <-> '5'
    # We don't do character replacement but collapse noise chars
    lower = re.sub(r'[|_~]', ' ', lower)
    # Join line breaks/hard spacing to handle split titles.
    lower = re.sub(r'\s+', ' ', lower)
    # Treat slash/hyphen variants as separators.
    lower = lower.replace('/', ' ').replace('-', ' ')
    return lower


def _has_pnl_title(text_lower: str) -> bool:
    """Check if text contains any recognised P&L title variant."""
    normalised = _normalise_for_title_match(text_lower)
    return any(pattern.search(normalised) for pattern in _PNL_TITLE_REGEXES)


def _is_likely_toc_page(text: str) -> bool:
    """Heuristic check for table-of-contents/summary pages.

    TOC pages often contain many short lines that end with integer page
    numbers/ranges (e.g. "Balance Sheet 51", "Notes ... 54-76").
    These pages can mention all statement names and otherwise look like
    valid targets, so we explicitly filter them out.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return False

    joined_header = ' '.join(lines[:8]).lower()
    # Expanded markers to catch more TOC/index variations
    toc_markers = (
        'table of contents', 'contents', 'index',
        'sr. no.', 'sr no', 'serial no', 'particulars',
        'list of', 'annexure',
    )
    # Only treat "contents" and "index" as TOC if they appear as standalone
    # headings (not as part of longer phrases like "insurance contents")
    for marker in toc_markers:
        if marker in joined_header:
            # For short markers, verify they appear as a heading
            if marker in ('contents', 'index'):
                # Must appear as a standalone word/heading
                if re.search(rf'\b{marker}\b', joined_header):
                    return True
            else:
                return True

    toc_entry_pattern = re.compile(
        r"^.*[A-Za-z].*(?:\.{2,}|\s)\d{1,3}(?:\s*-\s*\d{1,3})?$"
    )
    # Also catch entries like "Profit and Loss Statement ........... 42"
    toc_dotted_pattern = re.compile(r'\.{3,}')

    toc_like = 0
    dotted_lines = 0
    for line in lines[:50]:
        # Skip normal financial value rows (usually include commas/decimals).
        if ',' in line or re.search(r'\d+\.\d+', line):
            continue
        if toc_entry_pattern.match(line):
            toc_like += 1
        if toc_dotted_pattern.search(line):
            dotted_lines += 1

    sample_size = min(len(lines), 50)

    # Require both absolute and relative density to avoid false positives.
    if toc_like >= 4 and (toc_like / max(sample_size, 1)) >= 0.20:
        return True
    # Many dotted leader lines is also a strong TOC signal
    if dotted_lines >= 5:
        return True

    return False


# -------------------------------------------------------------------
# Content-based P&L page scoring (fallback when no title match)
# -------------------------------------------------------------------

# Keywords strongly associated with P&L / Statement of Profit and Loss pages.
# Weighted by specificity: higher weight = more unique to P&L pages.
_PNL_CONTENT_KEYWORDS = {
    # Very strong signals (unique to P&L)
    'revenue from operations': 5,
    'profit before tax': 5,
    'profit for the year': 5,
    'profit for the period': 5,
    'profit before exceptional': 5,
    'total comprehensive income': 4,
    'earnings per share': 4,
    'basic eps': 4,
    'diluted eps': 4,
    # Strong signals
    'other income': 3,
    'total income': 3,
    'total expenses': 3,
    'employee benefits expense': 3,
    'employee benefit expense': 3,
    'finance costs': 3,
    'finance cost': 3,
    'depreciation and amortisation': 3,
    'depreciation and amortization': 3,
    'cost of materials consumed': 3,
    'other expenses': 2,
    'current tax': 2,
    'deferred tax': 2,
    'tax expense': 2,
    # Moderate signals (present in P&L but also in other statements)
    'income from operations': 2,
    'operating revenue': 2,
    'cost of goods sold': 2,
    'cost of revenue': 2,
    'gross profit': 2,
    'operating profit': 2,
    'net profit': 2,
    'profit after tax': 2,
    'loss before tax': 2,
    'loss for the year': 2,
    'ebitda': 1,
}

# Keywords that indicate a page is NOT a P&L page (negative signals).
_PNL_NEGATIVE_KEYWORDS = [
    'table of contents', 'contents', 'index',
    'director', 'auditor', 'governance', 'management discussion',
    'chairman', 'board of', 'secretary', 'compliance',
    'notice of', 'agenda',
]


def _score_page_as_pnl(text: str, require_standalone: bool = False) -> int:
    """Score a page for how likely it is to be a P&L statement.

    Args:
        text: Raw text of the page.
        require_standalone: If True, pages labelled "consolidated" are
            heavily penalised and pages with a standalone/separate label
            get a bonus. Set this to True when the document is known to
            contain both standalone and consolidated sections.

    Returns a weighted score. Higher = more likely to be P&L.
    Typical genuine P&L pages score 25+. Non-P&L pages score < 10.
    """
    lower = _normalise_for_title_match(text)
    score = 0

    # Positive signals
    for keyword, weight in _PNL_CONTENT_KEYWORDS.items():
        if keyword in lower:
            score += weight

    # Negative signals (pages that mention P&L keywords in passing)
    for neg_kw in _PNL_NEGATIVE_KEYWORDS:
        if neg_kw in lower:
            score -= 8

    # Bonus: if the page has a recognisable P&L title
    if _has_pnl_title(text.lower()):
        score += 10

    # Penalise very short pages (likely headers/footers only)
    if len(text.strip()) < 200:
        score -= 10

    # Penalise TOC-like pages
    if _is_likely_toc_page(text):
        score -= 20

    # --- Guardrails for multi-section reports ---
    if require_standalone:
        # Heavily penalise consolidated pages so we don't accidentally
        # pick the consolidated P&L instead of standalone.
        header_text = '\n'.join(text.split('\n')[:10]).lower()
        if 'consolidated' in header_text:
            score -= 30
        # Bonus for pages explicitly labelled standalone/separate
        if _has_standalone_label(header_text):
            score += 15

    return score


def find_pnl_by_content_scoring(pdf_path: str, min_score: int = 20) -> dict:
    """Find P&L page by scoring each page on financial keyword density.

    This is used as a last-resort fallback when neither Claude nor regex
    title matching finds a P&L page (e.g. scanned PDFs, no index, unusual
    formatting, or OCR-damaged titles).

    GUARDRAIL: If the document contains a consolidated section, content
    scoring is applied with ``require_standalone=True`` so that
    consolidated pages are penalised and standalone pages are preferred.
    Without this guard, generic P&L keywords like "staff cost" or
    "profit before tax" could match the consolidated P&L instead.

    Args:
        pdf_path: Path to the PDF file
        min_score: Minimum score to consider a page as P&L (default 20)

    Returns:
        Dict with 'pnl' key mapping to the best scoring page index,
        or empty dict if no page scores above min_score.
    """
    doc = fitz.open(pdf_path)

    # Determine if this is a multi-section report so we can guard
    # against accidentally picking a consolidated page.
    has_consolidated = _has_consolidated_section(doc)

    best_page = -1
    best_score = 0

    for i in range(doc.page_count):
        text = doc[i].get_text()
        score = _score_page_as_pnl(text, require_standalone=has_consolidated)
        if score > best_score:
            best_score = score
            best_page = i

    doc.close()

    if best_score >= min_score and best_page >= 0:
        return {'pnl': best_page}
    return {}


def _has_consolidated_section(doc) -> bool:
    """Check if the PDF contains actual consolidated financial statement pages.

    Only checks page headers (first ~10 lines) so that incidental mentions
    of 'consolidated' in notes, table of contents, or director's report
    don't cause false positives.

    Also returns True if the document has explicit "standalone" / "separate"
    labels on any financial statement page, since that implies the existence
    of a separate consolidated section (even if we haven't scanned it yet).
    """
    for i in range(doc.page_count):
        text = doc[i].get_text()
        if _is_likely_toc_page(text):
            continue
        # Only look at the first ~10 lines (page header/title area)
        header = '\n'.join(text.split('\n')[:10]).lower()
        is_financial = (
            _has_pnl_title(header)
            or 'balance sheet' in header
            or 'cash flow' in header
        )
        if not is_financial:
            continue
        # Explicit consolidated label on a financial page
        if 'consolidated' in header:
            return True
        # Explicit standalone/separate label implies consolidated exists elsewhere
        if _has_standalone_label(header):
            return True
    return False


# Variations of "standalone" label across different annual reports
_STANDALONE_LABELS = ['standalone', 'separate', 'individual']


def _has_standalone_label(text_lower: str) -> bool:
    """Check if text contains any variant of 'standalone' labelling."""
    return any(label in text_lower for label in _STANDALONE_LABELS)


def find_standalone_pages(pdf_path: str) -> tuple[dict, int]:
    """Identify pages containing standalone financial statements.

    Uses a multi-pass strategy:
      Pass 1: Look for pages explicitly labelled "standalone" / "separate"
      Pass 2: For single-entity reports (no consolidated section), match
              any page with a financial statement title
      Pass 3: Content-based scoring fallback for P&L pages
    """
    doc = fitz.open(pdf_path)
    pages = {}

    # --- Pass 1: look for explicitly labelled "standalone" pages ---
    for i in range(doc.page_count):
        text = doc[i].get_text()
        if _is_likely_toc_page(text):
            continue
        lower = text.lower()
        if _has_pnl_title(lower) and _has_standalone_label(lower) and 'pnl' not in pages:
            pages['pnl'] = i
        if 'balance sheet' in lower and _has_standalone_label(lower) and 'bs' not in pages:
            pages['bs'] = i
        if 'cash flow' in lower and _has_standalone_label(lower) and 'cf' not in pages:
            pages['cf'] = i

    # --- Pass 2: single-entity fallback (no consolidated section) ---
    if 'pnl' not in pages and not _has_consolidated_section(doc):
        for i in range(doc.page_count):
            text = doc[i].get_text()
            if _is_likely_toc_page(text):
                continue
            lower = text.lower()
            if _has_pnl_title(lower) and 'pnl' not in pages:
                pages['pnl'] = i
            if 'balance sheet' in lower and 'bs' not in pages:
                # Avoid matching table-of-contents or index pages
                if len(text) > 200:
                    pages['bs'] = i
            if 'cash flow' in lower and 'cf' not in pages:
                if len(text) > 200:
                    pages['cf'] = i

    # --- Pass 3: content-based scoring fallback for P&L ---
    # GUARDRAIL: only use content scoring when the doc has NO consolidated
    # section, OR apply it with require_standalone=True so that consolidated
    # pages are penalised.  Generic keywords (e.g. "staff cost", "profit
    # before tax") appear on BOTH standalone and consolidated pages, so
    # without this guard we could pick the wrong one.
    if 'pnl' not in pages:
        has_consolidated = _has_consolidated_section(doc)
        best_page = -1
        best_score = 0
        for i in range(doc.page_count):
            text = doc[i].get_text()
            score = _score_page_as_pnl(text, require_standalone=has_consolidated)
            if score > best_score:
                best_score = score
                best_page = i
        if best_score >= 20 and best_page >= 0:
            pages['pnl'] = best_page

    total = doc.page_count
    doc.close()
    return pages, total


def find_all_standalone_candidates(pdf_path: str) -> dict[str, list[int]]:
    """
    Scan ALL pages for potential standalone P&L matches.

    Unlike find_standalone_pages() which returns only the first match,
    this returns ALL candidate P&L page numbers so the user can confirm
    when there is ambiguity (e.g. pages without headings or multiple matches).

    For single-entity reports (no consolidated section), pages are matched
    by "statement of profit and loss" alone.

    Also includes content-scored pages as candidates when they score highly.

    Returns:
        Dict with "pnl" key mapping to list of 0-indexed page numbers:
        {"pnl": [45, 102]}
    """
    doc = fitz.open(pdf_path)
    candidates: dict[str, list[int]] = {"pnl": []}
    has_consolidated = _has_consolidated_section(doc)

    for i in range(doc.page_count):
        text = doc[i].get_text()
        if _is_likely_toc_page(text):
            continue
        lower = text.lower()

        if _has_pnl_title(lower):
            if has_consolidated:
                # Only match explicitly labelled "standalone" / "separate" pages
                if _has_standalone_label(lower):
                    candidates['pnl'].append(i)
            else:
                # Single-entity report: any P&L page is a candidate
                candidates['pnl'].append(i)

    # If no title-based candidates found, try content scoring.
    # GUARDRAIL: use require_standalone when consolidated section exists
    # to avoid matching consolidated P&L pages.
    if not candidates['pnl']:
        for i in range(doc.page_count):
            text = doc[i].get_text()
            score = _score_page_as_pnl(text, require_standalone=has_consolidated)
            if score >= 20:
                candidates['pnl'].append(i)

    doc.close()
    return candidates


def compute_pnl_confidence(num_candidates: int, claude_identified: bool) -> float:
    """
    Compute confidence score (0.0-1.0) for the P&L page identification.

    Logic:
      - 1 candidate → 1.0 (certain)
      - 2 candidates + Claude picked one → 0.75 (above 70% threshold)
      - 2 candidates, no Claude → 0.50 (below threshold → prompt user)
      - 3+ candidates + Claude → 0.58 (below threshold → prompt user)
      - 3+ candidates, no Claude → 0.33 (low → prompt user)
      - 0 candidates → 0.0

    The 70% threshold is used to decide whether to prompt the user.
    """
    if num_candidates <= 0:
        return 0.0
    if num_candidates == 1:
        return 1.0

    base = 1.0 / num_candidates
    if claude_identified:
        base += 0.25

    return min(base, 1.0)


# -------------------------------------------------------------------
# Stage 2A: P&L Extraction (regex-based)
# -------------------------------------------------------------------

PNL_TARGETS = [
    ('Revenue from operations', ['Revenue from operations']),
    ('Other income', ['Other income']),
    ('Total income', ['Total income']),
    ('Cost of materials consumed', ['Cost of materials consumed', 'Cost of materials']),
    ('Employee benefits expense', ['Employee benefits expense']),
    ('Cost of professionals', ['Cost of professionals']),
    ('Finance costs', ['Finance costs']),
    ('Depreciation and amortisation', ['Depreciation and amortisation', 'Depreciation and amortization']),
    ('Other expenses', ['Other expenses', 'Administrative Charges', 'Administrative expenses']),
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
                   search_keyword: str = "Other expenses",
                   max_pages_to_search: int = 80) -> tuple[int | None, int | None]:
    """
    Find the PDF page containing a specific note number.

    Uses multiple search strategies in order of specificity:
      1. Note number + keyword on the same line (e.g., "27. Other expenses")
      2. Note number at start of line with keyword within nearby lines (±4)
      3. Broader search for the note number heading on any notes page

    Limits search to max_pages_to_search pages after search_start_page
    to avoid scanning the entire PDF and false-matching on random content.
    """
    doc = fitz.open(pdf_path)
    search_end = min(search_start_page + max_pages_to_search, doc.page_count)
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

    for i in range(search_start_page, search_end):
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

    for i in range(search_start_page, search_end):
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
    for i in range(search_start_page, search_end):
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

    # ------- Strategy 4: note heading only (no keyword required) -------
    # Some reports have the note inside a big combined table where the
    # keyword "Other expenses" does not appear as standalone text.
    # Search for just the note number heading pattern in the notes section.
    note_heading_re = re.compile(
        rf'(?:^|\n)\s*(?:note\s*)?{note_esc}\s*[.\-–—:)]\s+[A-Za-z]',
        re.IGNORECASE | re.MULTILINE,
    )
    for i in range(search_start_page, search_end):
        text = doc[i].get_text()
        m = note_heading_re.search(text)
        if m:
            lines = [l.strip() for l in text.split('\n')]
            for j, line in enumerate(lines):
                if re.search(rf'(?:note\s*)?{note_esc}\s*[.\-–—:)]', line, re.IGNORECASE):
                    doc.close()
                    return i, j

    # ------- Strategy 5: keyword on page in notes section -------
    # If the note number heading is absent (no heading), just find a page
    # in the notes section that mentions the keyword "Other expenses".
    for i in range(search_start_page, search_end):
        text = doc[i].get_text()
        lower_text = text.lower()
        if keyword_lower in lower_text and 'expense' in lower_text:
            doc.close()
            return i, None

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
# Stage 2C: Validate note extraction against P&L
# -------------------------------------------------------------------

def validate_note_extraction(pnl: dict, note_items: list,
                              note_total: dict | None,
                              note_num: str | None,
                              tolerance: float | None = None) -> list[dict]:
    """
    Validate the extracted note breakup against P&L figures.

    Tolerance is dynamic by default: 0.1% of the P&L value (minimum 1.0).
    This handles rounding differences between notes and P&L.

    Returns a list of check dicts:
        [{"name": str, "actual": float, "expected": float, "ok": bool}, ...]
    """
    items = pnl.get('items', {})
    pnl_oe_cy = items.get('Other expenses', {}).get('current', 0) or 0

    # Dynamic tolerance: 0.1% of the value, minimum 1.0
    if tolerance is None:
        tolerance = max(1.0, abs(pnl_oe_cy) * 0.001)
    pnl_oe_py = items.get('Other expenses', {}).get('previous', 0) or 0

    note_total_cy = (note_total.get('current', 0) or 0) if note_total else 0
    note_total_py = (note_total.get('previous', 0) or 0) if note_total else 0

    checks: list[dict] = []

    # 1. Note total CY vs P&L Other Expenses CY
    checks.append({
        'name': f'Note {note_num or "?"} total (CY) vs P&L Other Expenses (CY)',
        'actual': note_total_cy,
        'expected': pnl_oe_cy,
        'ok': abs(note_total_cy - pnl_oe_cy) < tolerance,
    })

    # 2. Note total PY vs P&L Other Expenses PY
    checks.append({
        'name': f'Note {note_num or "?"} total (PY) vs P&L Other Expenses (PY)',
        'actual': note_total_py,
        'expected': pnl_oe_py,
        'ok': abs(note_total_py - pnl_oe_py) < tolerance,
    })

    # 3. Sum of individual note items vs note total (CY)
    if note_items:
        # Exclude the total row itself from the sum
        non_total = [ni for ni in note_items
                     if 'total' not in ni.get('label', '').lower()]
        items_sum_cy = sum(ni.get('current', 0) or 0 for ni in non_total)
        # Only check if there are non-total rows and a reported total
        if non_total and note_total_cy:
            checks.append({
                'name': 'Sum of note line items (CY) vs Note total (CY)',
                'actual': items_sum_cy,
                'expected': note_total_cy,
                'ok': abs(items_sum_cy - note_total_cy) < tolerance,
            })

    # 4. Note item count (informational — always "ok")
    checks.append({
        'name': 'Note line items extracted (count)',
        'actual': float(len(note_items)),
        'expected': float(len(note_items)),
        'ok': len(note_items) > 0,
    })

    return checks


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
        cmc = items.get('Cost of materials consumed', {}).get(period, 0) or 0
        emp = items.get('Employee benefits expense', {}).get(period, 0) or 0
        cop = items.get('Cost of professionals', {}).get(period, 0) or 0
        dep = items.get('Depreciation and amortisation', {}).get(period, 0) or 0
        oe = items.get('Other expenses', {}).get(period, 0) or 0
        fc = items.get('Finance costs', {}).get(period, 0) or 0
        tax = items.get('Total tax expense', {}).get(period, 0) or 0
        pat = items.get('Profit for the year', {}).get(period, 0) or 0
        pbt = items.get('Profit before tax', {}).get(period, 0) or 0

        opex = cmc + emp + cop + dep + oe
        op_profit = rev - opex
        ebitda = op_profit + dep

        metrics[period] = {
            'Revenue from Operations': rev,
            'Other Income': oi,
            'Total Income': rev + oi,
            'Cost of Materials Consumed': cmc,
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
