"""
FastAPI application for Annual Report Financial Extractor.

Pipeline (PDF-only):
  1. Accept PDF upload
  2. Claude API (Sonnet 4.5) identifies standalone financial statement pages
  3. Extract page headers for company validation
  4. Docling extracts tables from only those 2 targeted pages
  5. Write to Excel with header validation
"""

import uuid
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import UPLOAD_DIR, ANTHROPIC_API_KEY, MAX_UPLOAD_SIZE_MB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Annual Report Extractor",
    description="Extract standalone financials from annual report PDFs into Excel",
    version="3.0.0",
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
async def extract(file: UploadFile = File(...)):
    """
    Upload a PDF annual report and extract standalone financials.
    Only PDF files are accepted. Returns the Excel file for download.
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
        result = _run_extraction(str(pdf_path), job_id)
        excel_path = result["excel_path"]

        download_name = f"{Path(file.filename).stem}_financials.xlsx"
        return FileResponse(
            path=excel_path,
            filename=download_name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        logger.exception(f"[{job_id}] Extraction failed")
        raise HTTPException(500, f"Extraction failed: {str(e)}")
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def _run_extraction(pdf_path: str, job_id: str) -> dict:
    """
    Run the extraction pipeline:
      1. Claude API identifies standalone financial statement pages
      2. Extract page headers for company validation
      3. Docling extracts P&L from those targeted pages only
      4. Docling extracts note breakup from standalone notes only
      5. Generate Excel
    """
    from app.excel_writer import create_excel
    from app.pdf_utils import extract_page_headers
    from app.docling_extractor import extract_pnl_docling, extract_note_docling
    from app.extractor import find_note_page

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")

    # ------------------------------------------------------------------
    # Step 1: Identify standalone pages (Claude API primary, regex fallback)
    # ------------------------------------------------------------------
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    pages = {}
    company_name = None

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

    # Fallback: regex (only if Claude didn't find pages)
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to regex page identification")
        from app.extractor import find_standalone_pages as find_standalone_pages_regex
        pages, _ = find_standalone_pages_regex(pdf_path)

    if "pnl" not in pages:
        raise ValueError(
            "Could not find Standalone P&L page. "
            "Please ensure the PDF is an annual report with standalone financial statements."
        )

    # ------------------------------------------------------------------
    # Step 2: Extract page headers for validation
    # ------------------------------------------------------------------
    page_headers = extract_page_headers(pdf_path, pages)
    logger.info(f"[{job_id}] Extracted headers from standalone pages: "
                f"{list(page_headers.keys())}")

    # ------------------------------------------------------------------
    # Step 3: Docling extracts P&L from targeted standalone page(s)
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
    # Step 4: Docling extracts note breakup from standalone notes
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
        "page_headers": page_headers,
    }

    create_excel(data, excel_path)
    logger.info(f"[{job_id}] Excel generated: {excel_path}")

    return {"excel_path": excel_path, "data": data}


if __name__ == "__main__":
    import uvicorn
    from app.config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
