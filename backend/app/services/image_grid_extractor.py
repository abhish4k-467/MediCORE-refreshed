import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

DEFAULT_COLUMNS = ("date", "customer", "product", "quantity", "price", "lead_time")
HEADER_ALIASES = {
    "date": ("date",),
    "customer": ("customer", "buyer", "client"),
    "product": ("product", "item", "ingredient", "chemical", "material", "medicine", "api", "name"),
    "quantity": ("quantity", "qty", "stock", "available"),
    "price": ("price", "rate", "quote", "cost"),
    "lead_time": ("lead", "delivery", "dispatch"),
}


@dataclass
class GridExtractionResult:
    horizontal_lines: list[int]
    vertical_lines: list[int]
    rows: list[dict[str, Any]]
    table_text: str


def group_positions(indices: np.ndarray, gap: int = 2) -> list[int]:
    if len(indices) == 0:
        return []

    groups: list[list[int]] = [[int(indices[0])]]
    for value in indices[1:]:
        value = int(value)
        if value - groups[-1][-1] <= gap:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [int(round(sum(group) / len(group))) for group in groups]


def detect_table_grid(image: Image.Image) -> tuple[list[int], list[int]]:
    gray = np.array(image.convert("L"))
    dark_pixels = gray < 80
    height, width = dark_pixels.shape

    row_candidates = np.where(dark_pixels.sum(axis=1) > width * 0.35)[0]
    col_candidates = np.where(dark_pixels.sum(axis=0) > height * 0.25)[0]
    return group_positions(row_candidates), group_positions(col_candidates)


def crop_cell(image: Image.Image, left: int, top: int, right: int, bottom: int) -> Image.Image:
    pad_x = 4
    pad_y = 3
    return image.crop((left + pad_x, top + pad_y, right - pad_x, bottom - pad_y))


def preprocess_for_ocr(cell: Image.Image, scale: int = 4) -> Image.Image:
    cell = ImageOps.autocontrast(cell.convert("L"))
    cell = cell.resize((cell.width * scale, cell.height * scale), Image.Resampling.LANCZOS)
    cell = cell.filter(ImageFilter.SHARPEN)
    arr = np.array(cell)
    arr = np.where(arr > 180, 255, 0).astype(np.uint8)
    return Image.fromarray(arr)


def clean_text(text: str) -> str:
    replacements = {
        "|": " ",
        "—": "-",
        "–": "-",
        "k¢": "kg",
        "K¢": "kg",
        "Phama": "Pharma",
        "â€”": "-",
        "â€“": "-",
        "â€": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.,")


def ocr_cell(cell: Image.Image, psm: int = 6) -> str:
    prepared = preprocess_for_ocr(cell)
    raw = pytesseract.image_to_string(prepared, config=f"--oem 3 --psm {psm}")
    return clean_text(raw)


def column_name_from_header(header: str, fallback: str) -> str:
    lowered = header.lower()
    for column, aliases in HEADER_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return column
    return fallback


def number_from_text(text: str) -> float | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def extract_quantity_parts(text: str) -> tuple[str, str, str, str]:
    quantity = ""
    quantity_unit = ""
    moq = ""
    pack_size = ""
    quantity_value = number_from_text(text)
    if quantity_value is not None:
        quantity = f"{quantity_value:g}"
    unit_match = re.search(r"\d[\d,]*(?:\.\d+)?\s*(kg|g|mg|l|ml|unit|units|pack|packs)\b", text or "", flags=re.IGNORECASE)
    if unit_match:
        quantity_unit = unit_match.group(1).lower()

    moq_match = re.search(
        r"\bMOQ\s*:?\s*(\d[\d,]*(?:\.\d+)?)\s*(kg|g|mg|l|ml|unit|pack)?",
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
    elif re.search(r"₹|INR|Rs\.?", text or "", flags=re.IGNORECASE):
        currency = "INR"
    elif re.search(r"€|EUR", text or "", flags=re.IGNORECASE):
        currency = "EUR"
    elif re.search(r"£|GBP", text or "", flags=re.IGNORECASE):
        currency = "GBP"
    match = re.search(
        r"((?:CIF|FOB|EXW|CNF|C&F)?\s*[A-Za-z ./-]*?(?:\$|USD|INR|Rs\.?|₹|EUR|€|GBP|£\s*)?\s*\d[\d,]*(?:\.\d+)?(?:\s*/\s*[A-Za-z]+)?)",
        text or "",
        flags=re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1)), currency
    return text, currency


def ocr_table_cells(image: Image.Image, horizontal: list[int], vertical: list[int]) -> list[dict[str, Any]]:
    header_top = horizontal[0]
    header_bottom = horizontal[1]
    table_bottom = horizontal[-1]

    fallback_columns = list(DEFAULT_COLUMNS[: max(0, len(vertical) - 1)])
    headers: list[str] = []
    for index in range(len(vertical) - 1):
        header_text = ocr_cell(crop_cell(image, vertical[index], header_top, vertical[index + 1], header_bottom), psm=7)
        fallback = fallback_columns[index] if index < len(fallback_columns) else f"column_{index + 1}"
        headers.append(column_name_from_header(header_text, fallback))

    date_text = ""
    customer_text = ""
    if "date" in headers:
        idx = headers.index("date")
        date_text = ocr_cell(crop_cell(image, vertical[idx], header_bottom, vertical[idx + 1], table_bottom), psm=6)
    if "customer" in headers:
        idx = headers.index("customer")
        customer_text = ocr_cell(crop_cell(image, vertical[idx], header_bottom, vertical[idx + 1], table_bottom), psm=6)

    rows: list[dict[str, Any]] = []
    for row_index in range(1, len(horizontal) - 1):
        top = horizontal[row_index]
        bottom = horizontal[row_index + 1]
        cell_text: dict[str, str] = {"date": date_text, "customer": customer_text}
        for column_index, column_name in enumerate(headers):
            if column_name in {"date", "customer"}:
                continue
            cell = crop_cell(image, vertical[column_index], top, vertical[column_index + 1], bottom)
            psm = 6 if column_name in {"product", "price"} else 7
            cell_text[column_name] = ocr_cell(cell, psm=psm)
        rows.append(
            {
                "row_number": row_index,
                "bbox": {"left": vertical[0], "top": top, "right": vertical[-1], "bottom": bottom},
                "cells": cell_text,
            }
        )
    return rows


def rows_to_catalog_table_text(rows: list[dict[str, Any]]) -> str:
    lines = ["Product | Qty | Unit | Price | Currency | Lead | MOQ | Pack | Notes"]
    for row in rows:
        cells = row["cells"]
        product = cells.get("product", "")
        if not product:
            continue
        quantity_text = cells.get("quantity", "")
        price_text = cells.get("price", "")
        lead_text = cells.get("lead_time", "")
        quantity, quantity_unit, moq, pack_size = extract_quantity_parts(quantity_text)
        price, currency = extract_price_parts(price_text)
        notes = []
        if quantity_text:
            notes.append(f"original_quantity={quantity_text}")
        if price_text:
            notes.append(f"original_price={price_text}")
        if lead_text and re.search(r"\d", lead_text):
            notes.append(f"lead_time={lead_text}")
        lines.append(
            " | ".join(
                [
                    product,
                    quantity,
                    quantity_unit,
                    price,
                    currency,
                    lead_text if re.search(r"\d", lead_text or "") else "",
                    moq,
                    pack_size,
                    "; ".join(notes),
                ]
            )
        )
    return "\n".join(lines)


def extract_grid_table_from_pil_image(image: Image.Image, source_name: str = "image") -> GridExtractionResult | None:
    try:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        horizontal, vertical = detect_table_grid(image)
        if len(horizontal) < 4 or len(vertical) < 4:
            return None
        row_count = len(horizontal) - 2
        column_count = len(vertical) - 1
        if row_count < 2 or column_count < 3:
            return None

        rows = ocr_table_cells(image, horizontal, vertical)
        product_rows = [row for row in rows if row["cells"].get("product")]
        if len(product_rows) < 2:
            return None

        table_text = rows_to_catalog_table_text(product_rows)
        logger.info(
            "Grid-cell OCR extracted %s rows from %s using %s columns",
            len(product_rows),
            source_name,
            column_count,
        )
        return GridExtractionResult(
            horizontal_lines=horizontal,
            vertical_lines=vertical,
            rows=product_rows,
            table_text=table_text,
        )
    except Exception:
        logger.debug("Grid-cell OCR not applicable for %s", source_name, exc_info=True)
        return None


def extract_grid_table_from_image(file_path: Path) -> GridExtractionResult | None:
    try:
        image = Image.open(file_path)
    except Exception:
        logger.debug("Unable to open %s for grid-cell OCR", file_path, exc_info=True)
        return None
    return extract_grid_table_from_pil_image(image, file_path.name)
