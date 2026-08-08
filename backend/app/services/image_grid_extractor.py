import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from backend.app.config import get_settings
from backend.app.services.ocr import OCRTextLine, preprocess_document_image, recognize_image

logger = logging.getLogger(__name__)

DEFAULT_COLUMNS = ("column_1", "column_2", "product", "quantity_kg", "price", "lead_time", "moq", "unit")
HEADER_ALIASES = {
    "date": ("date",),
    "customer": ("customer", "client", "buyer"),
    "product": ("product", "item", "ingredient", "chemical", "material", "medicine", "api", "name"),
    "specification": ("specification", "spec", "description", "assay", "purity", "grade", "content"),
    "quantity": ("quantity", "qty", "stock", "available"),
    "unit": ("unit", "uom"),
    "price": ("price", "rate", "quote", "cost"),
    "currency": ("currency", "curr"),
    "moq": ("moq", "minimum"),
    "lead_time": ("lead", "delivery", "dispatch"),
}


@dataclass
class GridExtractionResult:
    horizontal_lines: list[int]
    vertical_lines: list[int]
    rows: list[dict[str, Any]]
    table_text: str


def group_positions(indices: np.ndarray, gap: int = 3, min_size: int = 1) -> list[int]:
    if len(indices) == 0:
        return []

    groups: list[list[int]] = [[int(indices[0])]]
    for value in indices[1:]:
        value = int(value)
        if value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [int(round(sum(group) / len(group))) for group in groups if len(group) >= min_size]


def detect_table_grid(image: Image.Image) -> tuple[list[int], list[int]]:
    gray = ImageOps.autocontrast(image.convert("L"))
    arr = np.array(gray)
    dark_pixels = arr < 95
    height, width = dark_pixels.shape

    row_candidates = np.where(dark_pixels.sum(axis=1) > width * 0.45)[0]
    col_candidates = np.where(dark_pixels.sum(axis=0) > height * 0.50)[0]
    horizontal = _filter_line_positions(group_positions(row_candidates, min_size=1), height)
    vertical = _filter_line_positions(group_positions(col_candidates, min_size=1), width)
    return horizontal, vertical


def _filter_line_positions(values: list[int], size: int) -> list[int]:
    filtered = [value for value in values if 0 <= value <= size]
    if not filtered:
        return []
    merged: list[int] = []
    for value in filtered:
        if merged and value - merged[-1] < max(5, int(size * 0.003)):
            merged[-1] = int((merged[-1] + value) / 2)
        else:
            merged.append(value)
    return merged


def extract_grid_table_from_pil_image(image: Image.Image, source_name: str = "image") -> GridExtractionResult | None:
    try:
        image = preprocess_document_image(ImageOps.exif_transpose(image).convert("RGB"))
        lines = recognize_image(image, source_name, preprocess=False)
        if not lines:
            return None

        horizontal, vertical = detect_table_grid(image)
        if len(horizontal) >= 4 and len(vertical) >= 3:
            result = _extract_bordered_table(lines, horizontal, vertical, source_name, image)
            if result:
                return result

        return _extract_unbordered_table(lines, source_name)
    except Exception:
        logger.debug("Tesseract table extraction not applicable for %s", source_name, exc_info=True)
        return None


def _extract_bordered_table(
    lines: list[OCRTextLine],
    horizontal: list[int],
    vertical: list[int],
    source_name: str,
    image: Image.Image,
) -> GridExtractionResult | None:
    rows: list[dict[str, Any]] = []
    header_cells = _grid_row_cells(lines, horizontal[0], horizontal[1], vertical, image)
    headers = _headers_from_cells(header_cells, len(vertical) - 1)

    for row_index in range(1, len(horizontal) - 1):
        top = horizontal[row_index]
        bottom = horizontal[row_index + 1]
        cell_values = _grid_row_cells(lines, top, bottom, vertical, image, headers)
        if not any(cell_values):
            continue
        rows.append(
            {
                "row_number": row_index,
                "bbox": {"left": vertical[0], "top": top, "right": vertical[-1], "bottom": bottom},
                "cells": {
                    headers[index]: cell_values[index] if index < len(cell_values) else ""
                    for index in range(len(headers))
                },
            }
        )

    product_rows = [row for row in rows if _row_has_catalogue_signal(row["cells"])]
    if len(product_rows) < 1:
        return None

    table_text = rows_to_catalog_table_text(product_rows)
    logger.info(
        "Tesseract bordered table extraction produced %s row(s) from %s",
        len(product_rows),
        source_name,
    )
    return GridExtractionResult(horizontal, vertical, product_rows, table_text)


def _grid_row_cells(
    lines: list[OCRTextLine],
    top: int,
    bottom: int,
    vertical: list[int],
    image: Image.Image | None = None,
    headers: list[str] | None = None,
) -> list[str]:
    cells: list[str] = []
    for column_index in range(len(vertical) - 1):
        left = vertical[column_index]
        right = vertical[column_index + 1]
        cell_lines = [
            line
            for line in lines
            if left <= line.center_x <= right and top <= line.center_y <= bottom
        ]
        text = " ".join(line.text for line in sorted(cell_lines, key=lambda value: (value.center_y, value.center_x))).strip()
        header = headers[column_index] if headers and column_index < len(headers) else ""
        if image is not None and _needs_cell_ocr(text, header):
            text = _ocr_grid_cell(image, left, top, right, bottom) or text
        cells.append(text)
    return cells


def _needs_cell_ocr(text: str, header: str) -> bool:
    cleaned = clean_text(text)
    if not cleaned:
        return header in {"product", "price", "quantity", "quantity_kg", "lead_time"}
    if header == "product" and re.search(r"[\[\]{}*]{2,}|CdSSC|Suess", cleaned, flags=re.IGNORECASE):
        return True
    if header == "price" and re.search(r"\b(?:CIF|FOB|EXW|CNF|C&F)\b", cleaned, flags=re.IGNORECASE) and not re.search(r"\$|USD|INR|Rs\.?|\d+\s*/\s*[A-Za-z]+", cleaned, flags=re.IGNORECASE):
        return True
    return False


def _ocr_grid_cell(image: Image.Image, left: int, top: int, right: int, bottom: int) -> str:
    try:
        import pytesseract

        settings = get_settings()
        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        pad = 4
        crop = image.crop(
            (
                max(0, left + pad),
                max(0, top + pad),
                min(image.width, right - pad),
                min(image.height, bottom - pad),
            )
        )
        crop = ImageOps.autocontrast(crop.convert("L")).convert("RGB")
        text = pytesseract.image_to_string(
            crop,
            lang=settings.tesseract_lang,
            config="--oem 1 --psm 7 -c preserve_interword_spaces=1",
        )
        return clean_text(text)
    except Exception:
        logger.debug("Cell OCR failed", exc_info=True)
        return ""


def _extract_unbordered_table(lines: list[OCRTextLine], source_name: str) -> GridExtractionResult | None:
    clustered_rows = _cluster_lines_by_y(lines)
    table_rows = [row for row in clustered_rows if len(row) >= 2]
    if len(table_rows) < 2:
        return None

    header_index = _best_header_row_index(table_rows)
    if header_index is None:
        return None
    header_row = table_rows[header_index]
    headers = _headers_from_cells([line.text for line in header_row], len(header_row))
    boundaries = _column_boundaries(header_row, lines)
    rows: list[dict[str, Any]] = []
    for row_number, row in enumerate(table_rows[header_index + 1 :], start=1):
        cells = _assign_lines_to_boundaries(row, boundaries)
        if not any(cells):
            continue
        mapped = {
            headers[index]: cells[index] if index < len(cells) else ""
            for index in range(len(headers))
        }
        if _row_has_catalogue_signal(mapped):
            rows.append({"row_number": row_number, "bbox": {}, "cells": mapped})

    if not rows:
        return None

    table_text = rows_to_catalog_table_text(rows)
    logger.info("Tesseract unbordered table extraction produced %s row(s) from %s", len(rows), source_name)
    return GridExtractionResult([], [int(boundary) for boundary in boundaries], rows, table_text)


def _cluster_lines_by_y(lines: list[OCRTextLine]) -> list[list[OCRTextLine]]:
    rows: list[list[OCRTextLine]] = []
    for line in sorted(lines, key=lambda value: (value.center_y, value.center_x)):
        height = max(10.0, line.box[3] - line.box[1])
        if rows and abs(rows[-1][0].center_y - line.center_y) <= height * 0.75:
            rows[-1].append(line)
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda value: value.center_x)
    return rows


def _best_header_row_index(rows: list[list[OCRTextLine]]) -> int | None:
    best_index = None
    best_score = 0
    for index, row in enumerate(rows[:8]):
        text = " ".join(line.text for line in row).lower()
        score = sum(
            1
            for aliases in HEADER_ALIASES.values()
            if any(alias in text for alias in aliases)
        )
        if score > best_score:
            best_score = score
            best_index = index
    return best_index if best_score >= 2 else None


def _column_boundaries(header_row: list[OCRTextLine], all_lines: list[OCRTextLine]) -> list[float]:
    centers = [line.center_x for line in header_row]
    min_left = min(line.box[0] for line in all_lines)
    max_right = max(line.box[2] for line in all_lines)
    boundaries = [min_left]
    for left, right in zip(centers, centers[1:]):
        boundaries.append((left + right) / 2)
    boundaries.append(max_right)
    return boundaries


def _assign_lines_to_boundaries(row: list[OCRTextLine], boundaries: list[float]) -> list[str]:
    cells = ["" for _ in range(max(0, len(boundaries) - 1))]
    for line in row:
        for index in range(len(boundaries) - 1):
            if boundaries[index] <= line.center_x <= boundaries[index + 1]:
                cells[index] = f"{cells[index]} {line.text}".strip()
                break
    return cells


def _headers_from_cells(cells: list[str], expected_count: int) -> list[str]:
    headers: list[str] = []
    for index in range(expected_count):
        raw = cells[index] if index < len(cells) else ""
        fallback = DEFAULT_COLUMNS[index] if index < len(DEFAULT_COLUMNS) else f"column_{index + 1}"
        headers.append(column_name_from_header(raw, fallback))
    return _dedupe_headers(headers)


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    deduped: list[str] = []
    for header in headers:
        seen[header] = seen.get(header, 0) + 1
        deduped.append(header if seen[header] == 1 else f"{header}_{seen[header]}")
    return deduped


def column_name_from_header(header: str, fallback: str) -> str:
    lowered = header.lower()
    if "quantity" in lowered and "kg" in lowered:
        return "quantity_kg"
    for column, aliases in HEADER_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return column
    return fallback


def clean_text(text: str) -> str:
    replacements = {
        "|": " ",
        "â€”": "-",
        "â€“": "-",
        "Ã¢â‚¬â€": "-",
        "Ã¢â‚¬â€œ": "-",
        "Ã¢â‚¬Â": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip(" -_.,")


def number_from_text(text: str) -> float | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if not match:
        return None
    token = match.group(0)
    if "," in token and "." not in token and re.search(r",\d{1,2}$", token):
        token = token.replace(",", ".")
    else:
        token = token.replace(",", "")
    return float(token)


def extract_quantity_parts(text: str) -> tuple[str, str, str, str]:
    quantity = ""
    quantity_unit = ""
    moq = ""
    pack_size = ""
    quantity_value = number_from_text(text)
    if quantity_value is not None:
        quantity = f"{quantity_value:g}"
    unit_match = re.search(r"\d[\d,]*(?:\.\d+)?\s*(kg|g|mg|l|ml|unit|units|pack|packs|bags?)\b", text or "", flags=re.IGNORECASE)
    if unit_match:
        quantity_unit = unit_match.group(1).lower().rstrip("s")

    moq_match = re.search(
        r"\bMOQ\s*:?\s*(\d[\d,]*(?:\.\d+)?)\s*(kg|g|mg|l|ml|unit|pack|bag)?",
        text or "",
        flags=re.IGNORECASE,
    )
    if moq_match:
        unit = moq_match.group(2) or ""
        moq = f"{moq_match.group(1)}{unit}"

    pack_match = re.search(
        r"(\d[\d,]*(?:\.\d+)?\s*(?:kg|g|mg|l|ml)\s+packing)",
        text or "",
        flags=re.IGNORECASE,
    )
    if pack_match:
        pack_size = pack_match.group(1)
    return quantity, quantity_unit, moq, pack_size


def extract_price_parts(text: str) -> tuple[str, str]:
    if re.search(r"\bN\s*/?\s*A\b|\bNA\b", text or "", flags=re.IGNORECASE):
        return "", ""
    currency = ""
    if re.search(r"\$|USD", text or "", flags=re.IGNORECASE):
        currency = "USD"
    elif re.search(r"â‚¹|INR|Rs\.?", text or "", flags=re.IGNORECASE):
        currency = "INR"
    elif re.search(r"â‚¬|EUR", text or "", flags=re.IGNORECASE):
        currency = "EUR"
    elif re.search(r"Â£|GBP", text or "", flags=re.IGNORECASE):
        currency = "GBP"
    if (
        re.search(r"\b(?:CIF|FOB|EXW|CNF|C&F)\b", text or "", flags=re.IGNORECASE)
        and not re.search(r"\d[\d,]*(?:\.\d+)?\s*/\s*[A-Za-z]+", text or "", flags=re.IGNORECASE)
    ):
        return "", currency
    match = re.search(
        r"((?:CIF|FOB|EXW|CNF|C&F)?\s*[A-Za-z ./-]*?(?:\$|USD|INR|Rs\.?|â‚¹|EUR|â‚¬|GBP|Â£\s*)?\s*\d[\d,]*(?:\.\d+)?(?:\s*/\s*[A-Za-z]+)?)",
        text or "",
        flags=re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1)), currency
    return clean_text(text), currency


def rows_to_catalog_table_text(rows: list[dict[str, Any]]) -> str:
    lines = ["Product | Specification | Qty | Unit | Price | Currency | Lead | MOQ | Pack | Notes"]
    for row in rows:
        cells = row["cells"]
        product = clean_text(cells.get("product", "") or cells.get("product_2", ""))
        if not product:
            continue
        specification = clean_text(cells.get("specification", ""))
        product, specification = split_inline_specification(product, specification)
        quantity_text = clean_text(cells.get("quantity", "") or cells.get("quantity_kg", ""))
        unit_text = clean_text(cells.get("unit", ""))
        price_text = clean_text(cells.get("price", ""))
        currency_text = clean_text(cells.get("currency", ""))
        lead_text = clean_text(cells.get("lead_time", ""))
        if lead_text and not re.search(r"\d|days?|weeks?|months?", lead_text, flags=re.IGNORECASE):
            lead_text = ""
        moq_text = clean_text(cells.get("moq", ""))
        quantity, quantity_unit, moq, pack_size = extract_quantity_parts(" ".join([quantity_text, moq_text]))
        price, currency = extract_price_parts(" ".join([price_text, currency_text]).strip())
        header_unit = "kg" if clean_text(cells.get("quantity_kg", "")) else ""
        notes = []
        if quantity_text:
            notes.append(f"original_quantity={quantity_text}")
        if specification:
            notes.append(f"specification={specification}")
        if price_text:
            notes.append(f"original_price={price_text}")
        if lead_text:
            notes.append(f"lead_time={lead_text}")
        lines.append(
            " | ".join(
                [
                    product,
                    specification,
                    quantity,
                    unit_text or quantity_unit or header_unit,
                    price,
                    currency or currency_text,
                    lead_text,
                    moq or moq_text,
                    pack_size,
                    "; ".join(notes),
                ]
            )
        )
    return "\n".join(lines)


def split_inline_specification(product: str, specification: str) -> tuple[str, str]:
    if specification:
        return product, specification
    match = re.match(r"^(?P<name>.+?)\s+(?P<spec>\d+(?:\.\d+)?\s*%.*)$", product)
    if not match:
        return product, specification
    name = clean_text(match.group("name"))
    spec = clean_text(match.group("spec"))
    if len(name) < 3:
        return product, specification
    return name, spec


def _row_has_catalogue_signal(cells: dict[str, str]) -> bool:
    product = cells.get("product") or cells.get("product_2") or ""
    if len(product.strip()) < 3:
        return False
    commercial = " ".join(str(value) for key, value in cells.items() if key != "product")
    return bool(re.search(r"\d|USD|INR|Rs\.?|\$|MOQ|kg|g\b|price|rate", commercial, flags=re.IGNORECASE))


def extract_grid_table_from_image(file_path: Path) -> GridExtractionResult | None:
    try:
        image = Image.open(file_path)
    except Exception:
        logger.debug("Unable to open %s for Tesseract table extraction", file_path, exc_info=True)
        return None
    return extract_grid_table_from_pil_image(image, file_path.name)
