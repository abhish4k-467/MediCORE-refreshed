import re
from datetime import UTC, datetime

from backend.app.schemas import ExtractedCatalogItem, clean_optional_text

CATALOG_TABLE_PARSER_VERSION = "2026-07-22.vertical-catalog-v2"

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

PACK_PATTERN = re.compile(
    r"(?P<pack_size>\d+(?:\.\d+)?\s*(?:kg|g|mg|ml|l)\s+"
    r"(?:fibre drum|fiber drum|drum|bag|carton|strip|box|bottle|packing|packaging|pack))",
    re.IGNORECASE,
)
ROW_PATTERN = re.compile(
    r"^\s*(?P<ingredient>.+?)\s+"
    r"(?P<pack_size>\d+(?:\.\d+)?\s+(?:kg|g|mg|ml|l)\s+(?:fibre drum|fiber drum|drum|bag|carton|strip|box|bottle|pack))\s+"
    r"(?:INR|Rs\.?)\s*(?P<price>\d+(?:\.\d+)?)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s+(?P<unit>kg|g|mg|ml|l|units?|tabs?|tablets?|capsules?)\s+"
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})"
    r"(?:\s+(?P<status>.*))?\s*$",
    re.IGNORECASE,
)
QUOTE_ROW_PATTERN = re.compile(
    r"^\s*(?P<product>.+?)\s+"
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)"
    r"(?P<qty_extra>(?:\s+(?:(?:MOQ|M\.?O\.?Q\.?)\s*:?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*(?:kg|g|mg|ml|l|units?|packs?)\b|"
    r"\d[\d,]*(?:\.\d+)?\s*(?:kg|g|mg|ml|l|units?|packs?)\s*"
    r"(?:packing|packaging|pack)\b))*)"
    r"\s+"
    r"(?P<price_terms>(?:(?:CIF|FOB|EXW|CNF|C&F)\s+[A-Za-z ./-]+?\s+)*)"
    r"(?:(?P<currency>US\$|\$|USD|INR|Rs\.?|₹|EUR|€)\s*)?"
    r"(?P<price>\d[\d,]*(?:\.\d+)?)"
    r"\s*/\s*(?P<price_unit>[A-Za-z]+)"
    r"(?:\s+(?P<lead>\d+\s*(?:-|to)\s*\d+\s*days?|\d+\s*days?))?"
    r"\s*$",
    re.IGNORECASE,
)
HEADER_CURRENCY_PATTERN = re.compile(r"price\s*\(\s*(?P<currency>[A-Z$₹€]+)\s*\)", re.IGNORECASE)
HEADER_UNIT_PATTERN = re.compile(r"quantity\s*\(\s*(?P<unit>[A-Za-z]+)\s*\)", re.IGNORECASE)
MOQ_PATTERN = re.compile(
    r"\b(?:MOQ|M\.?O\.?Q\.?)\s*:?\s*(?P<moq>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>kg|g|mg|ml|l|units?|packs?)\b",
    re.IGNORECASE,
)
PRODUCT_CODE_PATTERN = re.compile(r"^[A-Z]{2,}\d{3,}[A-Z0-9-]*$")
STANDALONE_PRICE_PATTERN = re.compile(r"^(?:US\$|\$|USD|INR|Rs\.?|₹|EUR|€)?\s*\d[\d,]*(?:\.\d+)?\s*$", re.IGNORECASE)
FOOTER_OR_HEADER_PATTERN = re.compile(
    r"^(?:real-time raw material|sanyuan jinrui|tel:|add:|jinrui product code|product name|"
    r"product specification description|fob\s*\()",
    re.IGNORECASE,
)


def parse_catalog_table_text(
    text: str,
    reference_date: datetime | None = None,
) -> list[ExtractedCatalogItem]:
    items: list[ExtractedCatalogItem] = []
    seen: set[tuple[str, float, float, str]] = set()
    context = _table_context(text)
    reference_date = reference_date or datetime.now(UTC)
    for vertical_item in _parse_vertical_catalog_rows(text, context):
        key = _item_key(vertical_item)
        if key not in seen:
            seen.add(key)
            items.append(vertical_item)

    for table_item in _parse_generic_table(text, context):
        key = _item_key(table_item)
        if key not in seen:
            seen.add(key)
            items.append(table_item)

    for line in _candidate_lines(text):
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        match = ROW_PATTERN.match(cleaned)
        if match:
            month = MONTHS.get(match.group("month").lower())
            if not month:
                continue

            ingredient_name = match.group("ingredient").strip(" -")
            row_item = ExtractedCatalogItem(
                ingredient_name=ingredient_name,
                normalized_name=_normalize_name(ingredient_name),
                price_per_unit=float(match.group("price")),
                currency="INR",
                available_qty=float(match.group("qty").replace(",", "")),
                unit=_normalize_unit(match.group("unit")),
                valid_until=_infer_valid_until(int(match.group("day")), month, reference_date),
                notes=(match.group("status") or "").strip() or None,
            )
            if _item_key(row_item) not in seen:
                seen.add(_item_key(row_item))
                items.append(row_item)
            continue

        quote_item = _parse_quotation_row(cleaned, context)
        if quote_item and _item_key(quote_item) not in seen:
            seen.add(_item_key(quote_item))
            items.append(quote_item)
    return items


def _parse_vertical_catalog_rows(
    text: str,
    context: dict[str, str | None],
) -> list[ExtractedCatalogItem]:
    rows: list[ExtractedCatalogItem] = []
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    price_unit = _price_unit_context(lines) or context.get("quantity_unit") or "kg"
    currency = _vertical_currency_context(lines) or context.get("currency") or "USD"

    index = 0
    while index < len(lines):
        sku = lines[index]
        if not PRODUCT_CODE_PATTERN.match(sku):
            index += 1
            continue

        name_index = index + 1
        if name_index >= len(lines):
            break
        product_name = lines[name_index]
        if _looks_like_header(product_name) or FOOTER_OR_HEADER_PATTERN.search(product_name):
            index += 1
            continue

        spec_parts: list[str] = []
        price_text: str | None = None
        cursor = name_index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if PRODUCT_CODE_PATTERN.match(line):
                break
            if STANDALONE_PRICE_PATTERN.match(line):
                price_text = line
                cursor += 1
                break
            if not FOOTER_OR_HEADER_PATTERN.search(line):
                spec_parts.append(line)
            cursor += 1

        price = _number_from_text(price_text)
        if price is not None:
            raw_price = _format_original_price(price_text or str(price), currency, price_unit)
            source = " ".join([sku, product_name, *spec_parts, price_text or str(price)])
            notes = _notes(
                supplier_sku=sku,
                specification=" ".join(spec_parts).replace(";", ",") if spec_parts else None,
                original_price=raw_price,
                source=source[:500].replace(";", ","),
            )
            rows.append(
                ExtractedCatalogItem(
                    ingredient_name=product_name,
                    normalized_name=_normalize_name(product_name),
                    price_per_unit=price,
                    currency=currency,
                    available_qty=None,
                    unit=price_unit,
                    notes=notes,
                )
            )

        index = max(cursor, index + 1)
    return rows


def extract_pack_size(line: str) -> str | None:
    match = PACK_PATTERN.search(line)
    return match.group("pack_size") if match else None


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _normalize_name(name: str) -> str:
    normalized = name.lower().strip()
    normalized = re.sub(r"\s+api$", "", normalized)
    return normalized


def _normalize_unit(unit: str) -> str:
    unit = unit.lower().strip()
    if unit in {"kgs", "kilogram", "kilograms"}:
        return "kg"
    if unit in {"litre", "liter", "litres", "liters"}:
        return "l"
    if unit in {"packs", "pack"}:
        return "pack"
    if unit in {"units", "unit"}:
        return "unit"
    if unit in {"tabs", "tab", "tablets", "tablet"}:
        return "tablet"
    if unit in {"capsules", "capsule"}:
        return "capsule"
    return unit


def _infer_valid_until(day: int, month: int, reference_date: datetime) -> datetime:
    year = reference_date.year
    candidate = datetime(year, month, day, tzinfo=UTC)
    if candidate.date() < reference_date.date():
        candidate = datetime(year + 1, month, day, tzinfo=UTC)
    return candidate


def _table_context(text: str) -> dict[str, str | None]:
    context: dict[str, str | None] = {"currency": None, "quantity_unit": None}
    for line in text.splitlines():
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        currency_match = HEADER_CURRENCY_PATTERN.search(cleaned)
        if currency_match:
            context["currency"] = _currency_code(currency_match.group("currency"))
        unit_match = HEADER_UNIT_PATTERN.search(cleaned)
        if unit_match:
            context["quantity_unit"] = _normalize_unit(unit_match.group("unit"))
        if context["currency"] and context["quantity_unit"]:
            break
    return context


def _parse_quotation_row(line: str, context: dict[str, str | None]) -> ExtractedCatalogItem | None:
    if _price_occurrences(line) > 1:
        return None
    if " NA" in f" {line.upper()} " and "/" not in line:
        return None
    match = QUOTE_ROW_PATTERN.match(line)
    if not match:
        return None

    product = _strip_leading_date_customer(match.group("product"))
    if not product or _looks_like_header(product):
        return None

    qty = _number(match.group("qty"))
    price = _number(match.group("price"))
    if qty is None or price is None:
        return None

    price_unit = _normalize_unit(match.group("price_unit"))
    qty_unit = context.get("quantity_unit") or price_unit
    qty_extra = (match.group("qty_extra") or "").strip()
    price_terms = (match.group("price_terms") or "").strip()
    lead_text = (match.group("lead") or "").strip()
    currency = _currency_code(match.group("currency") or context.get("currency") or "INR")
    moq, moq_unit = _extract_moq(qty_extra)
    notes = _notes(
        original_quantity=f"{match.group('qty')} {qty_unit}".strip(),
        quantity_extra=qty_extra,
        original_price=_original_price(match, currency),
        price_terms=price_terms,
        lead_time=lead_text,
        moq_unit=moq_unit,
    )

    return ExtractedCatalogItem(
        ingredient_name=product,
        normalized_name=_normalize_name(product),
        price_per_unit=price,
        currency=currency,
        available_qty=qty,
        unit=qty_unit or price_unit,
        lead_time_days=_lead_time_days(lead_text),
        lead_time_text=lead_text or None,
        moq=moq,
        notes=notes,
    )


def _parse_generic_table(text: str, context: dict[str, str | None]) -> list[ExtractedCatalogItem]:
    rows: list[ExtractedCatalogItem] = []
    lines = [_clean_line(line) for line in text.splitlines() if _clean_line(line)]
    header: list[str] | None = None
    header_map: dict[str, int] = {}

    for line in lines:
        parts = _split_table_line(line)
        if len(parts) < 3:
            continue

        possible_map = _header_map(parts)
        if {"name", "price"}.issubset(possible_map) and ("qty" in possible_map or "unit" in possible_map):
            header = parts
            header_map = possible_map
            continue

        if not header or not header_map:
            continue

        if len(parts) < len(header):
            parts = parts + [""] * (len(header) - len(parts))

        name = _cell(parts, header_map.get("name"))
        if not name or _looks_like_header(name):
            continue

        price = _number_from_text(_cell(parts, header_map.get("price")))

        raw_qty = _cell(parts, header_map.get("qty"))
        qty = _number_from_text(raw_qty) if "qty" in header_map else None
        unit = _normalize_unit(
            _cell(parts, header_map.get("unit"))
            or _unit_from_text(raw_qty)
            or context.get("quantity_unit")
            or ""
        )
        currency = _currency_code(
            _cell(parts, header_map.get("currency"))
            or _currency_from_text(_cell(parts, header_map.get("price")))
            or context.get("currency")
            or "INR"
        )
        moq = _number_from_text(_cell(parts, header_map.get("moq"))) if "moq" in header_map else None
        lead_time_days = _lead_time_days(_cell(parts, header_map.get("lead_time")))
        notes_parts = []
        pack = clean_optional_text(_cell(parts, header_map.get("pack")))
        if pack:
            notes_parts.append(f"packaging={pack}")
        raw_price = clean_optional_text(_cell(parts, header_map.get("price")))
        if raw_price:
            notes_parts.append(f"original_price={raw_price}")
        raw_qty_note = clean_optional_text(raw_qty)
        if raw_qty_note:
            notes_parts.append(f"original_quantity={raw_qty_note}")
        raw_lead_time = clean_optional_text(_cell(parts, header_map.get("lead_time")))
        if raw_lead_time:
            notes_parts.append(f"lead_time={raw_lead_time}")

        rows.append(
            ExtractedCatalogItem(
                ingredient_name=name,
                normalized_name=_normalize_name(name),
                price_per_unit=price,
                currency=currency,
                available_qty=qty,
                unit=unit,
                lead_time_days=lead_time_days,
                lead_time_text=raw_lead_time,
                moq=moq,
                notes="; ".join(notes_parts) if notes_parts else None,
            )
        )

    return rows


def _split_table_line(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    if "\t" in stripped:
        return [part.strip() for part in stripped.split("\t")]
    if "|" in stripped:
        return [part.strip() for part in stripped.split("|")]
    if "," in stripped and len(stripped.split(",")) >= 3:
        return [part.strip() for part in stripped.split(",")]
    return [part.strip() for part in re.split(r"\s{2,}", stripped) if part.strip()]


def _header_map(parts: list[str]) -> dict[str, int]:
    aliases = {
        "name": ("product", "item", "ingredient", "chemical", "material", "medicine", "api", "name"),
        "qty": ("qty", "quantity", "stock", "available", "availability"),
        "unit": ("unit", "uom"),
        "price": ("price", "rate", "quote", "cost"),
        "currency": ("currency", "curr"),
        "moq": ("moq", "minimum order"),
        "lead_time": ("lead", "delivery", "dispatch"),
        "pack": ("pack", "packing", "packaging"),
    }
    mapped: dict[str, int] = {}
    for index, part in enumerate(parts):
        lowered = part.lower()
        for key, names in aliases.items():
            if key not in mapped and any(name in lowered for name in names):
                mapped[key] = index
    return mapped


def _cell(parts: list[str], index: int | None) -> str:
    if index is None or index >= len(parts):
        return ""
    return parts[index].strip()


def _currency_code(raw: str | None) -> str:
    value = (raw or "").strip().upper()
    if value in {"$", "US$", "USD"}:
        return "USD"
    if value in {"₹", "RS", "RS.", "INR"}:
        return "INR"
    if value in {"€", "EUR"}:
        return "EUR"
    return value or "INR"


def _currency_from_text(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"(US\$|\$|₹|€|(?<![A-Z])(?:USD|INR|EUR|Rs\.?)(?![A-Z]))", raw, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _vertical_currency_context(lines: list[str]) -> str | None:
    for line in lines[:30]:
        detected = _currency_from_text(line)
        if detected:
            return _currency_code(detected)
    return None


def _price_unit_context(lines: list[str]) -> str | None:
    for line in lines[:30]:
        match = re.search(r"/\s*(kg|g|mg|ml|l|litre|liter|units?|tabs?|tablets?|capsules?|packs?)\b", line, flags=re.IGNORECASE)
        if match:
            return _normalize_unit(match.group(1))
    return None


def _format_original_price(price_text: str, currency: str, price_unit: str) -> str:
    cleaned_price = (price_text or "").strip()
    if _currency_from_text(cleaned_price):
        prefix = ""
    elif currency == "USD":
        prefix = "$"
    elif currency == "INR":
        prefix = "INR "
    elif currency == "EUR":
        prefix = "EUR "
    else:
        prefix = f"{currency} "
    return f"{prefix}{cleaned_price}/{price_unit}".strip()


def _number(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _number_from_text(raw: str | None) -> float | None:
    if not clean_optional_text(raw):
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    return _number(match.group(0)) if match else None


def _unit_from_text(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?\s*(kg|kgs|g|mg|ml|l|litre|liter|units?|tabs?|tablets?|capsules?|packs?)\b", raw, flags=re.IGNORECASE)
    return _normalize_unit(match.group(1)) if match else None


def _extract_moq(text: str) -> tuple[float | None, str | None]:
    match = MOQ_PATTERN.search(text or "")
    if not match:
        return None, None
    return _number(match.group("moq")), _normalize_unit(match.group("unit"))


def _lead_time_days(text: str) -> int | None:
    if not text:
        return None
    if re.search(r"\d+\s*(?:-|–|to)\s*\d+", text, flags=re.IGNORECASE):
        return None
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def _original_price(match: re.Match[str], currency: str) -> str:
    raw_currency = match.group("currency") or currency
    return f"{raw_currency}{match.group('price')}/{match.group('price_unit')}"


def _notes(**parts: str | None) -> str | None:
    values = [f"{key}={value}" for key, value in parts.items() if value]
    return "; ".join(values) if values else None


def _strip_leading_date_customer(product: str) -> str:
    cleaned = re.sub(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+", "", product).strip()
    return re.sub(
        r"^[A-Za-z0-9&.,' -]{2,}?\b(?:Inc\.?|Ltd\.?|LLC|Corp\.?|Corporation|Pvt\.?\s+Ltd\.?)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()


def _looks_like_header(product: str) -> bool:
    lowered = product.lower()
    if re.match(r"^\d+\s*(?:-|to)\s*\d+\s*days?\b", lowered):
        return True
    return any(header in lowered for header in ("product", "customer", "quantity", "price"))


def _candidate_lines(text: str) -> list[str]:
    lines = [_clean_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    candidates: list[str] = []
    for index in range(len(lines)):
        max_end = min(index + 5, len(lines))
        for end in range(max_end, index, -1):
            candidates.append(" ".join(lines[index:end]))
    return candidates


def _item_key(item: ExtractedCatalogItem) -> tuple[str, float, float, str]:
    return (
        item.normalized_name or item.ingredient_name.lower(),
        float(item.available_qty or 0),
        float(item.price_per_unit or 0),
        item.currency,
    )


def _price_occurrences(line: str) -> int:
    return len(
        re.findall(
            r"(?:(?:US\$|\$|USD|INR|Rs\.?|₹|EUR|€)\s*)?\d[\d,]*(?:\.\d+)?\s*/\s*[A-Za-z]+",
            line,
            flags=re.IGNORECASE,
        )
    )
