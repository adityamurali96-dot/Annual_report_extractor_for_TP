"""PDF text extraction utilities using PyMuPDF."""

import fitz
import re


def parse_number(s: str) -> float | None:
    """Convert a string to a float, handling commas, dashes, and parentheses (negatives)."""
    s = s.strip().replace(',', '').replace(' ', '')
    if s in ['-', '']:
        return 0.0
    neg = s.startswith('(') and s.endswith(')')
    if neg:
        s = s[1:-1]
    try:
        val = float(s)
        return -val if neg else val
    except ValueError:
        return None


def is_note_ref(s: str) -> bool:
    """Detect note reference numbers like '24', '27', or '26.1'."""
    s = s.strip()
    if re.match(r'^\d{1,2}$', s):
        return True
    if re.match(r'^\d{1,2}\.\d$', s):
        return True
    return False


def is_value_line(s: str) -> bool:
    """Check if a string represents a numeric value."""
    s = s.strip()
    if not s or s == '-':
        return s == '-'
    test = s
    if test.startswith('(') and test.endswith(')'):
        test = test[1:-1]
    test = test.replace(',', '').strip()
    try:
        float(test)
        return True
    except ValueError:
        return False


def extract_pdf_text(pdf_path: str) -> list[dict]:
    """
    Extract text from all pages of a PDF.
    Returns a list of dicts: [{"page": 0, "text": "..."}]
    """
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        pages.append({
            "page": i,
            "text": doc[i].get_text(),
        })
    doc.close()
    return pages


def extract_pages_range(pdf_path: str, start: int, end: int) -> list[dict]:
    """Extract text from a range of PDF pages."""
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(max(0, start), min(end, doc.page_count)):
        pages.append({
            "page": i,
            "text": doc[i].get_text(),
        })
    doc.close()
    return pages
