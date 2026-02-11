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

COPY . .

# Create uploads directory
RUN mkdir -p uploads

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
