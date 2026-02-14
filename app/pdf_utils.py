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


def is_page_scanned(page) -> bool:
    """Check if a SINGLE page is scanned (image-based with little text).

    Used during page-level extraction to decide whether individual pages
    need OCR, even if the overall PDF is text-based (hybrid PDFs).
    """
    text = page.get_text().strip()
    images = page.get_images(full=True)
    word_count = len(text.split())
    return word_count < 20 and len(images) > 0


def is_scanned_pdf(pdf_path: str, sample_pages: int = 20) -> bool:
    """Detect if a PDF is scanned (image-based) vs text-based.

    Samples pages from three zones (front, middle, back) to catch
    hybrid PDFs where financials in the middle may be scanned while
    front matter is text-based.

    Returns True if the PDF appears to be scanned/image-based.
    """
    doc = fitz.open(pdf_path)
    total = doc.page_count
    if total == 0:
        doc.close()
        return False

    # Sample from three zones:
    # - First 5 pages (front matter)
    # - Middle 10 pages (where financials usually are)
    # - Last 5 pages (notes section)
    sample_indices = set()

    # Front
    for i in range(min(5, total)):
        sample_indices.add(i)

    # Middle
    mid = total // 2
    for i in range(max(0, mid - 5), min(total, mid + 5)):
        sample_indices.add(i)

    # Back
    for i in range(max(0, total - 5), total):
        sample_indices.add(i)

    low_text_pages = 0
    image_pages = 0

    for i in sorted(sample_indices):
        page = doc[i]
        text = page.get_text().strip()
        images = page.get_images(full=True)
        word_count = len(text.split())
        if word_count < 20:
            low_text_pages += 1
        if images:
            image_pages += 1

    doc.close()
    sampled = len(sample_indices)

    if sampled == 0:
        return False

    low_text_ratio = low_text_pages / sampled
    image_ratio = image_pages / sampled

    # If most pages have little text AND contain images → scanned
    is_scanned = low_text_ratio >= 0.5 and image_ratio >= 0.5
    if is_scanned:
        logger.info(
            f"PDF detected as scanned: {low_text_ratio:.0%} low-text, "
            f"{image_ratio:.0%} images (sampled {sampled}/{total} pages)"
        )
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

        # Per-page OCR check — catches hybrid PDFs where only some pages
        # are scanned (e.g. text-based director's report + scanned financials)
        if (scanned or is_page_scanned(page)) and len(text.strip().split()) < 20:
            try:
                # Actual OCR via PyMuPDF's Tesseract integration.
                # full=False means "only OCR image areas where no text exists"
                # so for normal PDFs it does nothing (fast), and for scanned
                # PDFs it runs Tesseract on the whole page image.
                tp = page.get_textpage_ocr(language="eng", dpi=150, full=False)
                ocr_text = page.get_text(textpage=tp)
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
            except Exception as e:
                logger.warning(f"Tesseract OCR unavailable for page {i}: {e}")

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
