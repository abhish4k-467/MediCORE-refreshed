import re
from dataclasses import dataclass


CATALOGUE = "catalogue"
CERTIFICATE = "certificate"
OTHER = "other"

CERTIFICATE_TERMS = (
    "certificate of analysis",
    "analysis certificate",
    "certificate of quality",
    "certificate of conformance",
    "quality certificate",
    "test certificate",
    "analytical report",
    "guarantee of analysis",
    "certificate",
    "coa",
    "c of a",
    "coc",
    "lab report",
    "laboratory report",
    "test report",
    "quality report",
    "specification sheet",
    "technical data sheet",
    "tds",
    "msds",
    "halal",
    "kosher",
    "organic certificate",
    "gmp",
    "cgmp",
    "iso",
    "assay",
    "purity",
    "batch release",
)
CATALOGUE_TERMS = (
    "catalog",
    "catalogue",
    "price list",
    "pricelist",
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
CATALOGUE_TABLE_HEADER_PATTERN = re.compile(
    r"\bproduct\s+name\b.{0,80}\bspecification\b|\bspecification\b.{0,80}\bproduct\s+name\b",
    re.IGNORECASE | re.DOTALL,
)
CATALOGUE_CATEGORY_PATTERN = re.compile(
    r"\b(?:used\s+for|sports\s+nutrition|dietary\s+supplements|oem\s+services|hard\s+capsules|soft\s+gels|tablets)\b",
    re.IGNORECASE,
)
CATALOGUE_PRODUCT_SPEC_ROW_PATTERN = re.compile(
    r"\b[A-Z][A-Za-z0-9][A-Za-z0-9 .,&()/+-]{2,80}\s{2,}"
    r"(?:all\s+grade|usp\s+grade|nf\s*(?:ii)?\s*grade|vegan|"
    r"\d+(?:\.\d+)?\s*%|[A-Za-z]+(?:oids?|in|ins?)\b|based\s+on\s+extract\s+ratio)",
    re.IGNORECASE,
)

# These fields commonly appear together on a COA or quality certificate even
# when the scan/OCR misses its heading.  They are intentionally not commercial
# catalogue fields, so the combination is a strong certificate signal.
CERTIFICATE_FIELD_TERMS = (
    "batch no",
    "batch number",
    "lot no",
    "lot number",
    "manufacturing date",
    "expiry date",
    "retest date",
    "appearance",
    "identification",
    "assay",
    "purity",
    "loss on drying",
    "heavy metals",
    "microbial",
    "conforms",
    "complies",
)

PRICE_UPDATE_SENTENCE_PATTERN = re.compile(
    r"\b(?:price|rate)\s+(?:of|for)\s+"
    r"(?P<material>[A-Za-z0-9][A-Za-z0-9 %().,+/'-]{2,120}?)\s+"
    r"(?:is\s+)?(?:updated|revised|changed|set|now|increased|decreased)\s+"
    r"(?:to|at|as)?\s*(?:US\$|\$|USD|INR|Rs\.?|₹|EUR|€|GBP|£)?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*/\s*[A-Za-z]+",
    re.IGNORECASE,
)


DIRECT_PRICE_SENTENCE_PATTERN = re.compile(
    r"\b(?:price|rate)\s+(?:of|for)\s+"
    r"(?P<material>[A-Za-z0-9][A-Za-z0-9 %().,+/'-]{2,120}?)\s+"
    r"(?:is|:|-)\s*"
    r"(?:US\$|\$|USD|INR|Rs\.?|EUR|GBP)?\s*"
    r"\d[\d,]*(?:\.\d+)?\s*/\s*[A-Za-z]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DocumentClassification:
    category: str
    confidence: float
    material_hint: str | None = None


def classify_document(
    filename: str,
    ext: str,
    text: str | None,
    context_text: str | None = None,
) -> DocumentClassification:
    combined_source = f"{filename}\n{context_text or ''}\n{text or ''}".lower()
    filename_lower = filename.lower()

    certificate_score = _term_score(combined_source, CERTIFICATE_TERMS)
    catalogue_score = _term_score(combined_source, CATALOGUE_TERMS) + _term_score(combined_source, COMMERCIAL_TERMS)
    product_spec_rows = _catalogue_product_spec_row_count(text or "")
    has_catalogue_header = bool(CATALOGUE_TABLE_HEADER_PATTERN.search(text or ""))
    has_catalogue_category = bool(CATALOGUE_CATEGORY_PATTERN.search(text or ""))
    if product_spec_rows >= 3:
        catalogue_score += 5
    if has_catalogue_header:
        catalogue_score += 4
    if has_catalogue_category:
        catalogue_score += 3

    certificate_field_score = _term_score(combined_source, CERTIFICATE_FIELD_TERMS)
    if certificate_field_score >= 2 and product_spec_rows < 3:
        # A heading can be lost in a scan, but two or more analytical/batch
        # fields identify the document as a certificate rather than a quote.
        certificate_score += certificate_field_score + 2

    table_like_rows = len(
        [
            line
            for line in (text or "").splitlines()
            if "|" in line and re.search(r"\b(?:price|qty|quantity|usd|inr|moq|kg)\b|\d", line, re.IGNORECASE)
        ]
    )
    if table_like_rows >= 2:
        catalogue_score += 3

    if PRICE_UPDATE_SENTENCE_PATTERN.search(text or "") or DIRECT_PRICE_SENTENCE_PATTERN.search(text or ""):
        catalogue_score += 3

    is_cert_file = _is_certificate_filename(filename_lower)
    is_cat_file = any(
        term in filename_lower for term in ("catalog", "catalogue", "price list", "pricelist", "price", "quotation", "quote")
    )

    if is_cert_file:
        certificate_score += 4
    if is_cat_file:
        catalogue_score += 4

    if (has_catalogue_header and product_spec_rows >= 2) or product_spec_rows >= 5:
        return DocumentClassification(CATALOGUE, min(0.99, 0.72 + catalogue_score / 20), None)

    if is_cert_file and not is_cat_file:
        return DocumentClassification(CERTIFICATE, min(0.99, 0.75 + certificate_score / 10), _material_hint(filename, text))

    if certificate_score >= 2 and catalogue_score < certificate_score + 3:
        return DocumentClassification(CERTIFICATE, min(0.99, 0.55 + certificate_score / 10), _material_hint(filename, text))

    if catalogue_score >= 3:
        return DocumentClassification(CATALOGUE, min(0.99, 0.50 + catalogue_score / 12), None)

    if certificate_score >= 2:
        return DocumentClassification(CERTIFICATE, min(0.99, 0.55 + certificate_score / 10), _material_hint(filename, text))

    return DocumentClassification(OTHER, 0.5, None)


def _term_score(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, re.IGNORECASE))


def _catalogue_product_spec_row_count(text: str) -> int:
    count = 0
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if not cleaned:
            continue
        if CATALOGUE_PRODUCT_SPEC_ROW_PATTERN.search(line) or CATALOGUE_PRODUCT_SPEC_ROW_PATTERN.search(cleaned):
            count += 1
    return count


def _is_certificate_filename(filename: str) -> bool:
    lowered = filename.lower()
    return bool(
        re.search(
            r"\b(?:coa|cert|certificate|analysis|halal|kosher|gmp|iso|msds|tds|coc|spec|specification|quality|report)\b"
            r"|(?<![a-z0-9])(?:coa|cert|certificate)[0-9_\-\.]",
            lowered,
        )
    )


def _material_hint(filename: str, text: str | None) -> str | None:
    candidates: list[str] = []
    source = f"{filename}\n{text or ''}"
    patterns = (
        r"certificate\s+of\s+analysis\s*[-:]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\bCOA\s*[-:]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\b(?:product|material|item|sample|chemical|ingredient)\s*(?:name)?\s*[:\-]\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\bCoA\s+for\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
        r"\bCertificate\s+for\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 %().,+/-]{2,120})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source, flags=re.IGNORECASE):
            candidates.append(match.group("name"))

    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    stem = re.sub(r"(?i)\b(?:certificate of analysis|certificate|cert|coa|analysis|report|pdf|scan|copy|doc|document)\b", " ", stem)
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
