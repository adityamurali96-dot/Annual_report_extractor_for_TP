"""
FastAPI application for Annual Report Financial Extractor.
Handles PDF upload, extraction via Claude API (with regex fallback), and Excel download.
"""

import os
import uuid
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import UPLOAD_DIR, ANTHROPIC_API_KEY, MAX_UPLOAD_SIZE_MB, ALLOWED_EXTENSIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Annual Report Extractor",
    description="Extract standalone financials from annual report PDFs into Excel",
    version="1.0.0",
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
    Returns the Excel file for download.
    """
    # Validate file
    if not file.filename:
        raise HTTPException(400, "No file provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Only PDF files are allowed, got {ext}")

    # Read file content
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_SIZE_MB:
        raise HTTPException(400, f"File too large ({size_mb:.1f}MB). Max is {MAX_UPLOAD_SIZE_MB}MB")

    # Save to temp file
    job_id = str(uuid.uuid4())[:8]
    pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
    pdf_path.write_bytes(content)

    logger.info(f"[{job_id}] Received {file.filename} ({size_mb:.1f}MB)")

    try:
        result = _run_extraction(str(pdf_path), job_id)
        excel_path = result["excel_path"]

        # Return the Excel file
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
        # Clean up uploaded PDF
        if pdf_path.exists():
            pdf_path.unlink()


def _run_extraction(pdf_path: str, job_id: str) -> dict:
    """
    Run the extraction pipeline.
    Uses Claude API if configured, falls back to regex-based extraction.
    """
    from app.excel_writer import create_excel

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")

    if ANTHROPIC_API_KEY:
        logger.info(f"[{job_id}] Using Claude API extraction")
        data = _extract_with_claude(pdf_path, job_id)
    else:
        logger.info(f"[{job_id}] Using regex-based extraction (no API key)")
        data = _extract_with_regex(pdf_path, job_id)

    create_excel(data, excel_path)
    logger.info(f"[{job_id}] Excel generated: {excel_path}")

    return {"excel_path": excel_path, "data": data}


def _extract_with_claude(pdf_path: str, job_id: str) -> dict:
    """Full extraction using Claude API."""
    from app.claude_parser import extract_with_claude
    data = extract_with_claude(pdf_path)
    logger.info(f"[{job_id}] Claude extraction complete. Company: {data.get('company')}")
    return data


def _extract_with_regex(pdf_path: str, job_id: str) -> dict:
    """Fallback extraction using regex patterns."""
    from app.extractor import (
        find_standalone_pages, extract_pnl_regex,
        find_note_page, extract_note_breakup
    )

    pages, total = find_standalone_pages(pdf_path)
    logger.info(f"[{job_id}] Found pages: {pages} (total: {total})")

    if 'pnl' not in pages:
        raise ValueError("Could not find Standalone P&L page. "
                         "Please ensure the PDF is an annual report with standalone statements.")

    pnl = extract_pnl_regex(pdf_path, pages['pnl'])
    logger.info(f"[{job_id}] P&L extracted: {len(pnl['items'])} items")

    note_items = []
    note_total = None
    note_num = pnl['note_refs'].get('Other expenses')

    if note_num:
        note_page, note_line = find_note_page(pdf_path, note_num, pages['pnl'], "Other expenses")
        if note_page is not None:
            note_items, note_total = extract_note_breakup(pdf_path, note_page, note_line, note_num)
            logger.info(f"[{job_id}] Note {note_num}: {len(note_items)} items extracted")

    return {
        "company": pnl['company'],
        "currency": pnl['currency'],
        "fy_current": "FY Current",
        "fy_previous": "FY Previous",
        "pages": pages,
        "pnl": pnl,
        "note_items": note_items,
        "note_total": note_total,
        "note_number": note_num,
    }
