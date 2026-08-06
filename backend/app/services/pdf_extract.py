import io
import logging
from pathlib import Path

import fitz
import pdfplumber
from PIL import Image

try:
    import pytesseract
except ImportError:  # pragma: no cover - depends on local OCR install
    pytesseract = None

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
    if not combined_text or _pdf_has_images(pdf_path):
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
        return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()


def _pdf_has_images(pdf_path: Path) -> bool:
    try:
        with fitz.open(pdf_path) as doc:
            return any(page.get_images(full=True) for page in doc)
    except Exception:
        logger.debug("Could not inspect PDF images for %s", pdf_path.name, exc_info=True)
        return False


def _extract_with_ocr(pdf_path: Path) -> str:
    if pytesseract is None:
        logger.warning("Skipping OCR for %s because pytesseract is not installed", pdf_path.name)
        return ""

    text_parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                from backend.app.services.image_grid_extractor import extract_grid_table_from_pil_image
                grid_result = extract_grid_table_from_pil_image(image, f"{pdf_path.name} page {page_number}")
                if grid_result:
                    text_parts.append("[GRID CELL TABLE OCR]\n" + grid_result.table_text)
            except Exception:
                logger.debug("Grid-cell OCR failed for %s page %s", pdf_path.name, page_number, exc_info=True)

            try:
                page_text = pytesseract.image_to_string(image, config="--psm 6")
            except pytesseract.TesseractNotFoundError:
                logger.warning(
                    "Skipping OCR for %s because the Tesseract executable is not installed or not on PATH",
                    pdf_path.name,
                )
                return ""
            if page_text.strip():
                text_parts.append(page_text.strip())
            logger.info(
                "OCR extracted %s characters from %s page %s",
                len(page_text),
                pdf_path.name,
                page_number,
            )

    return "\n".join(text_parts).strip()
