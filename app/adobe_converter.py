"""
Adobe PDF Services OCR integration for converting non-searchable PDFs.

Handles two types of problem PDFs that PyMuPDF/Tesseract cannot read:
  - Scanned PDFs (each page is a photograph)
  - Vector-outlined PDFs (fonts converted to Bezier curves)

Adobe's OCR API renders each page as an image, runs OCR, and adds an
invisible text layer — producing a PDF that looks identical but has
extractable text. The entire existing pipeline then works unchanged.

Cost: ~$0.05 per document (free tier: 500/month).
"""

import logging
import tempfile

from app.config import ADOBE_CLIENT_ID, ADOBE_CLIENT_SECRET

logger = logging.getLogger(__name__)


def is_adobe_available() -> bool:
    """Check if Adobe PDF Services credentials are configured."""
    return bool(ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET)


def convert_to_searchable_pdf(input_pdf_path: str) -> str:
    """
    Send a PDF to Adobe's OCR API and get back a searchable PDF.

    How it works:
      1. Upload your PDF to Adobe's cloud
      2. Adobe renders every page as an image internally
      3. Adobe runs OCR on those images
      4. Adobe adds an invisible text layer on top of the original
      5. You download the result — looks identical, but now has text

    Args:
        input_pdf_path: Path to the non-searchable PDF

    Returns:
        Path to a new searchable PDF (temp file — caller must clean up)

    Raises:
        RuntimeError: If Adobe API is not configured or SDK not installed
    """
    if not is_adobe_available():
        raise RuntimeError(
            "Adobe PDF Services API credentials not configured. "
            "Set ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET environment variables. "
            "Get free credentials at: "
            "https://acrobatservices.adobe.com/dc-integration-creation-app-cdn/main.html"
        )

    try:
        from adobe.pdfservices.operation.auth.service_principal_credentials import (
            ServicePrincipalCredentials,
        )
        from adobe.pdfservices.operation.io.stream_asset import StreamAsset
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from adobe.pdfservices.operation.pdf_services_media_type import (
            PDFServicesMediaType,
        )
        from adobe.pdfservices.operation.pdfjobs.jobs.ocr_pdf_job import OCRPDFJob
        from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_params import (
            OCRParams,
        )
        from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_locale import (
            OCRSupportedLocale,
        )
        from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_type import (
            OCRSupportedType,
        )
        from adobe.pdfservices.operation.pdfjobs.result.ocr_pdf_result import (
            OCRResult,
        )
    except ImportError:
        raise RuntimeError(
            "Adobe PDF Services SDK not installed. "
            "Run: pip install pdfservices-sdk"
        )

    # Authenticate
    credentials = ServicePrincipalCredentials(
        client_id=ADOBE_CLIENT_ID,
        client_secret=ADOBE_CLIENT_SECRET,
    )
    pdf_services = PDFServices(credentials=credentials)

    # Upload the PDF
    logger.info(f"[Adobe OCR] Uploading {input_pdf_path} to Adobe cloud...")
    with open(input_pdf_path, "rb") as f:
        input_asset = pdf_services.upload(
            input_stream=f,
            mime_type=PDFServicesMediaType.PDF,
        )

    # Create and submit the OCR job
    # SEARCHABLE_IMAGE_EXACT keeps the original pixel-perfect and adds
    # an invisible text layer on top.
    # EN_US locale works for Indian annual reports (financial terms are English).
    ocr_params = OCRParams(
        ocr_locale=OCRSupportedLocale.EN_US,
        ocr_type=OCRSupportedType.SEARCHABLE_IMAGE_EXACT,
    )
    ocr_job = OCRPDFJob(input_asset=input_asset, ocr_params=ocr_params)

    logger.info("[Adobe OCR] Submitting OCR job (this takes 15-45 seconds)...")
    location = pdf_services.submit(ocr_job)

    # Wait for result (SDK handles polling internally)
    pdf_services_response = pdf_services.get_job_result(location, OCRResult)

    # Download the searchable PDF
    result_asset = pdf_services_response.get_result().get_asset()
    stream_asset: StreamAsset = pdf_services.get_content(result_asset)

    output_file = tempfile.NamedTemporaryFile(
        suffix="_searchable.pdf", delete=False
    )
    output_path = output_file.name
    with open(output_path, "wb") as out_f:
        out_f.write(stream_asset.get_input_stream().read())

    logger.info(f"[Adobe OCR] Searchable PDF saved to: {output_path}")
    return output_path
