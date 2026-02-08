"""
CLI script for extracting standalone financials from a PDF annual report.

Pipeline:
  1. Identify standalone financial statement pages (Claude API primary, fallback to regex)
  2. Extract page headers for company validation
  3. Extract P&L from standalone pages only
  4. Extract note breakup from standalone notes only
  5. Generate Excel with header validation info
"""

import sys
import os

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_page_headers
from app.table_extractor import (
    find_standalone_pages as find_standalone_pages_table,
    extract_pnl_from_tables,
    extract_note_from_tables,
)
from app.extractor import (
    find_standalone_pages as find_standalone_pages_regex,
    extract_pnl_regex,
    find_note_page,
    extract_note_breakup,
    compute_metrics,
)
from app.excel_writer import create_excel


def run_extraction(pdf_path: str, output_path: str):
    """Run the full extraction pipeline on a PDF file."""

    # ==================================================================
    # STAGE 1: Identify standalone financial statement pages
    # Claude API is the primary method for page identification.
    # ==================================================================
    print("STAGE 1: Identify Standalone Financial Pages")

    pages = {}
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    company_name = None
    page_headers_from_claude = {}

    if ANTHROPIC_API_KEY:
        print("  Using Claude API for page identification...")
        try:
            from app.claude_parser import identify_pages
            page_info = identify_pages(pdf_path)

            raw_pages = page_info.get("pages", {})
            fy_current = page_info.get("fiscal_year_current", fy_current)
            fy_previous = page_info.get("fiscal_year_previous", fy_previous)
            company_name = page_info.get("company_name")
            page_headers_from_claude = page_info.get("page_headers", {})

            if raw_pages.get("pnl") is not None:
                pages["pnl"] = raw_pages["pnl"]
            if raw_pages.get("balance_sheet") is not None:
                pages["bs"] = raw_pages["balance_sheet"]
            if raw_pages.get("cash_flow") is not None:
                pages["cf"] = raw_pages["cash_flow"]
            if raw_pages.get("notes_start") is not None:
                pages["notes_start"] = raw_pages["notes_start"]

            print(f"  Claude identified pages: {pages}")
            print(f"  Company: {company_name}")
            print(f"  FY: {fy_current} / {fy_previous}")
        except Exception as e:
            print(f"  Claude failed: {e}")
    else:
        print("  No ANTHROPIC_API_KEY set, skipping Claude API")

    # Fallback: pymupdf4llm then regex
    if "pnl" not in pages:
        print("  Trying pymupdf4llm for page identification...")
        try:
            pages, total = find_standalone_pages_table(pdf_path)
            if "pnl" in pages:
                print(f"  pymupdf4llm found pages: {pages}")
        except Exception as e:
            print(f"  pymupdf4llm failed: {e}")

    if "pnl" not in pages:
        print("  Trying regex for page identification...")
        pages, total = find_standalone_pages_regex(pdf_path)

    if "pnl" not in pages:
        print("ERROR: Could not find Standalone P&L page.")
        sys.exit(1)

    print(f"  Standalone pages: {pages}")

    # ==================================================================
    # STAGE 2: Extract page headers for validation
    # ==================================================================
    print("\nSTAGE 2: Extract Page Headers (for company validation)")

    page_headers = extract_page_headers(pdf_path, pages)
    for section, header in page_headers.items():
        print(f"  [{section}] {header[:80]}...")

    # ==================================================================
    # STAGE 3: Extract P&L from standalone page only
    # ==================================================================
    print(f"\nSTAGE 3: Extract P&L from standalone page {pages['pnl']}")

    pnl = None
    # Primary: pymupdf4llm
    try:
        pnl = extract_pnl_from_tables(pdf_path, pages["pnl"])
        if len(pnl.get("items", {})) >= 5:
            print(f"  pymupdf4llm extracted {len(pnl['items'])} P&L items")
        else:
            print(f"  pymupdf4llm only got {len(pnl.get('items', {}))} items, trying regex...")
            pnl = None
    except Exception as e:
        print(f"  pymupdf4llm P&L extraction failed: {e}, trying regex...")

    # Fallback: regex
    if pnl is None:
        pnl = extract_pnl_regex(pdf_path, pages["pnl"])
        print(f"  Regex extracted {len(pnl['items'])} P&L items")

    # Override company name if Claude detected it
    if company_name:
        pnl["company"] = company_name

    print(f"  Company: {pnl['company']}")
    print(f"  Note refs: {pnl['note_refs']}")
    for k, v in pnl["items"].items():
        print(f"    {k:42s} | CY: {v['current']:>14,.2f} | PY: {v['previous']:>14,.2f}")

    # ==================================================================
    # STAGE 4: Extract note breakup from standalone notes only
    # ==================================================================
    print("\nSTAGE 4: Find & Extract Other Expenses Note (standalone only)")

    note_num = pnl["note_refs"].get("Other expenses")
    note_items = []
    note_total = None

    if note_num:
        print(f"  Other Expenses note ref from P&L: {note_num}")
        search_start = pages.get("notes_start", pages["pnl"])
        note_page, note_line = find_note_page(pdf_path, note_num, search_start, "Other expenses")

        if note_page is not None:
            print(f"  Found Note {note_num} on PDF page {note_page + 1} (0-idx: {note_page})")

            # Primary: pymupdf4llm
            try:
                note_items, note_total = extract_note_from_tables(pdf_path, note_page, str(note_num))
                if note_items:
                    print(f"  pymupdf4llm extracted {len(note_items)} note items")
                else:
                    raise ValueError("No items extracted")
            except Exception as e:
                print(f"  pymupdf4llm note extraction failed: {e}")
                if note_line is not None:
                    note_items, note_total = extract_note_breakup(pdf_path, note_page, note_line, note_num)
                    print(f"  Regex extracted {len(note_items)} note items")

            for ni in note_items:
                print(f"    {ni['label']:50s} | CY: {ni['current']:>12,.2f} | PY: {ni['previous']:>12,.2f}")

            if note_total:
                print(f"\n  Total: CY {note_total['current']:,.2f} | PY {note_total['previous']:,.2f}")
                pnl_total = pnl["items"].get("Other expenses", {}).get("current", 0)
                print(f"  P&L Other Expenses: {pnl_total:,.2f}")
                print(f"  Match: {'YES' if abs(note_total['current'] - pnl_total) < 1 else 'NO'}")
        else:
            print(f"  Could not find Note {note_num} page")
    else:
        print("  No note reference found for Other expenses")

    # ==================================================================
    # STAGE 5: Compute metrics
    # ==================================================================
    print("\nSTAGE 5: Compute Metrics")
    metrics = compute_metrics(pnl)
    for key in ['Revenue from Operations', 'Operating Profit (EBIT)', 'EBITDA',
                'Profit After Tax', 'Operating Margin (%)', 'EBITDA Margin (%)', 'PAT Margin (%)']:
        v = metrics['current'][key]
        print(f"    {key:42s} | {v:>10.2f}{'%' if '%' in key else ''}")

    # ==================================================================
    # STAGE 6: Generate Excel with header validation
    # ==================================================================
    print("\nSTAGE 6: Excel Output")

    data = {
        "company": pnl["company"],
        "currency": pnl["currency"],
        "fy_current": fy_current,
        "fy_previous": fy_previous,
        "pages": pages,
        "pnl": pnl,
        "note_items": note_items,
        "note_total": note_total,
        "note_number": note_num,
        "page_headers": page_headers,
    }

    create_excel(data, output_path)
    print(f"  Saved: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_standalone_financials.py <pdf_path> [output_path]")
        print("  pdf_path:    Path to the annual report PDF")
        print("  output_path: (optional) Path for output Excel file")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else pdf_path.rsplit(".", 1)[0] + "_financials.xlsx"

    if not os.path.exists(pdf_path):
        print(f"ERROR: File not found: {pdf_path}")
        sys.exit(1)

    if not pdf_path.lower().endswith(".pdf"):
        print("ERROR: Only PDF files are accepted")
        sys.exit(1)

    run_extraction(pdf_path, output_path)
