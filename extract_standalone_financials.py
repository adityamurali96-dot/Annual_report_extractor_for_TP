"""
CLI script for extracting standalone financials from a PDF annual report.

Pipeline:
  1. Claude API (Sonnet 4.5) identifies standalone financial statement pages
  2. Extract page headers for company validation
  3. Docling extracts tables from only those targeted pages
  4. Write to Excel
"""

import sys
import os

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import ANTHROPIC_API_KEY
from app.pdf_utils import extract_page_headers
from app.docling_extractor import extract_pnl_docling, extract_note_docling
from app.extractor import find_standalone_pages as find_standalone_pages_regex
from app.extractor import find_note_page, compute_metrics
from app.excel_writer import create_excel


def run_extraction(pdf_path: str, output_path: str):
    """Run the full extraction pipeline on a PDF file."""

    # ==================================================================
    # STAGE 1: Identify standalone financial statement pages
    # ==================================================================
    print("STAGE 1: Identify Standalone Financial Pages")

    pages = {}
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    company_name = None

    if ANTHROPIC_API_KEY:
        print("  Using Claude Sonnet 4.5 for page identification...")
        try:
            from app.claude_parser import identify_pages
            page_info = identify_pages(pdf_path)

            raw_pages = page_info.get("pages", {})
            fy_current = page_info.get("fiscal_year_current", fy_current)
            fy_previous = page_info.get("fiscal_year_previous", fy_previous)
            company_name = page_info.get("company_name")

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
        print("  No ANTHROPIC_API_KEY set, falling back to regex")

    if "pnl" not in pages:
        print("  Using regex for page identification...")
        pages, _ = find_standalone_pages_regex(pdf_path)

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
    # STAGE 3: Docling extracts P&L from targeted standalone page(s)
    # ==================================================================
    print(f"\nSTAGE 3: Docling - Extract P&L from standalone page {pages['pnl']}")

    pnl = extract_pnl_docling(pdf_path, pages["pnl"])

    if company_name:
        pnl["company"] = company_name

    print(f"  Company: {pnl['company']}")
    print(f"  Items extracted: {len(pnl['items'])}")
    print(f"  Note refs: {pnl['note_refs']}")
    for k, v in pnl["items"].items():
        print(f"    {k:42s} | CY: {v['current']:>14,.2f} | PY: {v['previous']:>14,.2f}")

    # ==================================================================
    # STAGE 4: Docling extracts note breakup from standalone notes
    # ==================================================================
    print("\nSTAGE 4: Docling - Extract Other Expenses Note")

    note_num = pnl["note_refs"].get("Other expenses")
    note_items = []
    note_total = None
    search_start = pages.get("notes_start", pages["pnl"])

    if note_num:
        print(f"  Note reference for Other expenses: {note_num}")
        note_page, _ = find_note_page(pdf_path, note_num, search_start, "Other expenses")

        if note_page is not None:
            print(f"  Extracting Note {note_num} from page {note_page}...")
            try:
                note_items, note_total = extract_note_docling(pdf_path, note_page, note_num)
                print(f"  Extracted {len(note_items)} note items")
                for ni in note_items:
                    print(f"    {ni['label']:50s} | CY: {ni['current']:>12,.2f} | PY: {ni['previous']:>12,.2f}")
                if note_total:
                    pnl_total = pnl["items"].get("Other expenses", {}).get("current", 0)
                    print(f"\n  Note Total: CY {note_total['current']:,.2f}")
                    print(f"  P&L Other Expenses: {pnl_total:,.2f}")
                    print(f"  Match: {'YES' if abs(note_total['current'] - pnl_total) < 1 else 'NO'}")
            except Exception as e:
                print(f"  Note extraction failed: {e}")
        else:
            print(f"  Could not find Note {note_num} page")
    else:
        print("  No note reference found for Other expenses in P&L table")

    # ==================================================================
    # STAGE 5: Compute metrics & generate Excel
    # ==================================================================
    print("\nSTAGE 5: Generate Excel")

    metrics = compute_metrics(pnl)
    for key in ['Operating Profit (EBIT)', 'EBITDA', 'Profit After Tax',
                'Operating Margin (%)', 'EBITDA Margin (%)', 'PAT Margin (%)']:
        v = metrics['current'][key]
        print(f"    {key:42s} | {v:>10.2f}{'%' if '%' in key else ''}")

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
