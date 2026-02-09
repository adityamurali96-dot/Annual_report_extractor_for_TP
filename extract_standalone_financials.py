"""
CLI script for extracting standalone financials from a PDF annual report.

Pipeline:
  1. Claude API (Sonnet 4.5) identifies standalone financial statement pages
  2. Extract page headers for company validation
  3. Docling extracts tables from only those targeted pages
  4. Write to Excel
"""

import os
import sys

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import ANTHROPIC_API_KEY
from app.docling_extractor import extract_note_docling, extract_pnl_docling
from app.excel_writer import create_excel
from app.extractor import (
    compute_metrics,
    find_all_standalone_candidates,
    find_note_page,
    validate_note_extraction,
)
from app.extractor import find_standalone_pages as find_standalone_pages_regex
from app.pdf_utils import extract_page_headers

SECTION_LABELS = {
    "pnl": "Statement of Profit & Loss",
    "bs": "Balance Sheet",
    "cf": "Cash Flow Statement",
}


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
    # STAGE 1b: Check for multiple standalone candidates & confirm
    # ==================================================================
    candidates = find_all_standalone_candidates(pdf_path)
    has_multiple = any(len(v) > 1 for v in candidates.values())

    # Ensure recommended pages are in the candidate lists
    for section in ["pnl", "bs", "cf"]:
        if section in pages and pages[section] not in candidates.get(section, []):
            candidates.setdefault(section, []).insert(0, pages[section])

    if has_multiple:
        print("\n  ** Multiple standalone financial pages detected **")
        print("  Please confirm the correct page for each section.\n")

        # Show candidates with headers for each section
        candidate_page_map = {}
        for section, page_list in candidates.items():
            for pg in page_list:
                candidate_page_map[f"{section}_p{pg}"] = pg
        candidate_headers = extract_page_headers(pdf_path, candidate_page_map, num_lines=5)

        for section in ["pnl", "bs", "cf"]:
            page_list = candidates.get(section, [])
            if not page_list:
                continue

            label = SECTION_LABELS.get(section, section)
            recommended = pages.get(section)

            print(f"  --- {label} ---")
            for idx, pg in enumerate(page_list):
                rec_tag = " [RECOMMENDED]" if pg == recommended else ""
                header = candidate_headers.get(f"{section}_p{pg}", "(no header text)")
                header_preview = header.replace('\n', ' | ')[:100]
                print(f"    [{idx + 1}] PDF Page {pg + 1}{rec_tag}")
                print(f"        Header: {header_preview}")
            print()

            if len(page_list) > 1:
                while True:
                    default_idx = page_list.index(recommended) + 1 if recommended in page_list else 1
                    choice = input(f"  Select {label} page [1-{len(page_list)}] "
                                   f"(default={default_idx}): ").strip()
                    if choice == "":
                        chosen_idx = default_idx - 1
                        break
                    try:
                        chosen_idx = int(choice) - 1
                        if 0 <= chosen_idx < len(page_list):
                            break
                        print(f"    Invalid choice. Enter 1-{len(page_list)}.")
                    except ValueError:
                        print(f"    Invalid input. Enter a number 1-{len(page_list)}.")

                pages[section] = page_list[chosen_idx]
                print(f"  -> Selected PDF Page {pages[section] + 1}\n")

        print(f"  Confirmed pages: {pages}")
    else:
        print("  Single candidate per section - no confirmation needed.")

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
    # STAGE 4b: Validate note extraction against P&L
    # ==================================================================
    print("\nSTAGE 4b: Validate Note Extraction")

    validation = validate_note_extraction(pnl, note_items, note_total, note_num)
    for check in validation:
        status = "PASS" if check["ok"] else "FAIL"
        print(f"  [{status:4s}] {check['name']:55s} | "
              f"Got: {check['actual']:>14,.2f}  Expected: {check['expected']:>14,.2f}")

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
        "note_validation": validation,
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
