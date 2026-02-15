"""
FastAPI application for Annual Report Financial Extractor.

Pipeline (PDF-only, supports batch of up to 2 reports):
  1. Accept 1 or 2 PDF uploads
  2. Identify standalone financial statement pages (Claude API / regex)
  3. Extract tables from targeted pages (auto-proceeds, no confirmation)
  4. Return Excel file(s) with warnings if multiple standalone sections detected
"""

import asyncio
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# Thread pool for running blocking extraction in background threads
executor = ThreadPoolExecutor(max_workers=2)


def _cleanup_old_files(max_age_seconds: int = 3600):
    """Delete output files older than max_age_seconds (default 1 hour)."""
    now = time.time()
    for f in UPLOAD_DIR.iterdir():
        if f.suffix == '.xlsx' and (now - f.stat().st_mtime) > max_age_seconds:
            f.unlink(missing_ok=True)
            logger.info(f"Cleaned up old file: {f.name}")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the upload page."""
    from app.adobe_converter import is_adobe_available
    return templates.TemplateResponse("index.html", {
        "request": request,
        "has_api_key": bool(ANTHROPIC_API_KEY),
        "has_adobe_ocr": is_adobe_available(),
    })


@app.get("/health")
async def health():
    """Health check for Railway."""
    from app.adobe_converter import is_adobe_available
    return {
        "status": "ok",
        "api_key_configured": bool(ANTHROPIC_API_KEY),
        "adobe_ocr_configured": is_adobe_available(),
    }


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

    _cleanup_old_files()

    results = []

    for file in files:
        if not file.filename:
            raise HTTPException(400, "No file provided")

        ext = Path(file.filename).suffix.lower()
        if ext != ".pdf":
            raise HTTPException(400, f"Only PDF files are accepted, got '{ext}' for {file.filename}")

        # Read file in chunks to avoid loading huge files into memory
        content = bytearray()
        max_bytes = MAX_UPLOAD_SIZE_MB * 1024 * 1024
        chunk_size = 1024 * 1024  # 1MB chunks
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise HTTPException(
                    400,
                    f"File too large. Maximum is {MAX_UPLOAD_SIZE_MB}MB."
                )
        content = bytes(content)
        size_mb = len(content) / (1024 * 1024)

        job_id = str(uuid.uuid4())[:8]
        pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
        pdf_path.write_bytes(content)

        logger.info(f"[{job_id}] Received {file.filename} ({size_mb:.1f}MB)")

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                executor,
                _run_extraction,
                str(pdf_path),
                job_id,
            )
            excel_path = result["excel_path"]
            download_name = f"{Path(file.filename).stem}_financials.xlsx"

            results.append({
                "job_id": job_id,
                "original_filename": file.filename,
                "download_name": download_name,
                "excel_path": excel_path,
                "warnings": result.get("warnings", []),
            })
        except ValueError as e:
            # ValueError = known failures (no P&L found, no tables, etc.)
            # These already contain comprehensive diagnostic info
            logger.warning(f"[{job_id}] Known failure for {file.filename}: {e}")
            if pdf_path.exists():
                pdf_path.unlink()
            raise HTTPException(
                422,
                f"{file.filename}: {str(e)}"
            ) from e
        except RuntimeError as e:
            # RuntimeError = configuration issues (missing SDK, bad credentials)
            logger.error(f"[{job_id}] Configuration error for {file.filename}: {e}")
            if pdf_path.exists():
                pdf_path.unlink()
            raise HTTPException(
                500,
                f"{file.filename}: {str(e)}"
            ) from e
        except Exception as e:
            logger.exception(f"[{job_id}] Unexpected error for {file.filename}")
            if pdf_path.exists():
                pdf_path.unlink()
            raise HTTPException(
                500,
                f"{file.filename}: An unexpected error occurred ({type(e).__name__}). "
                f"Please try a different PDF or contact support."
            ) from e
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
    from app.adobe_converter import convert_to_searchable_pdf, is_adobe_available
    from app.docling_extractor import extract_note_docling, extract_pnl_docling
    from app.excel_writer import create_excel
    from app.extractor import (
        _is_likely_toc_page,
        compute_pnl_confidence,
        find_all_standalone_candidates,
        find_note_page,
        find_pnl_by_content_scoring,
        validate_note_extraction,
    )
    from app.extractor import find_standalone_pages as find_standalone_pages_regex
    from app.pdf_utils import classify_pdf, extract_page_headers, is_scanned_pdf

    excel_path = str(UPLOAD_DIR / f"{job_id}_output.xlsx")
    warnings: list[str] = []
    converted_pdf_path = None

    # ------------------------------------------------------------------
    # Step 0: Classify PDF type and convert if needed
    # ------------------------------------------------------------------
    pdf_type = classify_pdf(pdf_path)
    logger.info(f"[{job_id}] PDF classified as: {pdf_type}")

    if pdf_type in ("scanned", "vector_outlined"):
        type_label = {
            "scanned": "scanned/image-based",
            "vector_outlined": "vector-outlined (fonts converted to shapes)",
        }[pdf_type]

        if is_adobe_available():
            logger.info(f"[{job_id}] PDF is {type_label}. Running Adobe OCR...")
            try:
                converted_pdf_path = convert_to_searchable_pdf(pdf_path)
                pdf_path = converted_pdf_path
                logger.info(f"[{job_id}] Adobe OCR successful. Using converted PDF.")
                warnings.append(
                    f"PDF detected as {type_label}. "
                    f"Converted to searchable PDF via Adobe OCR before extraction."
                )
            except Exception as e:
                logger.error(f"[{job_id}] Adobe OCR failed: {e}")
                warnings.append(
                    f"Adobe OCR conversion failed ({str(e)[:100]}). "
                    f"Attempting extraction on original PDF."
                )
        else:
            logger.warning(f"[{job_id}] PDF is {type_label} but Adobe API not configured.")
            warnings.append(
                f"PDF detected as {type_label}. Text cannot be extracted directly. "
                f"Adobe OCR API is not configured. "
                f"Set ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET for automatic conversion."
            )

    scanned = is_scanned_pdf(pdf_path)
    if scanned and not converted_pdf_path:
        logger.info(f"[{job_id}] Scanned/image-based PDF detected — Tesseract OCR will be used")
        warnings.append(
            "This PDF appears to be scanned/image-based. OCR was used for text extraction. "
            "Please verify the extracted data carefully as OCR accuracy may vary."
        )

    # ------------------------------------------------------------------
    # Step 1: Identify standalone pages (Claude API primary, regex fallback)
    # ------------------------------------------------------------------
    fy_current = "FY Current"
    fy_previous = "FY Previous"
    pages = {}
    company_name = None
    currency = None
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
            currency = page_info.get("currency")

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

    # Fallback 1: regex title matching (only if Claude didn't find pages)
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Falling back to regex page identification")
        pages, _ = find_standalone_pages_regex(pdf_path)

    # Fallback 2: content-based scoring (when no title match found)
    # This handles PDFs with no index, scanned documents with OCR-damaged
    # titles, or unusual formatting.
    if "pnl" not in pages:
        logger.info(f"[{job_id}] Regex failed, trying content-based scoring")
        scored_pages = find_pnl_by_content_scoring(pdf_path, min_score=20)
        if scored_pages:
            pages.update(scored_pages)
            warnings.append(
                "P&L page was identified by content scoring (no standard title found). "
                "Please verify the extracted data in the Validation sheet."
            )
            logger.info(f"[{job_id}] Content scoring found P&L at page {scored_pages.get('pnl', '?')}")

    if "pnl" not in pages:
        # Build a comprehensive error message explaining what was tried
        _diag_parts = []
        _diag_parts.append(
            "Could not find a P&L (Statement of Profit and Loss) page."
        )
        _diag_parts.append(f"PDF type detected: {pdf_type}.")

        if pdf_type in ("scanned", "vector_outlined"):
            type_label = {
                "scanned": "scanned/image-based",
                "vector_outlined": "vector-outlined (fonts converted to shapes)",
            }.get(pdf_type, pdf_type)
            if converted_pdf_path:
                _diag_parts.append(
                    f"Adobe OCR was used to convert this {type_label} PDF, "
                    f"but the converted text still did not contain recognizable "
                    f"financial statement titles."
                )
            elif is_adobe_available():
                _diag_parts.append(
                    f"This PDF is {type_label} and Adobe OCR conversion failed. "
                    f"The original PDF has no extractable text for page identification."
                )
            else:
                _diag_parts.append(
                    f"This PDF is {type_label} — text cannot be extracted directly. "
                    f"Adobe OCR API is not configured. "
                    f"Set ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET environment variables "
                    f"to enable automatic conversion of non-text PDFs."
                )
        else:
            _diag_parts.append(
                "The PDF appears to be text-based but no financial statement "
                "titles were found by any identification method."
            )

        methods_tried = ["Claude API"] if ANTHROPIC_API_KEY else []
        methods_tried.append("regex title matching")
        methods_tried.append("content-based scoring")
        _diag_parts.append(
            f"Methods tried: {', '.join(methods_tried)}."
        )

        if warnings:
            _diag_parts.append(f"Warnings: {'; '.join(warnings)}")

        raise ValueError(" ".join(_diag_parts))

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
    try:
        pnl = extract_pnl_docling(pdf_path, pages["pnl"])
    except Exception as e:
        logger.warning(f"[{job_id}] Docling failed: {e}, trying pymupdf4llm fallback")
        pnl = None

    # Fallback to pymupdf4llm-based extraction if Docling fails or extracts nothing
    if not pnl or not pnl.get('items'):
        try:
            from app.table_extractor import extract_pnl_from_tables
            logger.info(f"[{job_id}] Using pymupdf4llm fallback for P&L extraction")
            pnl = extract_pnl_from_tables(pdf_path, pages["pnl"])
        except Exception as e2:
            logger.warning(f"[{job_id}] pymupdf4llm fallback also failed: {e2}")

    if not pnl or not pnl.get('items'):
        _extract_diag = [
            f"Could not extract any P&L line items from page {pages['pnl'] + 1}.",
            f"PDF type: {pdf_type}.",
        ]
        if converted_pdf_path:
            _extract_diag.append(
                "Adobe OCR was used to convert the PDF, but the table structure "
                "could not be parsed by either Docling or pymupdf4llm."
            )
        elif pdf_type in ("scanned", "vector_outlined"):
            _extract_diag.append(
                "The PDF has no extractable text. Table extraction requires "
                "readable text. Configure Adobe OCR (ADOBE_CLIENT_ID / "
                "ADOBE_CLIENT_SECRET) to convert this PDF automatically."
            )
        else:
            _extract_diag.append(
                "Both Docling and pymupdf4llm failed to extract tables. "
                "The PDF table layout may be unsupported."
            )
        if warnings:
            _extract_diag.append(f"Warnings: {'; '.join(warnings)}")
        raise ValueError(" ".join(_extract_diag))

    if company_name:
        pnl['company'] = company_name
    if currency:
        pnl['currency'] = currency

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
        "claude_identified": claude_identified,
    }

    create_excel(data, excel_path, job_id=job_id)
    logger.info(f"[{job_id}] Excel generated: {excel_path}")

    # Clean up converted temp PDF if Adobe OCR was used
    if converted_pdf_path and os.path.exists(converted_pdf_path):
        try:
            os.unlink(converted_pdf_path)
            logger.info(f"[{job_id}] Cleaned up converted PDF: {converted_pdf_path}")
        except OSError:
            pass

    return {"excel_path": excel_path, "data": data, "warnings": warnings}


if __name__ == "__main__":
    import uvicorn

    from app.config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)
