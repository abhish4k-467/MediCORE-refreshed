import logging
from dataclasses import dataclass
from typing import Any
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OCRTextLine:
    text: str
    score: float
    box: tuple[float, float, float, float]

    @property
    def center_x(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def center_y(self) -> float:
        return (self.box[1] + self.box[3]) / 2


def preprocess_document_image(image: Image.Image, *, scale_small_images: bool = True) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    gray = ImageOps.autocontrast(image.convert("L"))
    if scale_small_images and max(gray.size) < 2200:
        scale = min(3, max(2, int(2200 / max(gray.size))))
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
    gray = ImageEnhance.Contrast(gray).enhance(1.7)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray.convert("RGB")


def _words_from_rapidocr_result(result: Any) -> list[OCRTextLine]:
    if not result:
        return []
    words: list[OCRTextLine] = []
    for item in result:
        if not item or len(item) < 3:
            continue
        box_points, text, conf = item[0], str(item[1] or "").strip(), float(item[2] or 0.0)
        if not text or conf < 0.15:
            continue
        xs = [float(pt[0]) for pt in box_points]
        ys = [float(pt[1]) for pt in box_points]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        words.append(OCRTextLine(text, conf, (left, top, right, bottom)))
    return sorted(words, key=lambda line: (line.center_y, line.center_x))


def recognize_image(image: Image.Image, source_name: str = "image", *, preprocess: bool = True) -> list[OCRTextLine]:
    prepared = preprocess_document_image(image) if preprocess else ImageOps.exif_transpose(image).convert("RGB")

    try:
        from rapidocr_onnxruntime import RapidOCR
        import numpy as np

        if not hasattr(recognize_image, "_rapidocr_engine"):
            setattr(recognize_image, "_rapidocr_engine", RapidOCR())
        engine = getattr(recognize_image, "_rapidocr_engine")
        np_img = np.array(prepared)
        result, _ = engine(np_img)
        lines = _words_from_rapidocr_result(result)
        if lines:
            logger.info("RapidOCR recognized %s text line(s) from %s", len(lines), source_name)
            return lines
    except Exception as rapid_err:
        logger.debug("RapidOCR failed for %s: %s", source_name, rapid_err)

    logger.warning("RapidOCR returned 0 lines for %s", source_name)
    return []


def recognize_image_to_text(image: Image.Image, source_name: str = "image") -> str:
    lines = recognize_image(image, source_name)
    if lines:
        rows = _cluster_lines_by_y(lines)
        text = "\n".join(" ".join(line.text for line in row).strip() for row in rows if row).strip()
        if text:
            return text

    return ""

def _cluster_lines_by_y(lines: list[OCRTextLine]) -> list[list[OCRTextLine]]:
    rows: list[list[OCRTextLine]] = []
    for line in sorted(lines, key=lambda value: (value.center_y, value.center_x)):
        height = max(8.0, line.box[3] - line.box[1])
        if rows and abs(rows[-1][0].center_y - line.center_y) <= height * 0.65:
            rows[-1].append(line)
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda value: value.center_x)
    return rows
