import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
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
    gray = ImageEnhance.Contrast(gray).enhance(1.6)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray.convert("RGB")


@lru_cache(maxsize=1)
def _paddle_ocr() -> Any:
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("PaddleOCR is not installed; OCR cannot run.") from exc

    settings = get_settings()
    common_kwargs = {
        "lang": settings.paddleocr_lang,
        "device": "cpu",
        "cpu_threads": settings.paddleocr_cpu_threads,
        "enable_mkldnn": True,
        "mkldnn_cache_capacity": 10,
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
        "text_detection_model_name": settings.paddleocr_det_model,
        "text_recognition_model_name": settings.paddleocr_rec_model,
        "text_recognition_batch_size": settings.paddleocr_rec_batch_size,
        "text_det_limit_side_len": settings.paddleocr_det_limit_side_len,
        "text_det_limit_type": "max",
        "text_det_thresh": 0.25,
        "text_det_box_thresh": 0.45,
        "text_det_unclip_ratio": 1.7,
    }
    try:
        return PaddleOCR(**common_kwargs)
    except TypeError:
        logger.info("Falling back to PaddleOCR 2.x-compatible CPU initialization")
        return PaddleOCR(
            lang=settings.paddleocr_lang,
            use_gpu=False,
            use_angle_cls=True,
            enable_mkldnn=True,
            cpu_threads=settings.paddleocr_cpu_threads,
            det_limit_side_len=settings.paddleocr_det_limit_side_len,
            det_limit_type="max",
            rec_batch_num=settings.paddleocr_rec_batch_size,
        )


def recognize_image(image: Image.Image, source_name: str = "image") -> list[OCRTextLine]:
    prepared = preprocess_document_image(image)
    try:
        ocr = _paddle_ocr()
        if hasattr(ocr, "predict"):
            raw_result = ocr.predict(np.array(prepared))
        else:
            raw_result = ocr.ocr(np.array(prepared), cls=True)
    except Exception:
        logger.warning("PaddleOCR failed for %s", source_name, exc_info=True)
        return []

    lines = _parse_paddle_result(raw_result)
    logger.info("PaddleOCR recognized %s text line(s) from %s", len(lines), source_name)
    return lines


def recognize_image_to_text(image: Image.Image, source_name: str = "image") -> str:
    lines = recognize_image(image, source_name)
    rows = _cluster_lines_by_y(lines)
    return "\n".join(" ".join(line.text for line in row).strip() for row in rows if row).strip()


def _parse_paddle_result(raw_result: Any) -> list[OCRTextLine]:
    parsed: list[OCRTextLine] = []
    for page in _page_results(raw_result):
        mapping = _result_mapping(page)
        if mapping:
            texts = mapping.get("rec_texts") or mapping.get("texts") or []
            scores = mapping.get("rec_scores") or mapping.get("scores") or []
            boxes = mapping.get("rec_boxes") or mapping.get("dt_boxes") or mapping.get("rec_polys") or mapping.get("dt_polys") or []
            for text, score, box in zip(texts, scores or [1.0] * len(texts), boxes):
                line = _line_from_parts(text, score, box)
                if line:
                    parsed.append(line)
            continue

        parsed.extend(_parse_legacy_page_result(page))
    return sorted(parsed, key=lambda line: (line.center_y, line.center_x))


def _page_results(raw_result: Any) -> list[Any]:
    if raw_result is None:
        return []
    if isinstance(raw_result, list):
        return raw_result
    return [raw_result]


def _result_mapping(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result.get("res") if isinstance(result.get("res"), dict) else result
    json_value = getattr(result, "json", None)
    if isinstance(json_value, dict):
        return json_value.get("res") if isinstance(json_value.get("res"), dict) else json_value
    if callable(json_value):
        try:
            value = json_value()
            if isinstance(value, dict):
                return value.get("res") if isinstance(value.get("res"), dict) else value
        except Exception:
            return None
    return None


def _parse_legacy_page_result(page: Any) -> list[OCRTextLine]:
    lines: list[OCRTextLine] = []
    if not isinstance(page, list):
        return lines
    for item in page:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        box = item[0]
        text_score = item[1]
        if not isinstance(text_score, (list, tuple)) or not text_score:
            continue
        text = text_score[0]
        score = text_score[1] if len(text_score) > 1 else 1.0
        line = _line_from_parts(text, score, box)
        if line:
            lines.append(line)
    return lines


def _line_from_parts(text: Any, score: Any, box: Any) -> OCRTextLine | None:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        numeric_score = 1.0
    bbox = _bbox(box)
    if not bbox:
        return None
    return OCRTextLine(cleaned, numeric_score, bbox)


def _bbox(box: Any) -> tuple[float, float, float, float] | None:
    try:
        arr = np.array(box, dtype=float)
    except Exception:
        return None
    if arr.ndim == 1 and arr.size >= 4:
        left, top, right, bottom = arr[:4]
        return float(left), float(top), float(right), float(bottom)
    if arr.ndim >= 2 and arr.shape[-1] >= 2:
        xs = arr[..., 0].reshape(-1)
        ys = arr[..., 1].reshape(-1)
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())
    return None


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
