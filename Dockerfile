FROM python:3.12-slim

WORKDIR /app

# System deps needed by Docling (torch, PDF parsing, etc.)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install opencv-python-headless FIRST so docling doesn't pull in the full opencv-python
RUN pip install --no-cache-dir opencv-python-headless && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Create uploads directory
RUN mkdir -p uploads

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
