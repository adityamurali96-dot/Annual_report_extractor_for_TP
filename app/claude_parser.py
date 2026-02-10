"""
Claude API integration for standalone financial statement page identification.

Uses Claude to identify which PDF pages contain the STANDALONE financial
statements (not consolidated). Also extracts page headers for company
validation so the user can verify the correct entity is being processed.

All data extraction (P&L parsing, note breakup) is done via pymupdf4llm/regex.
"""

import json
import logging

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_pdf_text

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

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
   - If you see pages with headers containing "Consolidated" (e.g., "Consolidated Statement of Profit and Loss"), this is a multi-section report. Find the pages labelled "Standalone" instead.
   - If NO page mentions "Consolidated" at all, this is a single-entity report. The financial statements you find are the ones to use.

3. The standalone section usually appears BEFORE the consolidated section in Indian annual reports, but not always.

Look for these sections:
1. **Statement of Profit and Loss** - the P&L statement
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

    # Build a condensed view - first 15 lines of each page to save tokens
    page_summaries = []
    for p in all_pages:
        lines = p["text"].split('\n')[:15]
        summary = '\n'.join(lines)
        page_summaries.append(f"=== PAGE {p['page']} ===\n{summary}")

    # Send in batches if too many pages
    batch_size = 80
    results = {}

    for batch_start in range(0, len(page_summaries), batch_size):
        batch = page_summaries[batch_start:batch_start + batch_size]
        content = '\n\n'.join(batch)

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
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
