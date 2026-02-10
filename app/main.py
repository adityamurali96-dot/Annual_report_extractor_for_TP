"""
FastAPI application for Annual Report Financial Extractor.

Pipeline (PDF-only, supports batch of up to 2 reports):
  1. Accept PDF upload(s)
  2. Identify standalone financial statement pages (Claude API / regex)
  3. If P&L confidence < 70%, ask user to confirm the P&L page number
  4. Docling extracts tables from only those targeted pages
  5. Write to Excel with header validation
"""

import json
import logging
import uuid
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ANTHROPIC_API_KEY, MAX_UPLOAD_SIZE_MB, UPLOAD_DIR

CONFIDENCE_THRESHOLD = 0.70

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Annual Report Extractor",
    description="Extract standalone financials from annual report PDFs into Excel",
    version="5.0.0",
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


@app.post("/identify")
async def identify(files: List[UploadFile] = File(...)):
    """
    Upload one or two PDFs and identify standalone P&L pages.

    Returns a list of identification results (one per file). Each result
    includes recommended pages, P&L candidate list, confidence score,
    and whether user confirmation is needed (confidence < 70%).
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
            result = _identify_pages(str(pdf_path), job_id)
            result["original_filename"] = file.filename
            results.append(result)
        except Exception as e:
            logger.exception(f"[{job_id}] Page identification failed")
            if pdf_path.exists():
                pdf_path.unlink()
            raise HTTPException(500, f"Page identification failed for {file.filename}: {str(e)}") from e

    return JSONResponse(results)


@app.post("/extract")
async def extract(
    job_id: str = Form(...),
    confirmed_pages: str = Form(...),
    original_filename: str = Form("report"),
):
    """
    Run extraction using confirmed page numbers.

    Expects job_id from a prior /identify call and user-confirmed page numbers.
    """
    pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(400, "Session expired or invalid job_id. Please re-upload the PDF.")

    try:
        pages = json.loads(confirmed_pages)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid confirmed_pages format")

    logger.info(f"[{job_id}] Extracting with confirmed pages: {pages}")

    try:
        result = _run_extraction(str(pdf_path), job_id, pages)
        excel_path = result["excel_path"]

        download_name = f"{Path(original_filename).stem}_financials.xlsx"
        return FileResponse(
            path=excel_path,
            filename=download_name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        logger.exception(f"[{job_id}] Extraction failed")
        raise HTTPException(500, f"Extraction failed: {str(e)}") from e
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def _identify_pages(pdf_path: str, job_id: str) -> dict:
    """
    Run page identification, scan for P&L candidates, compute confidence.

    Returns a dict with:
      - job_id: for the subsequent /extract call
      - recommended_pages: best-guess page numbers from Claude/regex
      - pnl_candidates: list of all P&L candidate page numbers
      - pnl_candidate_headers: header text for each P&L candidate page
      - confidence: float 0.0-1.0 for P&L page identification
      - needs_confirmation: True if confidence < 70%
      - company_name, fy_current, fy_previous: metadata
    """
    from app.extractor import compute_pnl_confidence, find_all_standalone_candidates
    from app.extractor import find_standalone_pages as find_standalone_pages_regex
    from app.pdf_utils import extract_page_headers

    pages = {}
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    company_name = None
    claude_identified = False

    # --- Claude API identification (primary) ---
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

            logger.info(f"[{job_id}] Claude identified standalone pages: {pages}, "
                        f"Company: {company_name}, FY: {fy_current} / {fy_previous}")
        except Exception as e:
            logger.warning(f"[{job_id}] Claude page identification failed: {e}")

    # --- Regex fallback ---
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to regex page identification")
        pages, _ = find_standalone_pages_regex(pdf_path)

    if "pnl" not in pages:
        raise ValueError(
            "Could not find Standalone P&L page. "
            "Please ensure the PDF is an annual report with standalone financial statements."
        )

    # --- Scan for ALL P&L candidate pages ---
    candidates = find_all_standalone_candidates(pdf_path)
    pnl_candidates = candidates.get("pnl", [])
    logger.info(f"[{job_id}] P&L candidates: {pnl_candidates}")

    # Ensure the recommended page is always in the candidate list
    if pages["pnl"] not in pnl_candidates:
        pnl_candidates.insert(0, pages["pnl"])

    # --- Compute confidence ---
    confidence = compute_pnl_confidence(len(pnl_candidates), claude_identified)
    needs_confirmation = confidence < CONFIDENCE_THRESHOLD

    logger.info(f"[{job_id}] P&L confidence: {confidence:.0%} "
                f"(candidates={len(pnl_candidates)}, claude={claude_identified}) "
                f"-> {'PROMPT' if needs_confirmation else 'AUTO-PROCEED'}")

    # --- Extract headers for P&L candidate pages (for user review) ---
    pnl_header_map = {f"pnl_p{pg}": pg for pg in pnl_candidates}
    raw_headers = extract_page_headers(pdf_path, pnl_header_map, num_lines=5)
    pnl_candidate_headers = {
        str(pg): raw_headers.get(f"pnl_p{pg}", "(no header text)")
        for pg in pnl_candidates
    }

    return {
        "job_id": job_id,
        "recommended_pages": pages,
        "pnl_candidates": pnl_candidates,
        "pnl_candidate_headers": pnl_candidate_headers,
        "confidence": round(confidence, 2),
        "needs_confirmation": needs_confirmation,
        "company_name": company_name,
        "fy_current": fy_current,
        "fy_previous": fy_previous,
    }


def _run_extraction(pdf_path: str, job_id: str, pages: dict) -> dict:
    """
    Run the extraction pipeline with user-confirmed pages:
      1. Extract page headers for company validation
      2. Docling extracts P&L from confirmed standalone page(s)
      3. Docling extracts note breakup from standalone notes
      4. Generate Excel
    """
    from app.docling_extractor import extract_note_docling, extract_pnl_docling
    from app.excel_writer import create_excel
    from app.extractor import find_note_page, validate_note_extraction
    from app.pdf_utils import extract_page_headers

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")

    # Use metadata passed via pages dict or defaults
    fy_current = pages.pop("fy_current", "FY Current")
    fy_previous = pages.pop("fy_previous", "FY Previous")
    company_name = pages.pop("company_name", None)

    # Ensure page values are ints
    for key in list(pages.keys()):
        if pages[key] is not None:
            pages[key] = int(pages[key])

    if "pnl" not in pages:
        raise ValueError(
            "Could not find Standalone P&L page. "
            "Please ensure the PDF is an annual report with standalone financial statements."
        )

    # ------------------------------------------------------------------
    # Step 1: Extract page headers for validation
    # ------------------------------------------------------------------
    page_headers = extract_page_headers(pdf_path, pages)
    logger.info(f"[{job_id}] Extracted headers from standalone pages: "
                f"{list(page_headers.keys())}")

    # ------------------------------------------------------------------
    # Step 2: Docling extracts P&L from targeted standalone page(s)
    # ------------------------------------------------------------------
    logger.info(f"[{job_id}] Docling extracting P&L from standalone page {pages['pnl']}")
    pnl = extract_pnl_docling(pdf_path, pages["pnl"])

    if not pnl.get('items'):
        raise ValueError(
            f"Could not extract any P&L line items from standalone page {pages['pnl']}. "
            f"The PDF table structure may not be supported."
        )

    if company_name:
        pnl['company'] = company_name

    logger.info(f"[{job_id}] Docling P&L: {len(pnl['items'])} items - "
                f"{list(pnl['items'].keys())}")

    # ------------------------------------------------------------------
    # Step 3: Docling extracts note breakup from standalone notes
    # ------------------------------------------------------------------
    note_items = []
    note_total = None
    note_num = pnl["note_refs"].get("Other expenses")

    search_start = pages.get("notes_start", pages["pnl"])

    if note_num:
        note_page, _ = find_note_page(
            pdf_path, note_num, search_start, "Other expenses"
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
    # Step 3b: Validate note extraction against P&L
    # ------------------------------------------------------------------
    validation = validate_note_extraction(pnl, note_items, note_total, note_num)
    for check in validation:
        status = "PASS" if check["ok"] else "FAIL"
        logger.info(f"[{job_id}] Validation: {check['name']} -> {status} "
                     f"(got {check['actual']:.2f}, expected {check['expected']:.2f})")

    # ------------------------------------------------------------------
    # Step 4: Generate Excel with header validation
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
    }

    create_excel(data, excel_path)
    logger.info(f"[{job_id}] Excel generated: {excel_path}")

    return {"excel_path": excel_path, "data": data}


if __name__ == "__main__":
    import uvicorn

    from app.config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
