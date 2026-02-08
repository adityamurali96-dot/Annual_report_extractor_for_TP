"""
FastAPI application for Annual Report Financial Extractor.

Pipeline:
  1. Accept PDF upload (only PDF files)
  2. Identify standalone financial statement pages (Claude API primary, regex fallback)
  3. Extract page headers for company validation
  4. Extract P&L data from identified standalone pages only
  5. Extract note breakup from standalone notes only
  6. Generate Excel with header validation info
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
    version="2.0.0",
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
      1. Identify standalone financial statement pages (Claude API or fallback)
      2. Extract page headers for validation
      3. Extract P&L from standalone pages only
      4. Extract note breakup from standalone notes only
      5. Generate Excel
    """
    from app.excel_writer import create_excel
    from app.pdf_utils import extract_page_headers
    from app.extractor import (
        find_standalone_pages as find_standalone_pages_regex,
        extract_pnl_regex,
        find_note_page, extract_note_breakup,
    )
    from app.table_extractor import (
        find_standalone_pages as find_standalone_pages_table,
        extract_pnl_from_tables,
        extract_note_from_tables,
    )

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")

    # ------------------------------------------------------------------
    # Step 1: Identify standalone financial statement pages
    # Claude API is the primary method for page identification.
    # Falls back to pymupdf4llm/regex only if Claude is unavailable.
    # ------------------------------------------------------------------
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    pages = {}
    page_headers_from_claude = {}
    company_name = None

    if ANTHROPIC_API_KEY:
        logger.info(f"[{job_id}] Using Claude API to identify standalone financial pages")
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

            logger.info(f"[{job_id}] Claude identified standalone pages: {pages}, "
                        f"Company: {company_name}, FY: {fy_current} / {fy_previous}")
        except Exception as e:
            logger.warning(f"[{job_id}] Claude page identification failed: {e}")

    # Fallback: pymupdf4llm then regex (only if Claude didn't find pages)
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to pymupdf4llm for page identification")
        try:
            pages, _ = find_standalone_pages_table(pdf_path)
        except Exception as e:
            logger.warning(f"[{job_id}] pymupdf4llm page identification failed: {e}")

    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to regex page identification")
        pages, _ = find_standalone_pages_regex(pdf_path)

    if "pnl" not in pages:
        raise ValueError(
            "Could not find Standalone P&L page. "
            "Please ensure the PDF is an annual report with standalone financial statements."
        )

    # ------------------------------------------------------------------
    # Step 2: Extract page headers for validation
    # Read actual headers from the PDF pages for the user to verify
    # that the correct company (not a subsidiary) is being extracted.
    # ------------------------------------------------------------------
    page_headers = extract_page_headers(pdf_path, pages)
    logger.info(f"[{job_id}] Extracted headers from standalone pages: "
                f"{list(page_headers.keys())}")

    # ------------------------------------------------------------------
    # Step 3: Extract P&L from standalone page only
    # ------------------------------------------------------------------
    pnl = None
    extraction_method = None

    # Primary: pymupdf4llm structured table extraction
    try:
        logger.info(f"[{job_id}] Extracting P&L from standalone page {pages['pnl']} (pymupdf4llm)")
        pnl = extract_pnl_from_tables(pdf_path, pages["pnl"])
        item_count = len(pnl.get('items', {}))
        logger.info(f"[{job_id}] pymupdf4llm P&L: {item_count} items extracted")
        if item_count < 5:
            logger.warning(f"[{job_id}] pymupdf4llm extracted only {item_count} items, "
                           f"falling back to regex")
            pnl = None
        else:
            extraction_method = "pymupdf4llm"
    except Exception as e:
        logger.warning(f"[{job_id}] pymupdf4llm P&L extraction failed: {e}")

    # Fallback: regex-based extraction
    if pnl is None:
        logger.info(f"[{job_id}] Extracting P&L from standalone page {pages['pnl']} (regex)")
        pnl = extract_pnl_regex(pdf_path, pages["pnl"])
        extraction_method = "regex"
        logger.info(f"[{job_id}] Regex P&L: {len(pnl['items'])} items extracted")

    if not pnl.get('items'):
        raise ValueError(
            f"Could not extract any P&L line items from standalone page {pages['pnl']}. "
            f"The PDF table structure may not be supported."
        )

    # Override company name with Claude-detected name if available
    if company_name:
        pnl['company'] = company_name

    logger.info(f"[{job_id}] P&L extraction complete ({extraction_method}): "
                f"{len(pnl['items'])} items - {list(pnl['items'].keys())}")

    # ------------------------------------------------------------------
    # Step 4: Extract note breakup from standalone notes only
    # ------------------------------------------------------------------
    note_items = []
    note_total = None
    note_num = pnl["note_refs"].get("Other expenses")

    if note_num:
        # Search for notes within standalone section only
        search_start = pages.get("notes_start", pages["pnl"])
        note_page, note_line = find_note_page(
            pdf_path, note_num, search_start, "Other expenses"
        )
        if note_page is not None:
            # Primary: pymupdf4llm table extraction for notes
            try:
                logger.info(f"[{job_id}] Extracting Note {note_num} from standalone page "
                            f"{note_page} (pymupdf4llm)")
                note_items, note_total = extract_note_from_tables(
                    pdf_path, note_page, note_num
                )
                if not note_items:
                    raise ValueError("No note items extracted")
                logger.info(f"[{job_id}] pymupdf4llm Note {note_num}: "
                            f"{len(note_items)} items extracted")
            except Exception as e:
                logger.warning(f"[{job_id}] pymupdf4llm note extraction failed: {e}")
                # Fallback: regex-based note extraction
                if note_line is not None:
                    logger.info(f"[{job_id}] Falling back to regex note extraction")
                    note_items, note_total = extract_note_breakup(
                        pdf_path, note_page, note_line, note_num
                    )
                    logger.info(f"[{job_id}] Regex Note {note_num}: "
                                f"{len(note_items)} items extracted")

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
