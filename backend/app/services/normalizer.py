from backend.app.schemas import ExtractedCatalogItem, clean_optional_text


UNIT_ALIASES = {
    "pcs": "unit",
    "piece": "unit",
    "pieces": "unit",
    "units": "unit",
    "tabs": "tablet",
    "tablet": "tablet",
    "tablets": "tablet",
    "kg": "kg",
    "kilogram": "kg",
    "g": "g",
    "gram": "g",
}


def normalize_item(item: ExtractedCatalogItem) -> ExtractedCatalogItem:
    unit = None
    cleaned_unit = clean_optional_text(item.unit)
    if cleaned_unit:
        raw_unit = cleaned_unit.strip().lower()
        unit = UNIT_ALIASES.get(raw_unit, raw_unit)
    return item.model_copy(
        update={
            "unit": unit,
            "currency": (clean_optional_text(item.currency) or "INR").upper(),
            "specification": clean_optional_text(item.specification),
            "lead_time_text": clean_optional_text(item.lead_time_text),
            "notes": clean_optional_text(item.notes),
        }
    )
