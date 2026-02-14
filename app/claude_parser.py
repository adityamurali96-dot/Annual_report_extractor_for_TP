"""
Claude API integration for standalone financial statement page identification.

Uses Claude to identify which PDF pages contain the STANDALONE financial
statements (not consolidated). Also extracts page headers for company
validation so the user can verify the correct entity is being processed.

All data extraction (P&L parsing, note breakup) is done via pymupdf4llm/regex.
"""

import json
import logging
import os

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_pdf_text

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

try:
    import anthropic
except ImportError:
    anthropic = None


def _get_client():
    if anthropic is None:
        raise ImportError(
            "The 'anthropic' package is required for page identification. "
            "Install it with: pip install anthropic"
        )
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# -------------------------------------------------------------------
# Identify standalone financial statement pages
# -------------------------------------------------------------------

IDENTIFY_PAGES_PROMPT = """You are a financial document analyst. I will give you text extracted from pages of an Indian annual report PDF. Your job is to identify which pages contain the financial statements to extract.

CRITICAL RULES:
1. **Two types of annual reports exist:**
   - **Multi-section reports** contain BOTH "Standalone" and "Consolidated" financial statements. In these reports, you MUST find only the STANDALONE statements. Ignore ALL pages labelled "Consolidated".
   - **Single-entity reports** have only ONE set of financial statements (no "Standalone" or "Consolidated" labels). In these reports, identify the financial statements directly — they are implicitly standalone.

2. **How to distinguish:**
   - If you see pages with headers containing "Consolidated" (e.g., "Consolidated Statement of Profit and Loss"), this is a multi-section report. Find the pages labelled "Standalone" or "Separate" instead.
   - If NO page mentions "Consolidated" at all, this is a single-entity report. The financial statements you find are the ones to use.

3. The standalone section usually appears BEFORE the consolidated section in Indian annual reports, but not always.

4. **IMPORTANT — Recognise P&L title variations.** The P&L statement can have many different titles across reports:
   - "Statement of Profit and Loss"
   - "Profit and Loss Account"
   - "Profit and Loss Statement"
   - "Statement of Profit or Loss" (IFRS)
   - "Statement of Income and Expenses"
   - "Income and Expenditure Account" (non-profit entities)
   - "Statement of Operations"
   - "Statement of Comprehensive Income"
   - Sometimes the title is split across lines or OCR-damaged (e.g., "Staternent of Profit and Loss")
   - The key signals are the CONTENT: look for pages with "Revenue from operations", "Profit before tax", "Total income", "Total expenses", "Earnings per share"

5. **IMPORTANT — Handle scanned/OCR PDFs.** Text from scanned PDFs may be garbled, have extra spaces, misspelled words, or broken formatting. Look at the overall structure and keywords rather than exact title matches. If the extracted text is mostly empty or garbled, use whatever signals are available.

6. **IMPORTANT — No table of contents / index.** Some PDFs have no table of contents. Search through ALL pages for the actual financial statements, not just pages referenced from an index.

7. **IMPORTANT — Labels.** Some reports use "Separate" instead of "Standalone". Treat "Separate Financial Statements" the same as "Standalone Financial Statements".

Look for these sections:
1. **Statement of Profit and Loss** (see title variations above) - the P&L statement
2. **Balance Sheet** - assets and liabilities
3. **Cash Flow Statement** - cash flows
4. **Notes to Financial Statements** - the starting page of notes

Also extract:
- The **company name** exactly as it appears in the financial statement headers (e.g., "XYZ Limited", "ABC Corp Ltd")
- The **page header text** for each identified page - this is the full text of the first 3-4 lines at the top of each page (for validation)
- The **fiscal years** being reported (e.g., "FY 2024-25" and "FY 2023-24", or "March 31, 2025" and "March 31, 2024")
- The **currency unit** (e.g., "INR Million", "Rs. in Crores", "Rs. in Lakhs")
- Whether this is a **single-entity report** (no consolidated section found) — set "report_type" to "single_entity" or "multi_section"

Respond with ONLY a JSON object (no markdown, no explanation):
{
    "company_name": "XYZ Limited",
    "currency": "INR Million",
    "fiscal_year_current": "FY 2024-25",
    "fiscal_year_previous": "FY 2023-24",
    "report_type": "single_entity or multi_section",
    "pages": {
        "pnl": <page_number or null>,
        "balance_sheet": <page_number or null>,
        "cash_flow": <page_number or null>,
        "notes_start": <page_number or null>
    },
    "page_headers": {
        "pnl": "Full header text from top of P&L page (first 3-4 lines)",
        "balance_sheet": "Full header text from top of Balance Sheet page",
        "cash_flow": "Full header text from top of Cash Flow page",
        "notes_start": "Full header text from top of Notes page"
    }
}

Use 0-indexed page numbers as provided in the text headers."""


def identify_pages(pdf_path: str) -> dict:
    """
    Use Claude to identify standalone financial statement pages.

    Returns dict with keys:
        - company_name: str
        - currency: str
        - fiscal_year_current: str
        - fiscal_year_previous: str
        - pages: dict with pnl, balance_sheet, cash_flow, notes_start (0-indexed page numbers)
        - page_headers: dict with header text from each identified page (for validation)
    """
    client = _get_client()
    all_pages = extract_pdf_text(pdf_path)

    # Build a condensed view of each page.
    # Take 25 meaningful lines from the first 40 raw lines to skip
    # blank lines from headers/logos common in OCR'd documents.
    page_summaries = []
    empty_page_count = 0

    for p in all_pages:
        raw_lines = p["text"].split('\n')
        non_empty = [l for l in raw_lines[:40] if l.strip()][:25]
        summary = '\n'.join(non_empty)
        if summary.strip():
            page_summaries.append(f"=== PAGE {p['page']} ===\n{summary}")
        else:
            empty_page_count += 1

    # If most pages are empty, the PDF is likely scanned without OCR
    if empty_page_count > len(all_pages) * 0.8:
        logger.error(
            f"[identify_pages] {empty_page_count}/{len(all_pages)} pages "
            f"have no extractable text. PDF may be scanned without OCR."
        )
        raise ValueError(
            f"Cannot identify financial statement pages: "
            f"{empty_page_count} of {len(all_pages)} pages have no readable text. "
            f"This PDF appears to be scanned/image-based. "
            f"Please ensure Tesseract OCR is installed, or use a text-based PDF."
        )

    # Send in batches if too many pages
    batch_size = 80
    results = {}

    for batch_start in range(0, len(page_summaries), batch_size):
        batch = page_summaries[batch_start:batch_start + batch_size]
        content = '\n\n'.join(batch)

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            timeout=30.0,
            messages=[
                {"role": "user", "content": f"{IDENTIFY_PAGES_PROMPT}\n\nHere are the page summaries:\n\n{content}"}
            ],
        )
        text = response.content[0].text.strip()
        # Parse JSON - handle possible markdown wrapping
        if text.startswith("```"):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

        try:
            batch_result = json.loads(text)
            # Merge results, preferring non-null values
            if not results:
                results = batch_result
            else:
                for key in ['pnl', 'balance_sheet', 'cash_flow', 'notes_start']:
                    if batch_result.get('pages', {}).get(key) is not None:
                        results.setdefault('pages', {})[key] = batch_result['pages'][key]
                    if batch_result.get('page_headers', {}).get(key):
                        results.setdefault('page_headers', {})[key] = batch_result['page_headers'][key]
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Claude response for page identification: {text[:200]}")
            continue

    return results
