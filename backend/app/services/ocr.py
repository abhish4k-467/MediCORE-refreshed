import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from backend.app.config import get_settings

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


def recognize_image(image: Image.Image, source_name: str = "image", *, preprocess: bool = True) -> list[OCRTextLine]:
    prepared = preprocess_document_image(image) if preprocess else ImageOps.exif_transpose(image).convert("RGB")
    prepared = _correct_orientation(prepared, source_name)
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("pytesseract is not installed; OCR cannot run.") from exc

    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    config = _tesseract_config(settings.tesseract_psm)
    try:
        data = pytesseract.image_to_data(
            prepared,
            lang=settings.tesseract_lang,
            config=config,
            output_type=Output.DICT,
        )
    except Exception:
        logger.warning("Tesseract OCR failed for %s", source_name, exc_info=True)
        return []

    lines = _words_from_tesseract_data(data)
    logger.info("Tesseract OCR recognized %s text line(s) from %s", len(lines), source_name)
    return lines


def recognize_image_to_text(image: Image.Image, source_name: str = "image") -> str:
    lines = recognize_image(image, source_name)
    rows = _cluster_lines_by_y(lines)
    return "\n".join(" ".join(line.text for line in row).strip() for row in rows if row).strip()


def _correct_orientation(image: Image.Image, source_name: str) -> Image.Image:
    settings = get_settings()
    if not settings.tesseract_enable_osd:
        return image
    try:
        import pytesseract

        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        osd = pytesseract.image_to_osd(image, lang=settings.tesseract_osd_lang)
        match = re.search(r"Rotate:\s*(\d+)", osd)
        angle = int(match.group(1)) if match else 0
        if angle:
            logger.info("Tesseract OSD rotating %s by %s degrees", source_name, angle)
            return image.rotate(-angle, expand=True)
    except Exception:
        logger.debug("Tesseract OSD orientation check failed for %s; using original orientation", source_name, exc_info=True)
    return image


def _tesseract_config(psm: int) -> str:
    return " ".join(
        [
            "--oem 1",
            f"--psm {psm}",
            "-c preserve_interword_spaces=1",
            "-c textord_tablefind_recognize_tables=1",
            "-c textord_tabfind_find_tables=1",
        ]
    )


def _words_from_tesseract_data(data: dict[str, list[Any]]) -> list[OCRTextLine]:
    words: list[OCRTextLine] = []
    total = len(data.get("text", []))
    for index in range(total):
        text = str(data["text"][index] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        left = float(data["left"][index])
        top = float(data["top"][index])
        right = left + float(data["width"][index])
        bottom = top + float(data["height"][index])
        words.append(OCRTextLine(text, conf, (left, top, right, bottom)))

    return sorted(words, key=lambda line: (line.center_y, line.center_x))


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
