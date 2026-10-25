FROM python:3.12-slim

# Tesseract for OCR on scanned invoices.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY eval ./eval
COPY data/__init__.py data/generate.py ./data/
COPY scripts ./scripts
RUN pip install --no-cache-dir ".[ocr]"

RUN useradd --create-home --uid 10001 apagent && mkdir -p /app/data/generated /app/.storage \
    && chown -R apagent /app
USER apagent

ENV PYTHONUNBUFFERED=1 OCR_BACKEND=tesseract PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
