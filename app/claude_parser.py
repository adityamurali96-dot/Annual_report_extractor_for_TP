"""
Claude API integration for optional TOC verification.

When an API key is configured, uses Claude to identify which pages contain
standalone financial statements (better accuracy for non-standard layouts).
All data extraction (P&L parsing, note breakup) is done via regex only.
"""

import json
import logging

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_pdf_text

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-20250514"

try:
    import anthropic
except ImportError:
    anthropic = None


def _get_client():
    if anthropic is None:
        raise ImportError(
            "The 'anthropic' package is required for TOC verification. "
            "Install it with: pip install anthropic"
        )
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# -------------------------------------------------------------------
# Identify standalone financial statement pages (TOC verification)
# -------------------------------------------------------------------

IDENTIFY_PAGES_PROMPT = """You are a financial document analyst. I will give you text extracted from pages of an annual report PDF. Your job is to identify which pages contain the STANDALONE financial statements.

Look for these sections:
1. **Statement of Profit and Loss** (Standalone) - the P&L statement
2. **Balance Sheet** (Standalone) - assets and liabilities
3. **Cash Flow Statement** (Standalone) - cash flows
4. **Notes to Financial Statements** - the starting page of standalone notes

Important distinctions:
- Look for "Standalone" specifically - ignore "Consolidated" statements
- The P&L may be titled "Statement of Profit and Loss" or similar
- Notes usually start after the cash flow statement

Also detect:
- The **company name** (look for "XYZ Limited" or "XYZ Ltd" in headers)
- The **fiscal years** being reported (e.g., "FY 2024-25" and "FY 2023-24", or "March 31, 2025" and "March 31, 2024")
- The **currency unit** (e.g., "INR Million", "Rs. in Crores", "Rs. in Lakhs")

Respond with ONLY a JSON object (no markdown, no explanation):
{
    "company_name": "XYZ Limited",
    "currency": "INR Million",
    "fiscal_year_current": "FY 2024-25",
    "fiscal_year_previous": "FY 2023-24",
    "pages": {
        "pnl": <page_number or null>,
        "balance_sheet": <page_number or null>,
        "cash_flow": <page_number or null>,
        "notes_start": <page_number or null>
    }
}

Use 0-indexed page numbers as provided in the text headers."""


def identify_pages(pdf_path: str) -> dict:
    """Use Claude to identify standalone financial statement pages (TOC verification)."""
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
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Claude response for page identification: {text[:200]}")
            continue

    return results
