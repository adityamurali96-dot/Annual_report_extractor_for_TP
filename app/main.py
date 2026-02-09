"""
FastAPI application for Annual Report Financial Extractor.

Pipeline (PDF-only):
  1. Accept PDF upload
  2. Identify standalone financial statement pages (Claude API / regex)
  3. If multiple candidates found, ask user to confirm page numbers
  4. Extract page headers for company validation
  5. Docling extracts tables from only those targeted pages
  6. Write to Excel with header validation
"""

import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ANTHROPIC_API_KEY, MAX_UPLOAD_SIZE_MB, UPLOAD_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Annual Report Extractor",
    description="Extract standalone financials from annual report PDFs into Excel",
    version="4.0.0",
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
async def identify(file: UploadFile = File(...)):
    """
    Upload a PDF and identify standalone financial statement pages.

    Returns the recommended pages plus all candidates so the user can
    confirm before extraction proceeds. This prevents wrong-page extraction
    when there are multiple standalone sections or missing headings.
    """
    if not file.filename:
        raise HTTPException(400, "No file provided")

    ext = Path(file.filename).suffix.lower()
    if ext != ".pdf":
        raise HTTPException(400, f"Only PDF files are accepted, got '{ext}'")

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
        return JSONResponse(result)
    except Exception as e:
        logger.exception(f"[{job_id}] Page identification failed")
        if pdf_path.exists():
            pdf_path.unlink()
        raise HTTPException(500, f"Page identification failed: {str(e)}") from e


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
    Run page identification and candidate scanning.

    Returns a dict with:
      - job_id: for the subsequent /extract call
      - recommended_pages: best-guess page numbers from Claude/regex
      - candidates: all pages matching standalone patterns
      - candidate_headers: header text for every candidate page
      - needs_confirmation: True if multiple candidates for any section
      - company_name, fy_current, fy_previous: metadata from identification
    """
    from app.extractor import find_all_standalone_candidates
    from app.extractor import find_standalone_pages as find_standalone_pages_regex
    from app.pdf_utils import extract_page_headers

    pages = {}
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    company_name = None

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

    # --- Scan for ALL candidate pages ---
    candidates = find_all_standalone_candidates(pdf_path)
    logger.info(f"[{job_id}] All standalone candidates: {candidates}")

    # Ensure the recommended page is always included in candidates
    for section in ["pnl", "bs", "cf"]:
        if section in pages and pages[section] not in candidates.get(section, []):
            candidates.setdefault(section, []).insert(0, pages[section])

    # --- Check if confirmation is needed ---
    needs_confirmation = any(
        len(candidate_list) > 1
        for candidate_list in candidates.values()
    )

    # --- Extract headers for ALL candidate pages (for user review) ---
    all_candidate_pages = {}
    for section, page_list in candidates.items():
        for page_num in page_list:
            all_candidate_pages[f"{section}_p{page_num}"] = page_num

    candidate_headers = extract_page_headers(pdf_path, all_candidate_pages, num_lines=5)

    # Reorganize headers by section for cleaner frontend consumption
    # Use string keys for page numbers so JSON serialization works cleanly
    headers_by_section: dict[str, dict[str, str]] = {}
    for section, page_list in candidates.items():
        headers_by_section[section] = {}
        for page_num in page_list:
            key = f"{section}_p{page_num}"
            headers_by_section[section][str(page_num)] = candidate_headers.get(key, "(no header text)")

    logger.info(f"[{job_id}] Needs confirmation: {needs_confirmation}")

    return {
        "job_id": job_id,
        "recommended_pages": pages,
        "candidates": candidates,
        "candidate_headers": headers_by_section,
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
