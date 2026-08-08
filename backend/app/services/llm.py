import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

from backend.app.config import get_settings
from backend.app.schemas import ExtractedCatalogItem, QueryPlan

logger = logging.getLogger(__name__)

EXTRACTION_CHUNK_CHARS = 50000
EXTRACTION_CHUNK_OVERLAP_LINES = 4


@dataclass(frozen=True)
class ModelProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str
    max_tokens_field: str = "max_tokens"
    site_url: str = ""
    app_name: str = ""


class ModelRouterClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.providers = [
            ModelProviderConfig(
                name="groq",
                api_key=settings.groq_api_key,
                model=settings.groq_model,
                base_url=settings.groq_base_url.rstrip("/"),
                max_tokens_field="max_completion_tokens",
            ),
            ModelProviderConfig(
                name="openrouter",
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                base_url=settings.openrouter_base_url.rstrip("/"),
                max_tokens_field="max_tokens",
                site_url=settings.openrouter_site_url or settings.frontend_origin,
                app_name=settings.openrouter_app_name or settings.app_name,
            ),
        ]

    def _available_providers(self) -> list[ModelProviderConfig]:
        return [provider for provider in self.providers if provider.api_key]

    def _headers(self, provider: ModelProviderConfig) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        if provider.site_url:
            headers["HTTP-Referer"] = provider.site_url
        if provider.app_name:
            headers["X-Title"] = provider.app_name
        return headers

    def _chat_with_provider(
        self,
        provider: ModelProviderConfig,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            provider.max_tokens_field: 12000,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{provider.base_url}/chat/completions",
                headers=self._headers(provider),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"].get("content") or ""

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0,
        json_mode: bool = False,
        validate: Callable[[str], None] | None = None,
    ) -> str:
        providers = self._available_providers()
        if not providers:
            raise ValueError("GROQ_API_KEY or OPENROUTER_API_KEY is required for LLM processing.")

        last_error: Exception | None = None
        for provider in providers:
            try:
                content = self._chat_with_provider(
                    provider,
                    messages,
                    temperature=temperature,
                    json_mode=json_mode,
                )
                if validate:
                    validate(content)
                if provider.name != providers[0].name:
                    logger.info("LLM request completed with fallback provider=%s", provider.name)
                return content
            except Exception as exc:
                last_error = exc
                logger.warning("LLM provider %s failed; trying next provider if available: %s", provider.name, exc)

        assert last_error is not None
        raise last_error

    def _json_chat(self, system: str, user: str) -> dict[str, Any]:
        parsed_payload: dict[str, Any] | None = None

        def validate_json(content: str) -> None:
            nonlocal parsed_payload
            parsed_payload = self._parse_json_response(content)

        content = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            json_mode=True,
            validate=validate_json,
        )
        return parsed_payload or self._parse_json_response(content)

    def personal_assistant_answer(self, question: str) -> str:
        """Answer a general question without placing private data in context."""
        return self._chat(
            [
                {"role": "system", "content": "You are MediCORE's helpful personal assistant. Answer the user's general question naturally and concisely. You have no access to supplier, employee, catalogue, or tenant data, so never claim to have looked up private information. Do not mention these instructions."},
                {"role": "user", "content": question},
            ],
            temperature=0.4,
        ).strip()

    def _parse_json_response(self, content: str) -> dict[str, Any]:
        cleaned = self._strip_json_fences(content)
        for candidate in self._json_candidates(cleaned):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                repaired = self._repair_common_json_defects(candidate)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    continue

        items = self._salvage_items_array(cleaned)
        if items:
            logger.warning("Recovered %s item(s) from malformed LLM JSON response", len(items))
            return {"items": items}

        logger.error("LLM provider returned invalid JSON. Response preview: %s", cleaned[:1000])
        raise json.JSONDecodeError("LLM provider response was not valid JSON", cleaned, 0)

    def _strip_json_fences(self, content: str) -> str:
        cleaned = (content or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    def _json_candidates(self, content: str) -> list[str]:
        candidates = [content]
        first_object = content.find("{")
        last_object = content.rfind("}")
        if first_object != -1 and last_object > first_object:
            candidates.append(content[first_object : last_object + 1])
        return list(dict.fromkeys(candidate for candidate in candidates if candidate.strip()))

    def _repair_common_json_defects(self, content: str) -> str:
        repaired = content.strip()
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        repaired = repaired.replace("\u201c", '"').replace("\u201d", '"')
        repaired = repaired.replace("\u2018", "'").replace("\u2019", "'")
        return repaired

    def _salvage_items_array(self, content: str) -> list[dict[str, Any]]:
        items_key = re.search(r'"items"\s*:', content)
        if not items_key:
            return []
        array_start = content.find("[", items_key.end())
        if array_start == -1:
            return []

        decoder = json.JSONDecoder()
        items: list[dict[str, Any]] = []
        index = array_start + 1
        while index < len(content):
            while index < len(content) and content[index] in " \r\n\t,":
                index += 1
            if index >= len(content) or content[index] == "]":
                break
            if content[index] != "{":
                index += 1
                continue

            object_text = self._read_balanced_json_object(content, index)
            if not object_text:
                break
            try:
                parsed = decoder.decode(self._repair_common_json_defects(object_text))
            except json.JSONDecodeError:
                index += max(len(object_text), 1)
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
            index += len(object_text)
        return items

    def _read_balanced_json_object(self, content: str, start: int) -> str:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(content)):
            char = content[index]
            if escape:
                escape = False
                continue
            if char == "\\" and in_string:
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return content[start : index + 1]
        return ""

    def extract_catalog_items(self, pdf_text: str, reference_date: datetime | None = None) -> list[ExtractedCatalogItem]:
        extracted: list[ExtractedCatalogItem] = []
        seen: set[tuple] = set()
        chunks = self._chunk_text(pdf_text)
        for chunk_index, chunk in enumerate(chunks, start=1):
            try:
                chunk_items = self._extract_catalog_items_chunk(chunk, reference_date=reference_date)
            except Exception:
                logger.exception(
                    "LLM extraction failed for chunk %s/%s; continuing with remaining chunks",
                    chunk_index,
                    len(chunks),
                )
                continue
            for item in chunk_items:
                key = (
                    item.ingredient_name.strip().lower(),
                    (item.specification or "").strip().lower(),
                    str(item.price_per_unit),
                    (item.currency or "").upper(),
                    str(item.available_qty) if item.available_qty is not None else None,
                    (item.unit or "").strip().lower(),
                    item.lead_time_text or item.lead_time_days,
                    str(item.moq) if item.moq is not None else None,
                )
                if key in seen:
                    continue
                seen.add(key)
                extracted.append(item)
        return extracted

    def _extract_catalog_items_chunk(self, pdf_text: str, reference_date: datetime | None = None) -> list[ExtractedCatalogItem]:
        date_context = ""
        if reference_date:
            date_context = f"\n- Reference Date Context: The email or document was received on {reference_date.strftime('%Y-%m-%d')}. Use this exact date to resolve relative validity expressions (e.g. 'valid for 15 days' resolves to valid_until='{reference_date.strftime('%Y-%m-%d')}' + 15 days, 'valid until end of month' resolves to the end of the current month, etc.).\n"

        system = (
            "You are an expert AI parser for pharmaceutical and chemical supplier catalogs. "
            "Your task is to analyze the provided text (which could be a structured table, a conversational email body, or an unstructured list/paragraph) "
            "and extract all catalog items into a strict JSON structure. "
            "Return only valid minified JSON with a single key 'items' mapping to an array of catalog items. "
            "Do not use markdown fences, comments, trailing commas, or explanatory text.\n\n"
            "Each catalog item in the array MUST contain the following fields:\n"
            "- ingredient_name: The raw name of the chemical, ingredient, or medicine (e.g., 'Citric Acid Anhydrous', 'Paracetamol API', 'Aspirin USP')\n"
            "- specification: The exact product specification/description/grade/purity/assay/content from the row if present, otherwise null. "
            "Examples: '97% Powder', 'Berberine Extract 20:1', 'Fe2+: 20.0%-23.7%, Nitrogen: 10.0%-12.0%'. Do not merge this into ingredient_name.\n"
            "- price_per_unit: The numeric price from a price/rate column or phrase only. "
            "Never copy the quantity value into price_per_unit. Preserve the exact decimal value visible in the source; do not round. If a price range is given, use the visible lower bound and put the full original range in notes. "
            "If no real price/rate is visible for an item, use null instead of guessing; still extract the item if the product name is visible.\n"
            "- currency: The quoted transaction currency as a currency code. '$' or Price(USD) means 'USD'; "
            "'₹', 'Rs', 'Rupees', or Price(INR) means 'INR'; '€' means 'EUR'. Do not convert values between currencies.\n"
            "- available_qty: The numeric stock/available quantity from a Quantity, Qty, Qty Avail, or Quantity(KG) column. "
            "For example, in 'Quantity(KG)=9.99' and 'Price(USD)=$10.50/kg', available_qty is 9.99 and price_per_unit is 10.50. "
            "Preserve the exact decimal value visible in the source; do not round. If quantity is not visible, use null. Never output 0 unless the source explicitly says zero.\n"
            "- unit: The quantity/price unit (e.g., 'kg', 'g', 'litre', 'tablet', 'capsule', 'pack', 'drum'). Normalize units like 'kilograms', 'kgs' to 'kg'.\n"
            "- valid_until: An ISO 8601 date string (e.g. '2026-12-31') if an offer validity or expiry date is mentioned, otherwise null.\n"
            "- lead_time_days: An integer only when the source gives one exact lead time (e.g., '5 days'). Parse expressions like '1 week' to 7, 'next day' to 1. If the source gives a range like '40-50 days' or '40 to 50 days', use null here.\n"
            "- lead_time_text: The exact source lead-time phrase when present, especially ranges like '40-50 days'. If not mentioned, use null.\n"
            "- moq: A numeric float representing the Minimum Order Quantity. Extract hidden MOQ text inside other columns, "
            "such as '4.66 MOQ:25kg' -> available_qty=4.66, moq=25.0, unit='kg'. If not mentioned, use null.\n"
            "- notes: Any extra specifications, purity levels, packaging details, original price strings, Incoterms, "
            "packing terms, or conditions (e.g., 'CIF Vancouver $6.00/kg', '99% purity', '25kg packing', 'Payment: 30 days').\n\n"
            "CRITICAL TABLE MAPPING RULES:\n"
            "1. Read column headers before values. Values under Quantity/Qty columns are quantities, not prices, even if they look like decimals.\n"
            "2. Values under Price/Rate columns are prices. If the header says Price(USD), use currency='USD' even when the row only says '10.50/kg'.\n"
            "3. Preserve the original quoted currency and commercial terms in notes, but keep price_per_unit numeric.\n"
            "4. Treat 'NA' prices as unavailable and set price_per_unit=null unless a real numeric price is present elsewhere in the same row.\n"
            "5. No hallucination: every ingredient, price, quantity, unit, MOQ, lead time, and date must be directly supported by text visible in this chunk. "
            "Put the exact source row/phrase for each item into notes as source='...'. Do not infer missing numeric values, do not convert units/currencies, and do not round decimal values.\n\n"
            "CRITICAL INSTRUCTIONS FOR UNSTRUCTURED / CONVERSATIONAL TEXT:\n"
            f"1. Conversational Emails: If the text is an email conversation, locate all mentions of products, prices, quantities, and terms, and map them to the schema.{date_context}\n"
            "2. Implicit Packaging: If the text says 'Rs 3000 per 25kg bag', normalize this to a single item with price_per_unit=3000, unit='bag' or price_per_unit=120, unit='kg', depending on how the price is stated, but map it logically.\n"
            "3. Purity & Grades: Keep grades (e.g. 'IP', 'USP', 'Food Grade') and CAS numbers in the ingredient_name and notes.\n"
            "4. Volume / Tiered Pricing: If the email lists multiple price tiers based on quantity (e.g., '$5/kg for 100kg, or $4/kg for 500kg'), extract EACH tier as a separate catalog item in the array, setting the price_per_unit, moq, and available_qty accordingly.\n"
            "5. CAS Registry Numbers: Extract CAS numbers (e.g. 'CAS 50-78-2') and specify them clearly in the 'notes' field (e.g. 'CAS: 50-78-2').\n"
            "6. Incoterms & Conditions: Extract Incoterms (FOB, CIF, EXW, DDP, CFR) or shipping details (e.g. 'FOB Shanghai', 'origin: India') and save them in 'notes'.\n"
            "7. Thoroughness: Extract EVERY single product listed in this chunk. Do not summarize or stop early. "
            "If there are 20 visible rows, return all 20 rows; use null for missing commercial fields.\n"
            "8. OCR Robustness: Correct obvious OCR confusions only when context is clear, e.g. O/0 in numbers, l/1 in quantities, broken table spacing. "
            "If a row is ambiguous, omit that row instead of guessing."
        )
        payload = self._json_chat(system, pdf_text)
        extracted = []
        for item in payload.get("items", []):
            try:
                extracted.append(ExtractedCatalogItem.model_validate(item))
            except Exception as e:
                logger.warning("Skipping invalid catalog item: %s. Error: %s", item, e)
        return extracted

    def _chunk_text(self, text: str) -> list[str]:
        normalized = text.strip()
        if not normalized:
            return []
        if len(normalized) <= EXTRACTION_CHUNK_CHARS:
            return [normalized]

        lines = normalized.splitlines()
        header_lines: list[str] = []
        for line in lines[:5]:
            if line.startswith("Sheet:") or "," in line or "|" in line or "\t" in line:
                header_lines.append(line)
                if len(header_lines) >= 2:
                    break

        header_prefix = "\n".join(header_lines) + "\n" if header_lines else ""

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current and current_len + line_len > EXTRACTION_CHUNK_CHARS:
                chunk_str = "\n".join(current)
                if chunks and header_prefix and not chunk_str.startswith(header_lines[0]):
                    chunk_str = header_prefix + chunk_str
                chunks.append(chunk_str)
                current = current[-EXTRACTION_CHUNK_OVERLAP_LINES:]
                current_len = sum(len(row) + 1 for row in current)
            current.append(line)
            current_len += line_len
        if current:
            chunk_str = "\n".join(current)
            if chunks and header_prefix and not chunk_str.startswith(header_lines[0]):
                chunk_str = header_prefix + chunk_str
            chunks.append(chunk_str)
        return chunks

    def generate_sql(self, question: str) -> str:
        system = (
            "You are a PostgreSQL SQL generator for a supplier catalog procurement database on Supabase Cloud. "
            "Your task is to analyze the user's natural-language query and generate a single, highly efficient, read-only SQL query.\n\n"
            "Database Schema Overview:\n"
            "- suppliers (id UUID, tenant_id UUID, name TEXT, email_domain TEXT, last_email_date TIMESTAMPTZ, certifications TEXT)\n"
            "- catalog_emails (id UUID, tenant_id UUID, supplier_id UUID, received_at TIMESTAMPTZ, raw_email_id TEXT, subject TEXT, pdf_url TEXT, processing_status TEXT)\n"
            "- catalog_items (id UUID, tenant_id UUID, catalog_email_id UUID, supplier_id UUID, ingredient_name TEXT, price_per_unit NUMERIC(14,4), currency TEXT, available_qty NUMERIC(14,4), unit TEXT, valid_until TIMESTAMPTZ, lead_time_days INT, moq NUMERIC(14,4), raw_payload JSONB)\n"
            "- purchase_history (id UUID, tenant_id UUID, supplier_id UUID, item_id UUID, purchased_at TIMESTAMPTZ, quantity NUMERIC(14,2), price_paid NUMERIC(14,4))\n\n"
            "CRITICAL SQL GENERATION RULES:\n"
            "1. ONLY generate a read-only SELECT query (or WITH ... SELECT). Never generate INSERT, UPDATE, DELETE, DROP, ALTER, or TRUNCATE statements.\n"
            "2. Return ONLY the raw SQL code in plain text. Do not wrap in markdown markdown fences (```sql), do not include comments or explanations.\n"
            "3. Select meaningful columns including catalog_items.id AS id, suppliers.name AS supplier_name, suppliers.email_domain AS email_domain, ingredient_name, price_per_unit, currency, available_qty, unit, moq, lead_time_days, and catalog_emails.received_at AS received_at.\n"
            "4. Use case-insensitive partial matching on catalog_items.ingredient_name. For multi-word ingredient searches, split meaningful words and match each with ILIKE wildcards where practical; do not require exact names.\n"
            "5. Rank closer ingredient_name matches first, then apply appropriate ORDER BY clauses (e.g. ORDER BY price_per_unit ASC NULLS LAST for best price/cheapest deal requests).\n"
            "6. When returning catalog items, always join catalog_emails on catalog_items.catalog_email_id = catalog_emails.id so received_at is the email received date for that item.\n"
            "7. Always limit results to at most 50 rows (LIMIT 50).\n"
            "8. Tenant isolation is mandatory: every query that reads tenant-owned tables must include an explicit predicate using the bound parameter :tenant_id (for example catalog_items.tenant_id = :tenant_id). Never use a literal tenant ID."
        )
        content = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0,
        )
        sql = self._strip_json_fences(content).strip()
        sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"\s*```$", "", sql).strip()
        return sql

    def plan_query(self, question: str) -> QueryPlan:
        system = (
            "You produce safe JSON query plans for a supplier catalogue database. "
            "Allowed operations:\n"
            "- supplier_compare: Compare prices and quantities of a specific chemical across different suppliers.\n"
            "- best_price: Find the cheapest/best deal for a specific chemical.\n"
            "- catalog_search: Search catalogs or find suppliers matching a general keyword or semantic context.\n"
            "- history_compare: Compare historical prices or price trends for an ingredient.\n"
            "- supplier_activity: Check recently received/synced emails, catalog activity, or sync statuses.\n"
            "- unrelated: Use when the request is unrelated to supplier catalogs, prices, procurement, or setting configurations.\n\n"
            "If the question is unrelated to the MediCORE procurement system (e.g. general knowledge, personal advice, coding, entertainment, unrelated topics), "
            "you MUST classify the operation as 'unrelated'.\n\n"
            "Do not emit SQL. You MUST output a FLAT JSON object (no nested 'filters' object) containing the following fields:\n"
            "- operation: one of the allowed operations\n"
            "- ingredient_name: string or null (extract the broad chemical/ingredient search phrase, preserving the user's wording where possible; for 'marigold', use 'marigold', not one full Marigold variant)\n"
            "- min_quantity: number or null (extract any minimum quantity/stock requirements)\n"
            "- unit: string or null (normalize units, e.g. 'kg', 'g', 'litre', 'tablet')\n"
            "- semantic_query: string or null\n"
            "- limit: number (default 10)\n\n"
            "Example output for 'Compare citric acid':\n"
            "{\"operation\": \"supplier_compare\", \"ingredient_name\": \"citric acid\", \"min_quantity\": null, \"unit\": null, \"semantic_query\": null, \"limit\": 10}"
        )
        payload = self._json_chat(system, question)
        return QueryPlan.model_validate(payload)

    def summarize_answer(self, question: str, rows: list[dict[str, Any]]) -> str:
        compact_rows = [
            {
                "supplier": row.get("supplier_name"),
                "item": self._display_item_name(row),
                "specification": row.get("specification"),
                "price": row.get("price_per_unit"),
                "price_display": row.get("price_display"),
                "currency": row.get("currency"),
                "qty": row.get("available_qty"),
                "quantity_display": row.get("quantity_display"),
                "unit": row.get("unit"),
                "lead_time": row.get("lead_time_text") or row.get("lead_time_days"),
                "certifications": row.get("certifications"),
            }
            for row in rows[:20]
        ]
        return self._chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are ProcuraAI, MediCORE's professional procurement assistant. You MUST adhere to these rules:\n"
                        "1. Relevance: You only answer questions related to the MediCORE procurement intelligence system, "
                        "such as supplier catalogues, ingredients/chemicals, prices, inventory, lead times, sync status, settings, or supplier comparisons. "
                        "If the question is unrelated, you must politely refuse to answer. Example: 'I'm sorry, I can only help you with questions related to the MediCORE procurement intelligence system.'\n"
                        "2. No Hallucinations: Do NOT invent or make up any suppliers, ingredient names, prices, quantities, lead times, reliability ratings, or scores. "
                        "Only reference facts directly present in the provided context rows.\n"
                        "3. Handling No Data: If there are no matching context rows or if you do not know the answer, "
                        "state politely that you couldn't find any matching data or records in the database, and offer to help with a different procurement query. "
                        "Do not assume or hallucinate search results.\n"
                        "If context rows are provided, they are matching database rows for the user's query. Do not say no data was found when rows are present.\n"
                        "4. Completeness: When mentioning prices, always include the exact currency (e.g. USD, INR, EUR) and unit (e.g. kg, bag, tablet). "
                        "Couple pricing with availability/quantity details if present to give a complete summary.\n"
                        "5. Professional Insights: Provide a brief, helpful insight on the best recommendation or cheapest deal based only on actual catalogue values such as price, quantity, lead time, MOQ, and date. "
                        "Never mention supplier reliability scores, confidence scores, AI scores, percentages, ratings, or scoring formulas.\n"
                        "6. Formatting: Respond in a natural, friendly, professional, conversational tone (3-4 sentences max). "
                        "Return plain text only—no markdown, no bold text, no bullet points, and no tables."
                    ),
                },
                {"role": "user", "content": json.dumps({"question": question, "rows": compact_rows}, default=str)},
            ],
            temperature=0.3,
        ) or "No answer generated."

    def _display_item_name(self, row: dict[str, Any]) -> str | None:
        name = row.get("ingredient_name")
        if not name:
            return None
        return f"{name} (U)" if row.get("is_updated") else str(name)


class OpenRouterClient(ModelRouterClient):
    """Backward-compatible name for the routed LLM client."""
