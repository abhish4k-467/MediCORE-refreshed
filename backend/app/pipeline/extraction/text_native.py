"""Native PDF text extractor using PyMuPDF.

Owned by: pipeline/extraction/text_native.py
"""

import fitz
from backend.app.pipeline.normalization.schema import ExtractedBlock


def extract_native_text(page: fitz.Page) -> list[ExtractedBlock]:
    """Extract native text blocks from a PyMuPDF Page object with bounding boxes."""
    blocks: list[ExtractedBlock] = []

    # page.get_text("blocks") returns tuples: (x0, y0, x1, y1, "text_content", block_no, block_type)
    page_blocks = page.get_text("blocks") or []
    for b in page_blocks:
        if len(b) >= 5:
            x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
            text_content = str(b[4] or "").strip()
            block_type_num = b[6] if len(b) >= 7 else 0

            # Only include non-empty text blocks (block_type 0 is text, 1 is image)
            if text_content and block_type_num == 0:
                blocks.append(
                    ExtractedBlock(
                        type="text",
                        bbox=[x0, y0, x1, y1],
                        content=text_content,
                        confidence=1.0,
                        engine="pymupdf",
                    )
                )

    return blocks
