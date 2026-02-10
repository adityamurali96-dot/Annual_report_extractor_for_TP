"""
FastAPI application for Annual Report Financial Extractor.

Pipeline (PDF-only, supports batch of up to 2 reports):
  1. Accept 1 or 2 PDF uploads
  2. Identify standalone financial statement pages (Claude API / regex)
  3. Extract tables from targeted pages (auto-proceeds, no confirmation)
  4. Return Excel file(s) with warnings if multiple standalone sections detected
"""

import logging
import uuid
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ANTHROPIC_API_KEY, MAX_UPLOAD_SIZE_MB, UPLOAD_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Annual Report Extractor",
    description="Extract standalone financials from annual report PDFs into Excel",
    version="5.1.0",
)

# Static files & templates
BASE_DIR = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the upload page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "has_api_key": bool(ANTHROPIC_API_KEY),
    })


@app.get("/health")
async def health():
    """Health check for Railway."""
    return {"status": "ok", "api_key_configured": bool(ANTHROPIC_API_KEY)}


@app.post("/extract")
async def extract(files: List[UploadFile] = File(...)):
    """
    Upload 1 or 2 PDF annual reports and extract standalone financials.

    Files are processed sequentially (queued). Each produces an Excel file.
    Returns JSON with download links and any warnings (e.g. multiple
    standalone P&L pages detected).
    """
    if not files or len(files) > 2:
        raise HTTPException(400, "Please upload 1 or 2 PDF files")

    results = []

    for file in files:
        if not file.filename:
            raise HTTPException(400, "No file provided")

        ext = Path(file.filename).suffix.lower()
        if ext != ".pdf":
            raise HTTPException(400, f"Only PDF files are accepted, got '{ext}' for {file.filename}")

        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_SIZE_MB:
            raise HTTPException(400, f"File too large ({size_mb:.1f}MB). Max is {MAX_UPLOAD_SIZE_MB}MB")

        job_id = str(uuid.uuid4())[:8]
        pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
        pdf_path.write_bytes(content)

        logger.info(f"[{job_id}] Received {file.filename} ({size_mb:.1f}MB)")

        try:
            result = _run_extraction(str(pdf_path), job_id)
            excel_path = result["excel_path"]
            download_name = f"{Path(file.filename).stem}_financials.xlsx"

            results.append({
                "job_id": job_id,
                "original_filename": file.filename,
                "download_name": download_name,
                "excel_path": excel_path,
                "warnings": result.get("warnings", []),
            })
        except Exception as e:
            logger.exception(f"[{job_id}] Extraction failed for {file.filename}")
            if pdf_path.exists():
                pdf_path.unlink()
            raise HTTPException(500, f"Extraction failed for {file.filename}: {str(e)}") from e
        finally:
            if pdf_path.exists():
                pdf_path.unlink()

    # Single file: return Excel directly
    if len(results) == 1:
        r = results[0]
        response = FileResponse(
            path=r["excel_path"],
            filename=r["download_name"],
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"X-Warnings": "|".join(r["warnings"])} if r["warnings"] else {},
        )
        return response

    # Multiple files: return JSON with download links + warnings
    return JSONResponse([
        {
            "job_id": r["job_id"],
            "original_filename": r["original_filename"],
            "download_name": r["download_name"],
            "warnings": r["warnings"],
        }
        for r in results
    ])


@app.get("/download/{job_id}")
async def download(job_id: str, filename: str = "report_financials.xlsx"):
    """Download a previously generated Excel file by job_id."""
    excel_path = UPLOAD_DIR / f"{job_id}_output.xlsx"
    if not excel_path.exists():
        raise HTTPException(404, "File not found or expired")
    return FileResponse(
        path=str(excel_path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _run_extraction(pdf_path: str, job_id: str) -> dict:
    """
    Run the full extraction pipeline:
      1. Identify standalone pages (Claude API / regex)
      2. Scan for multiple P&L candidates and generate warnings
      3. Docling extracts P&L from targeted page
      4. Docling extracts note breakup from standalone notes
      5. Generate Excel
    """
    from app.docling_extractor import extract_note_docling, extract_pnl_docling
    from app.excel_writer import create_excel
    from app.extractor import (
        _is_likely_toc_page,
        compute_pnl_confidence,
        find_all_standalone_candidates,
        find_note_page,
        validate_note_extraction,
    )
    from app.extractor import find_standalone_pages as find_standalone_pages_regex
    from app.pdf_utils import extract_page_headers

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Step 1: Identify standalone pages (Claude API primary, regex fallback)
    # ------------------------------------------------------------------
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    pages = {}
    company_name = None
    claude_identified = False

    if ANTHROPIC_API_KEY:
        logger.info(f"[{job_id}] Using Claude Sonnet 4.5 to identify standalone pages")
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


            # Guardrail: Claude can sometimes pick Table-of-Contents pages
            # because they mention all statement names with page numbers.
            for key, idx in list(pages.items()):
                if idx is None:
                    continue
                page_text = extract_page_headers(pdf_path, {key: idx}, num_lines=40).get(key, "")
                if page_text and _is_likely_toc_page(page_text):
                    logger.warning(f"[{job_id}] Ignoring Claude {key} page {idx + 1}: likely TOC page")
                    pages.pop(key, None)
                    if key == "pnl":
                        claude_identified = False

            logger.info(f"[{job_id}] Claude identified standalone pages: {pages}, "
                        f"Company: {company_name}, FY: {fy_current} / {fy_previous}")
        except Exception as e:
            logger.warning(f"[{job_id}] Claude page identification failed: {e}")

    # Fallback: regex (only if Claude didn't find pages)
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to regex page identification")
        pages, _ = find_standalone_pages_regex(pdf_path)

    if "pnl" not in pages:
        raise ValueError(
            "Could not find a P&L (Statement of Profit and Loss) page. "
            "Please ensure the PDF is an annual report with financial statements."
        )

    # ------------------------------------------------------------------
    # Step 1b: Check for multiple standalone P&L candidates and warn
    # ------------------------------------------------------------------
    candidates = find_all_standalone_candidates(pdf_path)
    pnl_candidates = candidates.get("pnl", [])

    if pages["pnl"] not in pnl_candidates:
        pnl_candidates.insert(0, pages["pnl"])

    confidence = compute_pnl_confidence(len(pnl_candidates), claude_identified)
    logger.info(f"[{job_id}] P&L confidence: {confidence:.0%} "
                f"(candidates={len(pnl_candidates)}, claude={claude_identified})")

    if len(pnl_candidates) > 1:
        candidate_pages_display = ", ".join(str(p + 1) for p in pnl_candidates)
        warnings.append(
            f"Multiple P&L pages detected (PDF pages: {candidate_pages_display}). "
            f"Extracted from page {pages['pnl'] + 1}. "
            f"Please verify the totals in the Validation sheet to ensure the correct page was used."
        )
        logger.warning(f"[{job_id}] {warnings[-1]}")

    # ------------------------------------------------------------------
    # Step 2: Extract page headers for validation
    # ------------------------------------------------------------------
    page_headers = extract_page_headers(pdf_path, pages)
    logger.info(f"[{job_id}] Extracted headers for identified pages: "
                f"{list(page_headers.keys())}")

    # ------------------------------------------------------------------
    # Step 3: Docling extracts P&L from targeted page(s)
    # ------------------------------------------------------------------
    logger.info(f"[{job_id}] Docling extracting P&L from page {pages['pnl']}")
    pnl = extract_pnl_docling(pdf_path, pages["pnl"])

    if not pnl.get('items'):
        raise ValueError(
            f"Could not extract any P&L line items from page {pages['pnl']}. "
            f"The PDF table structure may not be supported."
        )

    if company_name:
        pnl['company'] = company_name

    logger.info(f"[{job_id}] Docling P&L: {len(pnl['items'])} items - "
                f"{list(pnl['items'].keys())}")

    # ------------------------------------------------------------------
    # Step 4: Docling extracts note breakup from notes section
    # ------------------------------------------------------------------
    note_items = []
    note_total = None
    note_num = pnl["note_refs"].get("Other expenses")
    # Use the actual matched label (e.g. "Administrative Charges") as
    # the keyword for note page search, not always "Other expenses"
    oe_label = pnl.get("operating_expense_label", "Other expenses")

    search_start = pages.get("notes_start", pages["pnl"])

    if note_num:
        note_page, _ = find_note_page(
            pdf_path, note_num, search_start, oe_label
        )
        if note_page is not None:
            logger.info(f"[{job_id}] Docling extracting Note {note_num} from page {note_page}")
            try:
                note_items, note_total = extract_note_docling(
                    pdf_path, note_page, note_num
                )
                logger.info(f"[{job_id}] Docling Note {note_num}: "
                            f"{len(note_items)} items extracted")
            except Exception as e:
                logger.warning(f"[{job_id}] Docling note extraction failed: {e}")
        else:
            logger.warning(f"[{job_id}] Could not find Note {note_num} page")
    else:
        logger.warning(f"[{job_id}] No note reference found for Other expenses in P&L")

    # ------------------------------------------------------------------
    # Step 4b: Validate note extraction against P&L
    # ------------------------------------------------------------------
    validation = validate_note_extraction(pnl, note_items, note_total, note_num)
    for check in validation:
        status = "PASS" if check["ok"] else "FAIL"
        logger.info(f"[{job_id}] Validation: {check['name']} -> {status} "
                     f"(got {check['actual']:.2f}, expected {check['expected']:.2f})")
        if not check["ok"]:
            warnings.append(
                f"Validation FAIL: {check['name']} "
                f"(got {check['actual']:.2f}, expected {check['expected']:.2f})"
            )

    # ------------------------------------------------------------------
    # Step 5: Generate Excel with header validation
    # ------------------------------------------------------------------
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

    create_excel(data, excel_path)
    logger.info(f"[{job_id}] Excel generated: {excel_path}")

    return {"excel_path": excel_path, "data": data, "warnings": warnings}


if __name__ == "__main__":
    import uvicorn

    from app.config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
