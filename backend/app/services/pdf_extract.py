import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfInspectorExtraction:
    markdown: str
    pdf_type: str
    confidence: float
    page_count: int
    pages_needing_ocr: list[int]
    has_encoding_issues: bool


def extract_pdf_text(path: str | Path) -> str:
    pdf_path = Path(path)
    text_parts: list[str] = []

    inspection = _extract_with_pdf_inspector(pdf_path)
    if inspection and inspection.markdown:
        text_parts.append("[PDF INSPECTOR MARKDOWN]\n" + inspection.markdown)

    ocr_pages = _ocr_pages_for_pdf(pdf_path, inspection)
    if ocr_pages is None or ocr_pages:
        ocr_text = _extract_with_ocr(pdf_path, pages=ocr_pages)
        if ocr_text:
            text_parts.append(ocr_text)

    return "\n\n".join(dict.fromkeys(part.strip() for part in text_parts if part.strip()))


def _extract_with_pdf_inspector(pdf_path: Path) -> PdfInspectorExtraction | None:
    try:
        import pdf_inspector
    except Exception:
        logger.warning("pdf-inspector is not installed; falling back to OCR-only PDF extraction")
        return None

    try:
        result = pdf_inspector.process_pdf(str(pdf_path))
    except Exception:
        logger.warning("pdf-inspector failed for %s; falling back to OCR-only PDF extraction", pdf_path.name, exc_info=True)
        return None

    extraction = PdfInspectorExtraction(
        markdown=str(getattr(result, "markdown", "") or "").strip(),
        pdf_type=str(getattr(result, "pdf_type", "") or "").lower(),
        confidence=float(getattr(result, "confidence", 0.0) or 0.0),
        page_count=int(getattr(result, "page_count", 0) or 0),
        pages_needing_ocr=_normalize_pages_needing_ocr(getattr(result, "pages_needing_ocr", []) or []),
        has_encoding_issues=bool(getattr(result, "has_encoding_issues", False)),
    )
    logger.info(
        "pdf-inspector processed %s type=%s confidence=%.2f pages=%s pages_needing_ocr=%s encoding_issues=%s",
        pdf_path.name,
        extraction.pdf_type,
        extraction.confidence,
        extraction.page_count,
        extraction.pages_needing_ocr,
        extraction.has_encoding_issues,
    )
    return extraction


def _normalize_pages_needing_ocr(raw_pages: Any) -> list[int]:
    pages: list[int] = []
    for raw_page in raw_pages or []:
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            continue
        if page <= 0:
            page += 1
        pages.append(page)
    return sorted(set(pages))


def _ocr_pages_for_pdf(pdf_path: Path, inspection: PdfInspectorExtraction | None) -> list[int] | None:
    if inspection is None:
        return None
    if inspection.pdf_type in {"scanned", "image_based"}:
        return None
    if inspection.pages_needing_ocr:
        return inspection.pages_needing_ocr
    if inspection.has_encoding_issues and not inspection.markdown:
        return None
    if not inspection.markdown and inspection.pdf_type != "text_based":
        return None
    return []


def _extract_with_ocr(pdf_path: Path, pages: list[int] | None = None) -> str:
    requested_pages = set(pages or [])
    text_parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            if requested_pages and page_number not in requested_pages:
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                from backend.app.services.image_grid_extractor import extract_grid_table_from_pil_image
                grid_result = extract_grid_table_from_pil_image(image, f"{pdf_path.name} page {page_number}")
                if grid_result:
                    text_parts.append("[TESSERACT TABLE OCR]\n" + grid_result.table_text)
                    logger.info(
                        "Tesseract table OCR extracted %s characters from %s page %s",
                        len(grid_result.table_text),
                        pdf_path.name,
                        page_number,
                    )
                    continue
            except Exception:
                logger.debug("Tesseract table extraction failed for %s page %s", pdf_path.name, page_number, exc_info=True)

            try:
                from backend.app.services.ocr import recognize_image_to_text
                page_text = recognize_image_to_text(image, f"{pdf_path.name} page {page_number}")
            except Exception:
                logger.warning("Tesseract text extraction failed for %s page %s", pdf_path.name, page_number, exc_info=True)
                page_text = ""
            if page_text.strip():
                text_parts.append("[TESSERACT OCR]\n" + page_text.strip())
            logger.info(
                "Tesseract OCR extracted %s characters from %s page %s",
                len(page_text),
                pdf_path.name,
                page_number,
            )

    return "\n".join(text_parts).strip()
