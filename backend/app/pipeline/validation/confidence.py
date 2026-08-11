"""Validation module for confidence scoring and second-pass OCR retry fallback.

Owned by: pipeline/validation/confidence.py
"""

import logging
from PIL import Image

from backend.app.pipeline.config import default_config
from backend.app.pipeline.extraction.text_ocr import extract_text_with_ocr
from backend.app.pipeline.normalization.schema import ExtractedBlock
from backend.app.pipeline.preprocessing.preprocess import preprocess_for_ocr

logger = logging.getLogger(__name__)


def validate_and_retry_low_confidence_blocks(
    image: Image.Image,
    blocks: list[ExtractedBlock],
    min_confidence: float | None = None,
) -> list[ExtractedBlock]:
    """Flag blocks below confidence threshold and perform a secondary pass with modified scaling/contrast."""
    threshold = min_confidence if min_confidence is not None else default_config.min_block_confidence
    if not blocks:
        return blocks

    low_conf_blocks = [b for b in blocks if b.confidence < threshold]
    low_conf_rate = len(low_conf_blocks) / float(len(blocks))

    if low_conf_blocks:
        logger.info(
            "Low-confidence OCR detected: %s/%s blocks (%.1f%%) below %.2f threshold",
            len(low_conf_blocks),
            len(blocks),
            low_conf_rate * 100,
            threshold,
        )

    if low_conf_rate > 0.25:
        logger.info("Triggering second-pass OCR with enhanced contrast and smaller box threshold...")
        try:
            # Second-pass OCR with higher contrast boost and lower box threshold
            retry_img = preprocess_for_ocr(image, contrast_boost=2.0, denoise=True)
            retry_blocks = extract_text_with_ocr(
                retry_img,
                box_thresh=0.20,  # lower threshold for faint text
                preprocess=False,
                tile=True,
            )

            if retry_blocks:
                retry_avg_conf = sum(b.confidence for b in retry_blocks) / float(len(retry_blocks))
                orig_avg_conf = sum(b.confidence for b in blocks) / float(len(blocks))
                if retry_avg_conf > orig_avg_conf or len(retry_blocks) > len(blocks):
                    logger.info("Second-pass OCR succeeded (avg confidence %.2f -> %.2f)", orig_avg_conf, retry_avg_conf)
                    return retry_blocks
        except Exception as retry_err:
            logger.warning("Second-pass OCR retry failed: %s", retry_err)

    return blocks
