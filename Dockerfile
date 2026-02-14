FROM python:3.12-slim

WORKDIR /app

# System deps needed by Docling (torch, PDF parsing, etc.)
# libgl1: provides libGL.so.1 required by cv2/docling runtime
# libxcb1 + libx11-6 + libxext6 + libxrender1: needed by Pillow/pypdfium2 (via docling)
# tesseract-ocr + eng data: OCR support for scanned PDFs via PyMuPDF
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 \
        libgl1 \
        libxcb1 libx11-6 libxext6 libxrender1 \
        tesseract-ocr tesseract-ocr-eng && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install opencv-python-headless FIRST so docling doesn't pull in the full opencv-python
RUN pip install --no-cache-dir opencv-python-headless && \
    pip install --no-cache-dir -r requirements.txt

# Install curl for health check
RUN apt-get update -qq && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY . .

# Create uploads directory
RUN mkdir -p uploads

# Pre-download Docling's table extraction model during build
# so the first user request doesn't have to wait for model download
RUN python -c "from docling.document_converter import DocumentConverter; \
    from docling.datamodel.base_models import InputFormat; \
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode, TableStructureOptions; \
    from docling.document_converter import PdfFormatOption; \
    opts = PdfPipelineOptions(do_table_structure=True, \
        table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE)); \
    DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}); \
    print('Docling models downloaded')"

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
