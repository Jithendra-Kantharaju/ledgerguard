"""Turn a PDF into text.

Digital PDFs: PyMuPDF text layer (or Docling when USE_DOCLING=true).
Scanned PDFs (no text layer): OCR when OCR_BACKEND=tesseract, otherwise return
empty text with is_scanned=True so the router sends the document to a human.
"""

from __future__ import annotations

import hashlib

import pymupdf as fitz

from app.schemas import ParsedDocument
from app.settings import get_settings

MIN_TEXT_CHARS = 40


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_document(data: bytes) -> ParsedDocument:
    settings = get_settings()
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        text = "\n".join(page.get_text("text") for page in doc)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            if settings.use_docling:
                return _parse_docling(data)
            return ParsedDocument(text=text, is_scanned=False, ocr_confidence=None, method="pymupdf")
        if settings.ocr_backend == "tesseract":
            return _ocr_tesseract(doc)
        return ParsedDocument(text="", is_scanned=True, ocr_confidence=0.0, method="none")
    finally:
        doc.close()


def _parse_docling(data: bytes) -> ParsedDocument:
    import io

    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(DocumentStream(name="invoice.pdf", stream=io.BytesIO(data)))
    return ParsedDocument(
        text=result.document.export_to_markdown(), is_scanned=False, ocr_confidence=None, method="docling"
    )


def _ocr_tesseract(doc: fitz.Document) -> ParsedDocument:
    import io

    import pytesseract
    from PIL import Image

    texts: list[str] = []
    confidences: list[float] = []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        words = []
        for word, conf in zip(data["text"], data["conf"], strict=True):
            if word.strip():
                words.append(word)
                if float(conf) >= 0:
                    confidences.append(float(conf) / 100)
        texts.append(pytesseract.image_to_string(image))
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return ParsedDocument(text="\n".join(texts), is_scanned=True, ocr_confidence=mean_conf, method="tesseract")
