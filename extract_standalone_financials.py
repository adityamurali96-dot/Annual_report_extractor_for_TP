"""
CLI script for extracting standalone financials from PDF annual reports.

Supports processing up to 2 PDF files sequentially (queued).

Pipeline (per file):
  1. Identify standalone financial statement pages (Claude API / regex)
  2. Warn if multiple standalone P&L pages detected
  3. Extract page headers for company validation
  4. Docling extracts tables from only those targeted pages
  5. Write to Excel
"""

import os
import sys

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import ANTHROPIC_API_KEY
from app.docling_extractor import extract_note_docling, extract_pnl_docling
from app.excel_writer import create_excel
from app.extractor import (
    _is_likely_toc_page,
    compute_metrics,
    compute_pnl_confidence,
    find_all_standalone_candidates,
    find_note_page,
    validate_note_extraction,
)
from app.extractor import find_standalone_pages as find_standalone_pages_regex
from app.pdf_utils import extract_page_headers


def run_extraction(pdf_path: str, output_path: str):
    """Run the full extraction pipeline on a PDF file."""

    warnings: list[str] = []

    # ==================================================================
    # STAGE 1: Identify standalone financial statement pages
    # ==================================================================
    print("STAGE 1: Identify Financial Statement Pages")

    pages = {}
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    company_name = None
    claude_identified = False

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
                claude_identified = True
            if raw_pages.get("balance_sheet") is not None:
                pages["bs"] = raw_pages["balance_sheet"]
            if raw_pages.get("cash_flow") is not None:
                pages["cf"] = raw_pages["cash_flow"]
            if raw_pages.get("notes_start") is not None:
                pages["notes_start"] = raw_pages["notes_start"]

            for key, idx in list(pages.items()):
                if idx is None:
                    continue
                page_text = extract_page_headers(pdf_path, {key: idx}, num_lines=40).get(key, "")
                if page_text and _is_likely_toc_page(page_text):
                    print(f"  Ignoring Claude {key} page {idx + 1}: likely TOC page")
                    pages.pop(key, None)
                    if key == "pnl":
                        claude_identified = False

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
        print("ERROR: Could not find a P&L (Statement of Profit and Loss) page.")
        sys.exit(1)

    print(f"  Financial statement pages: {pages}")

    # ==================================================================
    # STAGE 1b: Check for multiple standalone P&L candidates and warn
    # ==================================================================
    candidates = find_all_standalone_candidates(pdf_path)
    pnl_candidates = candidates.get("pnl", [])

    if pages["pnl"] not in pnl_candidates:
        pnl_candidates.insert(0, pages["pnl"])

    confidence = compute_pnl_confidence(len(pnl_candidates), claude_identified)
    print(f"  P&L confidence: {confidence:.0%} ({len(pnl_candidates)} candidate(s), "
          f"claude={'yes' if claude_identified else 'no'})")

    if len(pnl_candidates) > 1:
        candidate_pages_display = ", ".join(str(p + 1) for p in pnl_candidates)
        warn_msg = (
            f"WARNING: Multiple P&L pages detected (PDF pages: {candidate_pages_display}). "
            f"Extracting from page {pages['pnl'] + 1}. "
            f"Please verify the totals in the output Excel to ensure the correct page was used."
        )
        warnings.append(warn_msg)
        print(f"\n  ** {warn_msg} **\n")

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
    print(f"\nSTAGE 3: Docling - Extract P&L from page {pages['pnl']}")

    pnl = extract_pnl_docling(pdf_path, pages["pnl"])

    if company_name:
        pnl["company"] = company_name

    print(f"  Company: {pnl['company']}")
    print(f"  Items extracted: {len(pnl['items'])}")
    print(f"  Note refs: {pnl['note_refs']}")
    for k, v in pnl["items"].items():
        print(f"    {k:42s} | CY: {v['current']:>14,.2f} | PY: {v['previous']:>14,.2f}")

    # ==================================================================
    # STAGE 4: Docling extracts note breakup from notes section
    # ==================================================================
    oe_label = pnl.get("operating_expense_label", "Other expenses")
    print(f"\nSTAGE 4: Docling - Extract {oe_label} Note")

    note_num = pnl["note_refs"].get("Other expenses")
    note_items = []
    note_total = None
    search_start = pages.get("notes_start", pages["pnl"])

    if note_num:
        print(f"  Note reference for {oe_label}: {note_num}")
        note_page, _ = find_note_page(pdf_path, note_num, search_start, oe_label)

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
        if not check["ok"]:
            warnings.append(
                f"Validation FAIL: {check['name']} "
                f"(got {check['actual']:.2f}, expected {check['expected']:.2f})"
            )

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
        "warnings": warnings,
    }

    create_excel(data, output_path)
    print(f"  Saved: {output_path}")

    # ==================================================================
    # Print warnings summary
    # ==================================================================
    if warnings:
        print(f"\n{'='*70}")
        print(f"  REVIEW REQUIRED - {len(warnings)} warning(s):")
        print(f"{'='*70}")
        for w in warnings:
            print(f"  - {w}")
        print(f"{'='*70}")
        print("  Please check the Validation sheet in the Excel output.")

    return warnings


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_standalone_financials.py <pdf_path> [pdf_path_2] [output_dir]")
        print("  pdf_path:    Path to the annual report PDF (up to 2 files)")
        print("  output_dir:  (optional) Directory for output Excel files")
        sys.exit(1)

    # Collect PDF paths and optional output directory
    pdf_paths = []
    output_dir = None
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".pdf"):
            pdf_paths.append(arg)
        elif os.path.isdir(arg):
            output_dir = arg
        else:
            # Treat as output path for single-file mode (backward compat)
            output_dir = arg

    if not pdf_paths:
        print("ERROR: No PDF files provided")
        sys.exit(1)

    if len(pdf_paths) > 2:
        print("ERROR: Maximum 2 PDF files supported")
        sys.exit(1)

    for pdf_path in pdf_paths:
        if not os.path.exists(pdf_path):
            print(f"ERROR: File not found: {pdf_path}")
            sys.exit(1)

    # Process each PDF
    all_warnings = []
    for i, pdf_path in enumerate(pdf_paths):
        if len(pdf_paths) > 1:
            print(f"\n{'='*70}")
            print(f"  REPORT {i + 1} of {len(pdf_paths)}: {os.path.basename(pdf_path)}")
            print(f"{'='*70}\n")

        if output_dir and os.path.isdir(output_dir):
            base = os.path.splitext(os.path.basename(pdf_path))[0]
            out_path = os.path.join(output_dir, base + "_financials.xlsx")
        elif output_dir and len(pdf_paths) == 1:
            out_path = output_dir
        else:
            out_path = pdf_path.rsplit(".", 1)[0] + "_financials.xlsx"

        file_warnings = run_extraction(pdf_path, out_path)
        all_warnings.extend(file_warnings)

    if len(pdf_paths) > 1:
        print(f"\nAll {len(pdf_paths)} reports processed.")
        if all_warnings:
            print(f"Total warnings across all reports: {len(all_warnings)}")
