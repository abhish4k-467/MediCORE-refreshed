from __future__ import annotations

import re
from dataclasses import dataclass


UNKNOWN_COUNTRY = "Unknown"


COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "United States": ("united states of america", "united states", "u.s.a.", "u.s.a", "usa", "u.s.", "u.s", "us"),
    "United Kingdom": ("united kingdom", "great britain", "england", "scotland", "wales", "northern ireland", "u.k.", "u.k", "uk"),
    "United Arab Emirates": ("united arab emirates", "u.a.e.", "u.a.e", "uae"),
    "China": ("people's republic of china", "prc", "china"),
    "India": ("bharat", "india"),
    "Germany": ("federal republic of germany", "germany", "deutschland"),
    "Japan": ("japan",),
    "South Korea": ("republic of korea", "south korea", "korea"),
    "Singapore": ("singapore",),
    "Malaysia": ("malaysia",),
    "Thailand": ("thailand",),
    "Vietnam": ("viet nam", "vietnam"),
    "Indonesia": ("indonesia",),
    "Philippines": ("philippines",),
    "Taiwan": ("taiwan",),
    "Hong Kong": ("hong kong", "hongkong"),
    "Australia": ("australia",),
    "New Zealand": ("new zealand",),
    "Canada": ("canada",),
    "Mexico": ("mexico",),
    "Brazil": ("brazil",),
    "Argentina": ("argentina",),
    "Chile": ("chile",),
    "France": ("france",),
    "Italy": ("italy",),
    "Spain": ("spain",),
    "Netherlands": ("netherlands", "holland"),
    "Belgium": ("belgium",),
    "Switzerland": ("switzerland", "swiss"),
    "Austria": ("austria",),
    "Ireland": ("ireland",),
    "Poland": ("poland",),
    "Czech Republic": ("czech republic", "czechia"),
    "Denmark": ("denmark",),
    "Sweden": ("sweden",),
    "Norway": ("norway",),
    "Finland": ("finland",),
    "Russia": ("russian federation", "russia"),
    "Turkey": ("turkey", "turkiye", "tuerkiye"),
    "Israel": ("israel",),
    "Saudi Arabia": ("saudi arabia", "ksa"),
    "Qatar": ("qatar",),
    "Oman": ("oman",),
    "Kuwait": ("kuwait",),
    "Bahrain": ("bahrain",),
    "South Africa": ("south africa",),
    "Egypt": ("egypt",),
    "Kenya": ("kenya",),
    "Nigeria": ("nigeria",),
    "Pakistan": ("pakistan",),
    "Bangladesh": ("bangladesh",),
    "Sri Lanka": ("sri lanka",),
}

CITY_REGION_COUNTRIES: dict[str, str] = {
    "shanghai": "China",
    "beijing": "China",
    "guangzhou": "China",
    "shenzhen": "China",
    "hangzhou": "China",
    "nanjing": "China",
    "ahmedabad": "India",
    "gujarat": "India",
    "mumbai": "India",
    "delhi": "India",
    "hyderabad": "India",
    "chennai": "India",
    "bangalore": "India",
    "bengaluru": "India",
    "maharashtra": "India",
    "ontario ca": "United States",
    "california": "United States",
    "new jersey": "United States",
    "texas": "United States",
    "florida": "United States",
    "new york": "United States",
    "london": "United Kingdom",
    "dubai": "United Arab Emirates",
    "abu dhabi": "United Arab Emirates",
    "frankfurt": "Germany",
    "hamburg": "Germany",
    "munich": "Germany",
}

ADDRESS_CUES = (
    "address",
    "office",
    "warehouse",
    "factory",
    "plant",
    "headquarter",
    "headquarters",
    "registered",
    "contact",
    "tel",
    "phone",
    "mobile",
    "email",
    "zip",
    "postal",
    "postcode",
    "road",
    "street",
    "avenue",
    "suite",
    "floor",
    "building",
    "industrial",
    "zone",
)


@dataclass(frozen=True)
class CountryDetection:
    country: str
    confidence: int


def normalize_country(value: str | None) -> str:
    if not value:
        return UNKNOWN_COUNTRY
    normalized = _canonical_text(value)
    for country, aliases in COUNTRY_ALIASES.items():
        if normalized == _canonical_text(country) or normalized in {_canonical_text(alias) for alias in aliases}:
            return country
    return UNKNOWN_COUNTRY


def detect_supplier_country(*texts: str | None) -> str:
    return detect_supplier_country_with_confidence(*texts).country


def detect_supplier_country_with_confidence(*texts: str | None) -> CountryDetection:
    # Supplier letterheads often contain a customer's/shipping country near
    # the beginning.  The registered office in the footer is the stronger
    # supplier signal, so resolve a footer address before considering other
    # occurrences in the document.
    for text in texts:
        footer = _footer_address_window(text)
        if not footer:
            continue
        footer_matches = _countries_in_window(footer)
        if footer_matches:
            country, confidence = max(footer_matches, key=lambda item: item[1])
            if confidence >= 70:
                return CountryDetection(country, confidence + 30)

    candidates: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for window_no, window in enumerate(_address_windows(text)):
            window_bonus = max(0, 40 - window_no)
            for country, score in _countries_in_window(window):
                candidates[country] = max(candidates.get(country, 0), score + window_bonus)

    if not candidates:
        return CountryDetection(UNKNOWN_COUNTRY, 0)

    country, confidence = max(candidates.items(), key=lambda item: item[1])
    if confidence < 55:
        return CountryDetection(UNKNOWN_COUNTRY, confidence)
    return CountryDetection(country, confidence)


def _footer_address_window(text: str | None) -> str:
    if not text:
        return ""
    lines = [" ".join(line.strip().split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    footer_lines = lines[-12:]
    # Start at the final address/contact cue. This avoids treating a shipping
    # destination earlier in the footer as the supplier's registered office.
    address_indexes = [
        index
        for index, line in enumerate(footer_lines)
        if any(cue in line.lower() for cue in ("address", "office", "warehouse", "factory", "plant", "registered", "headquarter"))
        or _looks_like_address_line(line)
    ]
    if address_indexes:
        footer_lines = footer_lines[address_indexes[-1] :]
    footer = "\n".join(footer_lines)
    if any(cue in footer.lower() for cue in ADDRESS_CUES) or any(_looks_like_address_line(line) for line in footer_lines):
        return footer
    return ""


def _address_windows(text: str) -> list[str]:
    lines = [" ".join(line.strip().split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    interesting: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(cue in lowered for cue in ADDRESS_CUES) or _looks_like_address_line(line):
            interesting.append((idx, 35))

    tail_start = max(0, len(lines) - 35)
    interesting.append((tail_start, 25))
    interesting.append((0, 10))

    windows: list[str] = []
    seen: set[str] = set()
    for idx, _score in sorted(interesting, key=lambda item: item[1], reverse=True):
        start = max(0, idx - 4)
        end = min(len(lines), idx + 8)
        window = "\n".join(lines[start:end])
        key = window.lower()
        if key and key not in seen:
            seen.add(key)
            windows.append(window)
    return windows


def _countries_in_window(window: str) -> list[tuple[str, int]]:
    lowered = window.lower()
    canonical_window = _canonical_text(window)
    matches: list[tuple[str, int]] = []

    for country, aliases in COUNTRY_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if _alias_matches(window, alias):
                score = 70
                if re.search(rf"(?:^|[\n,])\s*{re.escape(alias)}\s*(?:$|[\n,.])", lowered, re.IGNORECASE):
                    score += 20
                if any(cue in lowered for cue in ADDRESS_CUES):
                    score += 20
                matches.append((country, score))
                break

    for phrase, country in CITY_REGION_COUNTRIES.items():
        if phrase in canonical_window:
            score = 60 + (20 if any(cue in lowered for cue in ADDRESS_CUES) else 0)
            matches.append((country, score))

    return matches


def _alias_matches(text: str, alias: str) -> bool:
    lowered = text.lower()
    alias_lower = alias.lower()
    if alias_lower in {"us", "u.s", "u.s."}:
        return bool(re.search(r"\bU\.?S\.?\b", text)) and any(cue in lowered for cue in ADDRESS_CUES)
    if alias_lower in {"uk", "u.k", "u.k."}:
        return bool(re.search(r"\bU\.?K\.?\b", text)) or re.search(r"\bUK\b", text) is not None
    pattern = rf"(?<![a-z0-9]){re.escape(alias_lower)}(?![a-z0-9])"
    return re.search(pattern, lowered) is not None


def _looks_like_address_line(line: str) -> bool:
    lowered = line.lower()
    return bool(re.search(r"\b\d{5,6}(?:-\d{4})?\b", line)) or "," in line and any(
        token in lowered
        for token in (
            "road",
            "street",
            "st.",
            "avenue",
            "ave",
            "industrial",
            "city",
            "province",
            "state",
            "zip",
            "postal",
        )
    )


def _canonical_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
