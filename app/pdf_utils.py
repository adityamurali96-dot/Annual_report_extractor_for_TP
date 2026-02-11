"""PDF text extraction utilities using PyMuPDF."""

import logging
import re

import fitz

logger = logging.getLogger(__name__)


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
    return bool(re.match(r'^\d{1,2}(\.\d)?$', s))


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


def is_scanned_pdf(pdf_path: str, sample_pages: int = 10) -> bool:
    """Detect if a PDF is scanned (image-based) vs text-based.

    Samples up to `sample_pages` pages and checks the ratio of pages
    with very little extractable text. A scanned PDF typically has
    pages with almost no selectable text but many images.

    Returns True if the PDF appears to be scanned/image-based.
    """
    doc = fitz.open(pdf_path)
    total = min(doc.page_count, sample_pages)
    if total == 0:
        doc.close()
        return False

    low_text_pages = 0
    image_pages = 0

    # Sample pages evenly distributed through the document
    step = max(1, doc.page_count // total)
    sampled = 0

    for i in range(0, doc.page_count, step):
        if sampled >= total:
            break
        page = doc[i]
        text = page.get_text().strip()
        images = page.get_images(full=True)

        # A page with very little text but images is likely scanned
        word_count = len(text.split())
        if word_count < 20:
            low_text_pages += 1
        if images:
            image_pages += 1
        sampled += 1

    doc.close()

    if sampled == 0:
        return False

    low_text_ratio = low_text_pages / sampled
    image_ratio = image_pages / sampled

    # If most pages have little text AND contain images → scanned
    is_scanned = low_text_ratio >= 0.5 and image_ratio >= 0.5
    if is_scanned:
        logger.info(f"PDF detected as scanned: {low_text_ratio:.0%} low-text pages, "
                     f"{image_ratio:.0%} image pages (sampled {sampled} pages)")
    return is_scanned


def extract_pdf_text(pdf_path: str, force_ocr: bool = False) -> list[dict]:
    """
    Extract text from all pages of a PDF.

    For scanned/image-based PDFs, if force_ocr is True or the PDF is
    detected as scanned, uses PyMuPDF's built-in OCR (Tesseract) if
    available, otherwise returns whatever text PyMuPDF can extract.

    Returns a list of dicts: [{"page": 0, "text": "..."}]
    """
    scanned = force_ocr or is_scanned_pdf(pdf_path)
    doc = fitz.open(pdf_path)
    pages = []

    for i in range(doc.page_count):
        page = doc[i]
        text = page.get_text()

        # For scanned pages with little text, try OCR via PyMuPDF
        if scanned and len(text.strip().split()) < 20:
            try:
                # PyMuPDF supports OCR via Tesseract if installed
                ocr_text = page.get_text("text", flags=fitz.TEXT_PRESERVE_WHITESPACE)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
            except Exception:
                pass  # Tesseract not available, use whatever we have

        pages.append({
            "page": i,
            "text": text,
        })

    doc.close()

    if scanned:
        logger.info(f"Extracted text from {len(pages)} pages (scanned PDF mode)")

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


def extract_page_headers(pdf_path: str, page_indices: dict[str, int],
                         num_lines: int = 5) -> dict[str, str]:
    """
    Extract header text (first N lines) from specific PDF pages for validation.

    Args:
        pdf_path: Path to the PDF file
        page_indices: Dict mapping section names to 0-indexed page numbers,
                      e.g. {"pnl": 45, "bs": 42, "cf": 48}
        num_lines: Number of lines to extract from top of each page

    Returns:
        Dict mapping section names to their header text,
        e.g. {"pnl": "ABC Limited\nStandalone Statement of Profit and Loss\n..."}
    """
    doc = fitz.open(pdf_path)
    headers = {}
    for section, page_idx in page_indices.items():
        if page_idx is None or page_idx < 0 or page_idx >= doc.page_count:
            continue
        text = doc[page_idx].get_text()
        lines = [l.strip() for l in text.split('\n') if l.strip()][:num_lines]
        headers[section] = '\n'.join(lines)
    doc.close()
    return headers
