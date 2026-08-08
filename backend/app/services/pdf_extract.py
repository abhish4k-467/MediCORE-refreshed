import io
import logging
import re
from pathlib import Path

import fitz
import pdfplumber
from PIL import Image

logger = logging.getLogger(__name__)


def extract_pdf_text(path: str | Path) -> str:
    pdf_path = Path(path)
    text_parts: list[str] = []
    pymupdf_text = _extract_with_pymupdf(pdf_path)
    if pymupdf_text:
        text_parts.append(pymupdf_text)

    plumber_text = _extract_with_pdfplumber(pdf_path)
    if plumber_text:
        text_parts.append(plumber_text)

    combined_text = "\n\n".join(dict.fromkeys(part.strip() for part in text_parts if part.strip()))
    if not _native_pdf_text_sufficient(combined_text, pdf_path):
        ocr_text = _extract_with_ocr(pdf_path)
        if ocr_text:
            text_parts.append(ocr_text)

    return "\n\n".join(dict.fromkeys(part.strip() for part in text_parts if part.strip()))


def _extract_with_pymupdf(pdf_path: Path) -> str:
    text_parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text_parts.append(page.get_text("text"))
    return "\n".join(part.strip() for part in text_parts if part.strip())


def _extract_with_pdfplumber(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        text_parts: list[str] = []
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                rows = [
                    " | ".join(str(cell or "").strip() for cell in row)
                    for row in table
                    if row and any(str(cell or "").strip() for cell in row)
                ]
                if rows:
                    text_parts.append("[PDF NATIVE TABLE]\n" + "\n".join(rows))
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())
        return "\n".join(text_parts).strip()


def _pdf_has_images(pdf_path: Path) -> bool:
    try:
        with fitz.open(pdf_path) as doc:
            return any(page.get_images(full=True) for page in doc)
    except Exception:
        logger.debug("Could not inspect PDF images for %s", pdf_path.name, exc_info=True)
        return False


def _native_pdf_text_sufficient(text: str, pdf_path: Path) -> bool:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return False
    word_count = len(re.findall(r"[A-Za-z0-9]{2,}", cleaned))
    table_signals = cleaned.count("|") + len(re.findall(r"\b(?:price|qty|quantity|specification|MOQ|USD|INR|kg)\b", cleaned, re.IGNORECASE))
    if word_count >= 80 and table_signals >= 3:
        return True
    if word_count >= 200 and not _pdf_has_images(pdf_path):
        return True
    return False


def _extract_with_ocr(pdf_path: Path) -> str:
    text_parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                from backend.app.services.image_grid_extractor import extract_grid_table_from_pil_image
                grid_result = extract_grid_table_from_pil_image(image, f"{pdf_path.name} page {page_number}")
                if grid_result:
                    text_parts.append("[PADDLE TABLE OCR]\n" + grid_result.table_text)
            except Exception:
                logger.debug("PaddleOCR table extraction failed for %s page %s", pdf_path.name, page_number, exc_info=True)

            try:
                from backend.app.services.ocr import recognize_image_to_text
                page_text = recognize_image_to_text(image, f"{pdf_path.name} page {page_number}")
            except Exception:
                logger.warning("PaddleOCR text extraction failed for %s page %s", pdf_path.name, page_number, exc_info=True)
                page_text = ""
            if page_text.strip():
                text_parts.append("[PADDLE OCR]\n" + page_text.strip())
            logger.info(
                "PaddleOCR extracted %s characters from %s page %s",
                len(page_text),
                pdf_path.name,
                page_number,
            )

    return "\n".join(text_parts).strip()
