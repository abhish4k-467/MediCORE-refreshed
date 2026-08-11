"""Page classifier to classify PDF pages into native_text, scanned, or mixed.

Replaces pdf-inspector using PyMuPDF text density and image coverage metrics.
Owned by: pipeline/classification/page_classifier.py
"""

from dataclasses import dataclass
from typing import Literal
import fitz

PageType = Literal["native_text", "scanned", "mixed"]


@dataclass
class PageClassification:
    page_number: int
    page_type: PageType
    confidence: float
    text_char_count: int
    image_count: int
    image_coverage_ratio: float


def classify_pdf_page(page: fitz.Page, page_number: int = 1) -> PageClassification:
    """Classify a PyMuPDF page into 'native_text', 'scanned', or 'mixed'."""
    text = page.get_text("text") or ""
    clean_text = "".join(text.split())
    text_char_count = len(clean_text)

    rect = page.rect
    page_area = max(1.0, rect.width * rect.height)

    images = page.get_images(full=True)
    image_count = len(images)

    total_image_area = 0.0
    for img_info in images:
        if len(img_info) >= 4:
            img_width = float(img_info[2])
            img_height = float(img_info[3])
            total_image_area += img_width * img_height

    image_coverage_ratio = min(1.0, total_image_area / page_area)

    if text_char_count >= 15 and image_count == 0:
        page_type: PageType = "native_text"
        confidence = 0.98
    elif text_char_count < 15 and image_count > 0:
        page_type = "scanned"
        confidence = 0.95
    elif text_char_count >= 30 and image_count > 0:
        page_type = "mixed"
        confidence = 0.90
    elif text_char_count >= 30:
        page_type = "native_text"
        confidence = 0.90
    else:
        page_type = "scanned"
        confidence = 0.75

    return PageClassification(
        page_number=page_number,
        page_type=page_type,
        confidence=confidence,
        text_char_count=text_char_count,
        image_count=image_count,
        image_coverage_ratio=image_coverage_ratio,
    )
