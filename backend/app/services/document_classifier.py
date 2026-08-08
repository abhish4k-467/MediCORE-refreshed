import re
from dataclasses import dataclass


CATALOGUE = "catalogue"
CERTIFICATE = "certificate"
OTHER = "other"

CERTIFICATE_TERMS = (
    "certificate of analysis",
    "analysis certificate",
    "certificate",
    "coa",
    "c of a",
    "lab report",
    "laboratory report",
    "test report",
    "quality report",
    "halal",
    "kosher",
    "organic certificate",
    "gmp",
    "cgmp",
    "iso",
    "msds",
)
CATALOGUE_TERMS = (
    "catalog",
    "catalogue",
    "price list",
    "quotation",
    "quote",
    "offer",
    "inventory",
    "stock list",
    "available stock",
    "rate list",
    "fob",
    "cif",
    "exw",
)
COMMERCIAL_TERMS = (
    "price",
    "rate",
    "usd",
    "inr",
    "rs.",
    "$",
    "moq",
    "quantity",
    "qty",
    "lead time",
    "delivery",
)


@dataclass(frozen=True)
class DocumentClassification:
    category: str
    confidence: float
    material_hint: str | None = None


def classify_document(filename: str, ext: str, text: str | None) -> DocumentClassification:
    haystack = f"{filename}\n{text or ''}".lower()
    filename_lower = filename.lower()
    certificate_score = _term_score(haystack, CERTIFICATE_TERMS)
    catalogue_score = _term_score(haystack, CATALOGUE_TERMS) + _term_score(haystack, COMMERCIAL_TERMS)

    table_like_rows = len(
        [
            line
            for line in (text or "").splitlines()
            if "|" in line and re.search(r"\b(?:price|qty|quantity|usd|inr|moq|kg)\b|\d", line, re.IGNORECASE)
        ]
    )
    if table_like_rows >= 2:
        catalogue_score += 3

    if _is_certificate_filename(filename_lower):
        certificate_score += 3
    if any(term in filename_lower for term in ("catalog", "catalogue", "price", "quotation", "quote")):
        catalogue_score += 3

    if certificate_score >= 2 and catalogue_score < certificate_score + 2:
        return DocumentClassification(CERTIFICATE, min(0.99, 0.55 + certificate_score / 10), _material_hint(filename, text))
    if catalogue_score >= 3:
        return DocumentClassification(CATALOGUE, min(0.99, 0.50 + catalogue_score / 12), None)
    return DocumentClassification(OTHER, 0.5, None)


def _term_score(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, re.IGNORECASE))


def _is_certificate_filename(filename: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])(?:coa|certificate|cert|analysis|halal|kosher|gmp|iso|msds)(?![a-z0-9])", filename))


def _material_hint(filename: str, text: str | None) -> str | None:
    candidates: list[str] = []
    source = f"{filename}\n{text or ''}"
    patterns = (
        r"certificate\s+of\s+analysis\s*[-:]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\bCOA\s*[-:]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\b(?:product|material|item|sample)\s*(?:name)?\s*[:\-]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source, flags=re.IGNORECASE):
            candidates.append(match.group("name"))

    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    stem = re.sub(r"(?i)\b(?:certificate of analysis|certificate|cert|coa|analysis|report|pdf|scan|copy)\b", " ", stem)
    stem = re.sub(r"[_-]+", " ", stem)
    if stem.strip():
        candidates.append(stem)

    for candidate in candidates:
        cleaned = _clean_material_hint(candidate)
        if cleaned:
            return cleaned
    return None


def _clean_material_hint(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" -_:.,")
    cleaned = re.split(r"\b(?:batch|lot|mfg|manufacturing|expiry|date|page|supplier)\b", cleaned, flags=re.IGNORECASE)[0].strip(" -_:.,")
    if len(cleaned) < 3:
        return None
    if cleaned.lower() in {"certificate", "analysis", "report", "quality"}:
        return None
    return cleaned[:120]
