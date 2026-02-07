"""
Claude API integration for intelligent PDF section extraction.

Uses Claude to:
1. Identify which pages contain standalone financial statements
2. Extract structured P&L data from PDF text
3. Extract note breakup details
4. Detect fiscal years dynamically
"""

import json
import logging
import anthropic

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_pdf_text, extract_pages_range

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-20250514"


def _get_client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# -------------------------------------------------------------------
# 1. Identify standalone financial statement pages
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
    """Use Claude to identify standalone financial statement pages."""
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


# -------------------------------------------------------------------
# 2. Extract P&L using Claude
# -------------------------------------------------------------------

EXTRACT_PNL_PROMPT = """You are a financial data extraction expert. I will give you the text of a Standalone Statement of Profit and Loss from an annual report.

Extract ALL line items with their values for both the current year (CY) and previous year (PY).

Return ONLY a JSON object with this structure (no markdown, no explanation):
{
    "items": {
        "Revenue from operations": {"current": 12345.67, "previous": 11000.00},
        "Other income": {"current": 500.00, "previous": 400.00},
        "Total income": {"current": 12845.67, "previous": 11400.00},
        "Employee benefits expense": {"current": 5000.00, "previous": 4500.00},
        "Cost of professionals": {"current": 0, "previous": 0},
        "Finance costs": {"current": 100.00, "previous": 90.00},
        "Depreciation and amortisation": {"current": 300.00, "previous": 250.00},
        "Other expenses": {"current": 2000.00, "previous": 1800.00},
        "Total expenses": {"current": 7400.00, "previous": 6640.00},
        "Profit before tax": {"current": 5445.67, "previous": 4760.00},
        "Current tax": {"current": 1200.00, "previous": 1000.00},
        "Deferred tax": {"current": -50.00, "previous": -30.00},
        "Total tax expense": {"current": 1150.00, "previous": 970.00},
        "Profit for the year": {"current": 4295.67, "previous": 3790.00},
        "Total comprehensive income": {"current": 4300.00, "previous": 3800.00},
        "Basic EPS": {"current": 45.67, "previous": 40.23},
        "Diluted EPS": {"current": 45.50, "previous": 40.10}
    },
    "note_refs": {
        "Revenue from operations": "23",
        "Other income": "24",
        "Other expenses": "27"
    }
}

Rules:
- Use negative numbers for expenses that reduce profit (like negative deferred tax)
- Values in parentheses like (123.45) mean negative: -123.45
- Include note reference numbers where visible (small numbers like 23, 24, 27 next to line items)
- If a line item is not found, use 0 for both current and previous
- Parse ALL numbers accurately - remove commas, handle decimals
- If "Cost of professionals" is not a separate line item, set it to 0
- Map line items to the standard names shown above as closely as possible"""


def extract_pnl_claude(pdf_path: str, pnl_page: int) -> dict:
    """Use Claude to extract structured P&L data from the identified page."""
    client = _get_client()

    # Get the P&L page and the next page (P&L sometimes spans 2 pages)
    pages = extract_pages_range(pdf_path, pnl_page, pnl_page + 2)
    page_text = '\n\n'.join([
        f"=== PAGE {p['page']} ===\n{p['text']}" for p in pages
    ])

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "user", "content": f"{EXTRACT_PNL_PROMPT}\n\nHere is the P&L text:\n\n{page_text}"}
        ],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse Claude P&L extraction: {text[:500]}")
        return {"items": {}, "note_refs": {}}


# -------------------------------------------------------------------
# 3. Extract Note Breakup using Claude
# -------------------------------------------------------------------

EXTRACT_NOTE_PROMPT = """You are a financial data extraction expert. I will give you text from an annual report that contains a note breakup (detailed breakdown of a financial line item).

The note is: Note {note_number} - {note_title}

Extract all line items from this note with their current year (CY) and previous year (PY) values.

Return ONLY a JSON object (no markdown, no explanation):
{{
    "items": [
        {{"label": "Item name", "current": 1234.56, "previous": 1100.00}},
        {{"label": "Another item", "current": 567.89, "previous": 500.00}}
    ],
    "total": {{"label": "Total", "current": 1802.45, "previous": 1600.00}}
}}

Rules:
- Include ALL line items in the note
- For sub-items, prefix with parent category: "Parent - Sub item"
- Values in parentheses like (123.45) mean negative: -123.45
- Dashes (-) mean 0
- The total should be the last/summary row of the note
- Parse ALL numbers accurately"""


def extract_note_claude(pdf_path: str, note_page: int, note_number: str,
                        note_title: str = "Other expenses") -> tuple[list[dict], dict | None]:
    """Use Claude to extract note breakup details."""
    client = _get_client()

    # Get the note page and the next page (notes may span pages)
    pages = extract_pages_range(pdf_path, note_page, note_page + 2)
    page_text = '\n\n'.join([
        f"=== PAGE {p['page']} ===\n{p['text']}" for p in pages
    ])

    prompt = EXTRACT_NOTE_PROMPT.format(note_number=note_number, note_title=note_title)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "user", "content": f"{prompt}\n\nHere is the note text:\n\n{page_text}"}
        ],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

    try:
        data = json.loads(text)
        items = data.get("items", [])
        total = data.get("total")
        return items, total
    except json.JSONDecodeError:
        logger.error(f"Failed to parse Claude note extraction: {text[:500]}")
        return [], None


# -------------------------------------------------------------------
# 4. Full extraction pipeline using Claude
# -------------------------------------------------------------------

def extract_with_claude(pdf_path: str) -> dict:
    """
    Full extraction pipeline using Claude API.
    Returns a dict with all extracted data ready for Excel generation.
    """
    logger.info("Step 1: Identifying pages with Claude...")
    page_info = identify_pages(pdf_path)

    company = page_info.get("company_name", "Unknown Company")
    currency = page_info.get("currency", "INR Million")
    fy_current = page_info.get("fiscal_year_current", "Current Year")
    fy_previous = page_info.get("fiscal_year_previous", "Previous Year")
    pages = page_info.get("pages", {})

    pnl_page = pages.get("pnl")
    if pnl_page is None:
        raise ValueError("Could not find Standalone P&L page in the PDF")

    logger.info(f"Step 2: Extracting P&L from page {pnl_page}...")
    pnl_data = extract_pnl_claude(pdf_path, pnl_page)
    pnl_data["company"] = company
    pnl_data["currency"] = currency

    # Extract note breakup for Other Expenses
    note_items = []
    note_total = None
    note_num = pnl_data.get("note_refs", {}).get("Other expenses")

    if note_num:
        notes_start = pages.get("notes_start", pnl_page)
        logger.info(f"Step 3: Finding Note {note_num} (Other expenses)...")

        # Use regex finder as it's reliable for locating the exact page
        from app.extractor import find_note_page
        note_page, note_line = find_note_page(pdf_path, note_num, notes_start, "Other expenses")

        if note_page is not None:
            logger.info(f"Step 4: Extracting Note {note_num} from page {note_page}...")
            note_items, note_total = extract_note_claude(
                pdf_path, note_page, note_num, "Other expenses"
            )

    return {
        "company": company,
        "currency": currency,
        "fy_current": fy_current,
        "fy_previous": fy_previous,
        "pages": pages,
        "pnl": pnl_data,
        "note_items": note_items,
        "note_total": note_total,
        "note_number": note_num,
    }
