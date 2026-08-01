import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import CatalogEmail, CatalogItem, Supplier
from backend.app.schemas import ChatResponse
from backend.app.services.llm import OpenRouterClient
from backend.app.services.query_whitelist import validate_operation
from backend.app.services.ranking import SupplierRanker
from backend.app.services.sql_executor import execute_readonly_sql

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngredientMatchResult:
    extracted_phrase: str
    search_phrase: str | None
    matched_names: list[str]
    best_match: str | None
    confidence: float


class NaturalLanguageQueryEngine:
    def __init__(self, db: Session, cache: Redis) -> None:
        self.db = db
        self.cache = cache
        self.llm = OpenRouterClient()
        self.ranker = SupplierRanker(db)

    def _answer(
        self,
        question: str,
        tenant_id: Any | None = None,
        user_id: Any | None = None,
    ) -> ChatResponse:
        try:
            cache_key = f"chat:answer:v14:{tenant_id}:{question.strip().lower()}"
            cached = self._cache_get(cache_key)
            if cached:
                payload = json.loads(cached)
                self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type="cached")
                return ChatResponse(**payload)

            match_result: IngredientMatchResult | None = None
            try:
                match_result = self._resolve_ingredient_from_db(question, tenant_id=tenant_id)
            except Exception as exc:
                logger.warning("Catalogue ingredient pre-match failed; continuing with query plan: %s", exc)

            try:
                plan = self.llm.plan_query(question)
            except Exception:
                plan = self._fallback_plan(question)

            detected_operation = self._extract_query_intent(question)
            if match_result and match_result.search_phrase:
                plan = plan.model_copy(
                    update={
                        "operation": detected_operation if plan.operation in {"unrelated", "supplier_activity"} else plan.operation,
                        "ingredient_name": match_result.search_phrase,
                    }
                )
            elif match_result and match_result.extracted_phrase and self._looks_like_procurement_question(question):
                logger.info(
                    "ProcuraAI ingredient match failed original_query=%r extracted_ingredient=%r best_database_match=%r confidence=%.3f generated_sql=%r",
                    question,
                    match_result.extracted_phrase,
                    match_result.best_match,
                    match_result.confidence,
                    "",
                )
                self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type="catalog_search")
                return ChatResponse(answer="No matching ingredient found.", rows=[])

            if plan.operation == "unrelated":
                self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type=plan.operation)
                return ChatResponse(
                    answer="I'm sorry, but I can only answer questions related to the MediCORE procurement intelligence system (such as supplier catalogues, ingredients/chemicals, prices, inventory, and procurement settings).",
                    rows=[]
                )

            try:
                validate_operation(plan.operation)
            except ValueError:
                plan = plan.model_copy(update={"operation": "catalog_search"})

            if not (match_result and match_result.matched_names):
                plan = self._ground_plan_in_catalog(question, plan, tenant_id=tenant_id)
            self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type=plan.operation)

            # 1. Grounded ingredient searches should use deterministic partial/fuzzy
            # matching before model-generated SQL can overfit the user's typo.
            rows: list[dict[str, Any]] = []
            if match_result and match_result.matched_names:
                rows = self._execute_matched_ingredient_query(plan, match_result.matched_names, tenant_id=tenant_id)
            elif getattr(plan, "ingredient_name", None):
                try:
                    rows = self._execute_plan(plan, tenant_id=tenant_id)
                except Exception as exc:
                    logger.warning("Structured query plan execution failed: %s", exc)
                    rows = []

            # 2. Attempt AI Read-Only SQL Generation & Execution against Supabase Cloud
            try:
                generated_sql = (
                    self.llm.generate_sql(self._grounded_sql_question(question, plan))
                    if not rows and not (match_result and match_result.extracted_phrase)
                    else ""
                )
                if generated_sql:
                    logger.info("ProcuraAI generated SQL original_query=%r generated_sql=%s", question, generated_sql)
                if generated_sql:
                    sql_rows = execute_readonly_sql(self.db, generated_sql, tenant_id=tenant_id)
                    if sql_rows:
                        rows = self._normalize_sql_rows(sql_rows)
            except Exception as exc:
                logger.warning("AI SQL generation/execution failed; falling back to structured plan: %s", exc)
                rows = []

            # 3. Fallback to structured QueryPlan execution if AI SQL produced no results
            if not rows:
                if match_result and match_result.extracted_phrase:
                    rows = []
                else:
                    try:
                        rows = self._execute_plan(plan, tenant_id=tenant_id)
                    except Exception as exc:
                        logger.warning("Structured query plan execution failed: %s", exc)
                        rows = []

            if match_result and match_result.extracted_phrase and not rows:
                response = ChatResponse(answer="No matching ingredient found.", rows=[])
                self._cache_set(cache_key, response.model_dump_json())
                return response

            try:
                rows = self.ranker._dedupe_supplier_item_rows(rows, plan.ingredient_name)
                rows = self._sort_rows_for_question(question, rows)
            except Exception as exc:
                logger.warning("Failed to rank/sort query rows: %s", exc)
                pass

            try:
                answer = self.llm.summarize_answer(question, rows)
            except Exception:
                logger.exception("LLM answer summarization failed; using fallback summary")
                answer = self._fallback_summary(question, rows)

            if rows and self._looks_like_false_negative(answer):
                answer = self._fallback_summary(question, rows)

            response = ChatResponse(answer=answer, rows=rows)
            self._cache_set(cache_key, response.model_dump_json())
            return response
        except Exception:
            logger.exception("Natural language query failed unexpectedly")
            return ChatResponse(
                answer=self._fallback_summary(question, []),
                rows=[]
            )

    def _normalize_sql_rows(self, sql_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_list = []
        for row in sql_rows:
            norm = dict(row)
            # Ensure price_per_unit, available_qty, moq are floats if numeric
            for field in ("price_per_unit", "available_qty", "moq"):
                if norm.get(field) is not None:
                    try:
                        norm[field] = float(norm[field])
                    except (ValueError, TypeError):
                        pass

            if "supplier_name" not in norm and "name" in norm:
                norm["supplier_name"] = norm["name"]
            elif "supplier_name" not in norm:
                norm["supplier_name"] = "Supplier"
            if not norm.get("email_domain"):
                for alias in ("supplier_email", "email", "supplier_email_domain"):
                    if norm.get(alias):
                        norm["email_domain"] = norm[alias]
                        break

            self._normalize_received_at(norm)

            if not norm.get("price_display") and norm.get("price_per_unit") is not None:
                currency = norm.get("currency") or "INR"
                unit = norm.get("unit") or "kg"
                norm["price_display"] = f"{currency} {norm['price_per_unit']}/{unit}"

            if not norm.get("quantity_display") and norm.get("available_qty") is not None:
                unit = norm.get("unit") or "kg"
                norm["quantity_display"] = f"{norm['available_qty']} {unit}"

            if not norm.get("certificate_pdfs") and isinstance(norm.get("raw_payload"), dict):
                norm["certificate_pdfs"] = self._certificate_pdfs(norm.get("raw_payload"))

            normalized_list.append(norm)
        self._hydrate_missing_received_at(normalized_list)
        self._hydrate_missing_certificate_pdfs(normalized_list)
        self._hydrate_missing_supplier_email(normalized_list)
        return normalized_list

    def _normalize_received_at(self, row: dict[str, Any]) -> None:
        if not row.get("received_at"):
            for alias in (
                "email_received_at",
                "catalog_received_at",
                "received_date",
                "email_date",
                "mail_date",
                "date",
            ):
                if row.get(alias):
                    row["received_at"] = row[alias]
                    break

        value = row.get("received_at")
        if hasattr(value, "isoformat"):
            row["received_at"] = value.isoformat()

    def _hydrate_missing_received_at(self, rows: list[dict[str, Any]]) -> None:
        missing_rows = [row for row in rows if not row.get("received_at")]
        if not missing_rows:
            return

        email_ids = {self._coerce_uuid(row.get("catalog_email_id")) for row in missing_rows}
        email_ids.discard(None)
        item_ids = {
            self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            for row in missing_rows
        }
        item_ids.discard(None)

        received_by_email_id: dict[Any, Any] = {}
        received_by_item_id: dict[Any, Any] = {}
        try:
            if email_ids:
                for email_id, received_at in self.db.execute(
                    select(CatalogEmail.id, CatalogEmail.received_at).where(CatalogEmail.id.in_(email_ids))
                ):
                    received_by_email_id[email_id] = received_at

            if item_ids:
                for item_id, received_at in self.db.execute(
                    select(CatalogItem.id, CatalogEmail.received_at)
                    .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
                    .where(CatalogItem.id.in_(item_ids))
                ):
                    received_by_item_id[item_id] = received_at
        except Exception:
            return

        for row in missing_rows:
            received_at = None
            email_id = self._coerce_uuid(row.get("catalog_email_id"))
            item_id = self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            if email_id:
                received_at = received_by_email_id.get(email_id)
            if not received_at and item_id:
                received_at = received_by_item_id.get(item_id)
            if hasattr(received_at, "isoformat"):
                row["received_at"] = received_at.isoformat()
            elif received_at:
                row["received_at"] = received_at

    def _coerce_uuid(self, value: Any) -> UUID | None:
        if isinstance(value, UUID):
            return value
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None

    def _hydrate_missing_certificate_pdfs(self, rows: list[dict[str, Any]]) -> None:
        missing_rows = [row for row in rows if not row.get("certificate_pdfs")]
        item_ids = {
            self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            for row in missing_rows
        }
        item_ids.discard(None)
        if not item_ids:
            return

        payload_by_item_id: dict[Any, dict] = {}
        try:
            for item_id, raw_payload in self.db.execute(
                select(CatalogItem.id, CatalogItem.raw_payload).where(CatalogItem.id.in_(item_ids))
            ):
                payload_by_item_id[item_id] = raw_payload or {}
        except Exception:
            return

        for row in missing_rows:
            item_id = self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            raw_payload = payload_by_item_id.get(item_id) if item_id else None
            row["certificate_pdfs"] = self._certificate_pdfs(raw_payload)

    def _hydrate_missing_supplier_email(self, rows: list[dict[str, Any]]) -> None:
        missing_rows = [row for row in rows if not row.get("email_domain")]
        item_ids = {
            self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            for row in missing_rows
        }
        item_ids.discard(None)
        if not item_ids:
            return

        email_by_item_id: dict[Any, str] = {}
        try:
            for item_id, email_domain in self.db.execute(
                select(CatalogItem.id, Supplier.email_domain)
                .join(Supplier, Supplier.id == CatalogItem.supplier_id)
                .where(CatalogItem.id.in_(item_ids))
            ):
                email_by_item_id[item_id] = email_domain
        except Exception:
            return

        for row in missing_rows:
            item_id = self._coerce_uuid(row.get("id") or row.get("item_id") or row.get("catalog_item_id"))
            if item_id and email_by_item_id.get(item_id):
                row["email_domain"] = email_by_item_id[item_id]

    def _certificate_pdfs(self, raw_payload: dict | None) -> list[dict[str, str]]:
        values = (raw_payload or {}).get("certificate_pdfs")
        if not isinstance(values, list):
            return []
        return [
            {
                "name": str(row.get("name") or "Certificate PDF"),
                "url": str(row.get("url")),
                "type": str(row.get("type") or "Certificate"),
            }
            for row in values
            if isinstance(row, dict) and row.get("url")
        ]

    def answer(
        self,
        question: str,
        tenant_id: Any | None = None,
        user_id: Any | None = None,
    ) -> ChatResponse:
        return self._answer(question, tenant_id=tenant_id, user_id=user_id)

    def _log_query(
        self,
        question: str,
        tenant_id: Any | None,
        user_id: Any | None,
        operation_type: str | None,
    ) -> None:
        if not tenant_id or not user_id:
            return
        try:
            from backend.app.models import AIQueryLog

            self.db.add(
                AIQueryLog(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    query_text=question[:2000],
                    operation_type=operation_type,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()

    def _execute_plan(self, plan, tenant_id: Any | None = None) -> list[dict[str, Any]]:
        if plan.operation in {"supplier_compare", "best_price", "catalog_search"}:
            return self.ranker.ranked_items(plan, tenant_id=tenant_id)
        if plan.operation == "history_compare":
            return self.ranker.ranked_items(plan, tenant_id=tenant_id)
        if plan.operation == "supplier_activity":
            return self.ranker.ranked_items(plan, tenant_id=tenant_id)
        return []

    def _execute_matched_ingredient_query(
        self,
        plan,
        matched_names: list[str],
        tenant_id: Any | None = None,
    ) -> list[dict[str, Any]]:
        if plan.operation not in {"supplier_compare", "best_price", "catalog_search", "history_compare", "supplier_activity"}:
            return []
        return self.ranker.ranked_items(plan, tenant_id=tenant_id, matched_ingredient_names=matched_names)

    def _grounded_sql_question(self, question: str, plan: Any) -> str:
        ingredient_name = getattr(plan, "ingredient_name", None)
        if not ingredient_name:
            return question
        return f"{question}\nMatched catalogue ingredient search phrase: {ingredient_name}"

    def _sort_rows_for_question(self, question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lowered = question.lower()

        def safe_float(value: Any) -> tuple[bool, float]:
            if value is None:
                return (True, 0.0)
            try:
                return (False, float(value))
            except (ValueError, TypeError):
                return (True, 0.0)

        if "sort" in lowered or "order" in lowered:
            if "lead" in lowered:
                return sorted(rows, key=lambda row: safe_float(row.get("lead_time_days")))
            if "quantity" in lowered or "qty" in lowered or "stock" in lowered:
                return sorted(rows, key=lambda row: safe_float(row.get("available_qty")))
            if "moq" in lowered:
                return sorted(rows, key=lambda row: safe_float(row.get("moq")))
            if "date" in lowered or "latest" in lowered or "recent" in lowered:
                return sorted(rows, key=lambda row: str(row.get("received_at") or ""), reverse=True)
            if "price" in lowered or "rate" in lowered or "cost" in lowered:
                return sorted(rows, key=lambda row: safe_float(row.get("price_per_unit")))

        return sorted(
            rows,
            key=lambda row: (
                str(row.get("ingredient_name") or "").lower(),
                str(row.get("specification") or "").lower(),
                str(row.get("supplier_name") or "").lower(),
            ),
        )

    def _looks_like_false_negative(self, answer: str) -> bool:
        lowered = (answer or "").lower()
        return any(
            phrase in lowered
            for phrase in (
                "couldn't find",
                "could not find",
                "no matching",
                "no data",
                "not find any",
                "couldn't locate",
            )
        )

    def _ground_plan_in_catalog(self, question: str, plan, tenant_id: Any | None = None):
        if getattr(plan, "ingredient_name", None):
            matched_item = self._match_catalog_item_name(str(plan.ingredient_name), tenant_id=tenant_id)
            if matched_item and matched_item != plan.ingredient_name:
                return plan.model_copy(update={"ingredient_name": matched_item})
            return plan
        matched_item = self._match_catalog_item_name(question, tenant_id=tenant_id)
        if matched_item:
            return plan.model_copy(update={"ingredient_name": matched_item, "operation": plan.operation if plan.operation != "supplier_activity" else "catalog_search"})
        return plan

    def _match_catalog_item_name(self, question: str, tenant_id: Any | None = None) -> str | None:
        return self._resolve_ingredient_from_db(question, tenant_id=tenant_id).search_phrase

    def _best_ingredient_match_from_candidates(
        self,
        question: str,
        candidates: list[tuple[str | None, str | None]],
    ) -> str | None:
        result = self._best_ingredient_result_from_candidates(question, [row[0] for row in candidates])
        return result.search_phrase

    def _resolve_ingredient_from_db(self, question: str, tenant_id: Any | None = None) -> IngredientMatchResult:
        extracted_phrase = self._extract_ingredient_phrase(question)
        if not extracted_phrase:
            return IngredientMatchResult("", None, [], None, 0.0)

        stmt = select(CatalogItem.ingredient_name).where(CatalogItem.ingredient_name.is_not(None)).distinct().limit(5000)
        if tenant_id:
            stmt = stmt.where(CatalogItem.tenant_id == (UUID(str(tenant_id)) if isinstance(tenant_id, str) else tenant_id))

        names = [name for (name,) in self.db.execute(stmt) if name]
        result = self._best_ingredient_result_from_candidates(extracted_phrase, names)
        logger.info(
            "ProcuraAI ingredient match original_query=%r extracted_ingredient=%r best_database_match=%r confidence=%.3f matched_count=%d generated_sql=%r",
            question,
            result.extracted_phrase,
            result.best_match,
            result.confidence,
            len(result.matched_names),
            "deterministic ingredient_name query" if result.matched_names else "",
        )
        return result

    def _best_ingredient_result_from_candidates(
        self,
        question: str,
        candidates: list[str | None],
    ) -> IngredientMatchResult:
        extracted_phrase = self._extract_ingredient_phrase(question)
        query_tokens = self._ingredient_query_tokens(extracted_phrase)
        if not extracted_phrase or not query_tokens:
            return IngredientMatchResult(extracted_phrase, None, [], None, 0.0)

        canonical_query = self._canonical_ingredient_text(extracted_phrase)
        expanded_query = self._canonical_ingredient_text(" ".join(sorted(query_tokens)))
        scored: list[tuple[float, str, str | None]] = []

        for ingredient_name in dict.fromkeys(name for name in candidates if name):
            score, anchor = self._ingredient_match_score(canonical_query, expanded_query, query_tokens, str(ingredient_name))
            if score > 0:
                scored.append((score, str(ingredient_name), anchor))

        if not scored:
            return IngredientMatchResult(extracted_phrase, None, [], None, 0.0)

        scored.sort(key=lambda row: (-row[0], row[1].lower()))
        best_score, best_name, best_anchor = scored[0]
        if best_score < 0.70:
            return IngredientMatchResult(extracted_phrase, None, [], best_name, best_score)

        broad_anchor = best_anchor or self._best_matching_catalog_token(query_tokens, best_name)
        search_phrase = self._resolved_search_phrase(query_tokens, best_name, broad_anchor)
        search_terms = set(self._canonical_ingredient_text(search_phrase).split())
        matched_names: list[str] = []
        if len(search_terms) > 1:
            for _, candidate, _ in scored:
                candidate_terms = set(self._canonical_ingredient_text(candidate).split())
                if search_terms <= candidate_terms:
                    matched_names.append(candidate)
        elif broad_anchor and len(broad_anchor) >= 4:
            for _, candidate, _ in scored:
                candidate_tokens = self._catalog_name_tokens(candidate)
                canonical_candidate = self._canonical_ingredient_text(candidate)
                if broad_anchor in candidate_tokens or broad_anchor in canonical_candidate:
                    matched_names.append(candidate)

        if not matched_names:
            floor = max(0.70, best_score - 0.12)
            matched_names = [candidate for score, candidate, _ in scored if score >= floor]

        return IngredientMatchResult(
            extracted_phrase=extracted_phrase,
            search_phrase=search_phrase,
            matched_names=list(dict.fromkeys(matched_names)),
            best_match=best_name,
            confidence=best_score,
        )

    def _ingredient_match_score(
        self,
        canonical_query: str,
        expanded_query: str,
        query_tokens: set[str],
        ingredient_name: str,
    ) -> tuple[float, str | None]:
        canonical_name = self._canonical_ingredient_text(ingredient_name)
        name_tokens = self._catalog_name_tokens(ingredient_name)
        if not canonical_name or not name_tokens:
            return (0.0, None)

        score = SequenceMatcher(None, canonical_query, canonical_name).ratio() * 0.45
        anchor: str | None = None
        strong_match = False

        if canonical_query and canonical_query in canonical_name:
            score += 0.55
            strong_match = True
            anchor = canonical_query.split()[0]
        elif expanded_query and expanded_query in canonical_name:
            score += 0.50
            strong_match = True
            anchor = expanded_query.split()[0]

        non_numeric_tokens = [token for token in query_tokens if re.search(r"[a-z]", token)]
        numeric_tokens = [token for token in query_tokens if token not in non_numeric_tokens]
        matched_non_numeric = 0

        for token in non_numeric_tokens:
            if token in name_tokens or any(token in name_token or name_token.startswith(token) for name_token in name_tokens):
                score += 0.28
                strong_match = True
                matched_non_numeric += 1
                anchor = self._matching_name_token(token, name_tokens) or token
                continue
            fuzzy = max((SequenceMatcher(None, token, name_token).ratio() for name_token in name_tokens if len(name_token) >= 4), default=0.0)
            if len(token) >= 4 and fuzzy >= 0.78:
                score += fuzzy * 0.28
                strong_match = True
                matched_non_numeric += 1
                anchor = self._best_fuzzy_name_token(token, name_tokens)

        for token in numeric_tokens:
            if token in name_tokens:
                score += 0.08

        if non_numeric_tokens:
            score += (matched_non_numeric / len(non_numeric_tokens)) * 0.25

        if not strong_match:
            return (0.0, None)
        return (min(score, 1.0), anchor)

    def _matching_name_token(self, query_token: str, name_tokens: set[str]) -> str | None:
        for name_token in name_tokens:
            if query_token == name_token or query_token in name_token or name_token.startswith(query_token):
                return name_token
        return None

    def _best_fuzzy_name_token(self, query_token: str, name_tokens: set[str]) -> str | None:
        best_token = None
        best_score = 0.0
        for name_token in name_tokens:
            if len(name_token) < 4:
                continue
            score = SequenceMatcher(None, query_token, name_token).ratio()
            if score > best_score:
                best_score = score
                best_token = name_token
        return best_token if best_score >= 0.78 else None

    def _resolved_search_phrase(self, query_tokens: set[str], best_name: str, anchor: str | None) -> str:
        name_tokens = self._catalog_name_tokens(best_name)
        ordered = [token for token in self._canonical_ingredient_text(best_name).split() if token in query_tokens]
        if ordered:
            return " ".join(dict.fromkeys(ordered))
        if anchor:
            if "vitamin" in name_tokens and "d3" in query_tokens and "d3" in name_tokens:
                return "vitamin d3"
            return anchor
        return best_name

    def _ingredient_query_tokens(self, value: str) -> set[str]:
        tokens = {
            token
            for token in self._canonical_ingredient_text(value).split()
            if len(token) >= 2 and token not in {
                "any",
                "have",
                "has",
                "find",
                "give",
                "me",
                "get",
                "list",
                "supplier",
                "suppliers",
                "vendor",
                "vendors",
                "name",
                "names",
                "price",
                "rate",
                "cost",
                "stock",
                "inventory",
                "available",
                "availability",
                "sort",
                "show",
                "best",
                "for",
                "and",
                "the",
                "a",
                "an",
                "in",
                "of",
                "with",
                "from",
                "quote",
                "quotation",
                "moq",
                "lead",
                "time",
                "certificate",
                "certificates",
                "certification",
                "certifications",
            }
        }
        return self._expand_abbreviations(tokens)

    def _extract_query_intent(self, question: str) -> str:
        lowered = question.lower()
        if any(token in lowered for token in ("cheapest", "lowest price", "best price", "price", "rate", "cost")):
            return "best_price"
        if any(token in lowered for token in ("supplier", "suppliers", "vendor", "vendors", "who sells", "source")):
            return "supplier_compare"
        return "catalog_search"

    def _extract_ingredient_phrase(self, question: str) -> str:
        tokens = self._ingredient_query_tokens(question)
        if not tokens:
            return ""
        canonical = self._canonical_ingredient_text(question)
        ordered = [token for token in canonical.split() if token in tokens]
        return " ".join(dict.fromkeys(ordered))

    def _looks_like_procurement_question(self, question: str) -> bool:
        lowered = question.lower()
        intent_words = {
            "supplier",
            "suppliers",
            "vendor",
            "vendors",
            "price",
            "rate",
            "cost",
            "stock",
            "available",
            "availability",
            "moq",
            "lead",
            "certificate",
            "certificates",
            "compare",
            "catalog",
            "catalogue",
            "ingredient",
            "item",
            "items",
        }
        return bool(set(re.findall(r"[a-z0-9]+", lowered)) & intent_words)

    def _canonical_ingredient_text(self, value: Any) -> str:
        text = str(value or "").lower()
        text = re.sub(r"(\d+)\s*:\s*(\d+)", r"\1 \2", text)
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return " ".join(text.split())

    def _catalog_name_tokens(self, value: Any) -> set[str]:
        return {
            token
            for token in self._canonical_ingredient_text(value).split()
            if len(token) >= 2
        }

    def _expand_abbreviations(self, tokens: set[str]) -> set[str]:
        expanded = set(tokens)
        abbreviation_map = {
            "vit": "vitamin",
            "vitamin": "vit",
            "d3": "d3",
            "b3": "b3",
            "b12": "b12",
            "mg": "magnesium",
            "zn": "zinc",
            "ca": "calcium",
            "na": "sodium",
            "k": "potassium",
        }
        for token in list(tokens):
            mapped = abbreviation_map.get(token)
            if mapped:
                expanded.add(mapped)
        return expanded

    def _best_matching_catalog_token(self, query_tokens: set[str], ingredient_name: str | None) -> str | None:
        name_tokens = [
            token
            for token in re.sub(r"[^a-z0-9\s]+", " ", str(ingredient_name or "").lower()).split()
            if len(token) >= 3
        ]
        best_token = None
        best_score = 0.0
        for query_token in query_tokens:
            for name_token in name_tokens:
                score = SequenceMatcher(None, query_token, name_token).ratio()
                if score > best_score:
                    best_score = score
                    best_token = name_token
        return best_token if best_score >= 0.78 else None

    def _cache_get(self, key: str) -> str | None:
        try:
            return self.cache.get(key)
        except RedisError:
            return None

    def _cache_set(self, key: str, value: str) -> None:
        try:
            self.cache.setex(key, 300, value)
        except RedisError:
            return

    def _fallback_plan(self, question: str):
        from backend.app.schemas import QueryPlan

        normalized_question = question.lower()
        known_items = [
            "ascorbic acid",
            "nicotinamide",
            "vitamin b3",
            "paracetamol",
            "citric acid",
            "sodium benzoate",
            "magnesium stearate",
            "lactose monohydrate",
            "microcrystalline cellulose",
            "povidone k30",
            "ibuprofen",
            "caffeine anhydrous",
            "zinc sulphate",
            "calcium carbonate",
        ]
        item = next((name for name in known_items if name in normalized_question), None)
        if item is None and "vitamin c" in normalized_question:
            item = "ascorbic acid"

        quantities = [float(value.replace(",", "")) for value in re.findall(r"\d[\d,]*", question)]
        min_quantity = max(quantities) if quantities else None
        operation = "best_price" if any(word in normalized_question for word in ["cheap", "best", "price"]) else "catalog_search"
        return QueryPlan(operation=operation, ingredient_name=item, min_quantity=min_quantity, limit=10)

    def _fallback_summary(self, question: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "I couldn't find any matching data or records in the database for your query. Please check the spelling or try searching for another supplier or chemical ingredient."

        best = rows[0]
        price = best.get("price_display") or (
            f"{best.get('price_per_unit')} {best.get('currency')}/{best.get('unit')}"
            if best.get("price_per_unit") is not None
            else "price not mentioned"
        )
        qty = best.get("quantity_display") or (
            f"{best.get('available_qty')} {best.get('unit')}"
            if best.get("available_qty") is not None
            else "quantity not mentioned"
        )
        lines = [
            (
                f"Found {self._display_item_name(best)} from {best.get('supplier_name')}: "
                f"{price}, {qty} available."
            ),
            "Sorted by available catalogue price.",
        ]
        if len(rows) > 1:
            next_best = rows[1]
            next_price = next_best.get("price_display") or (
                f"{next_best.get('price_per_unit')} {next_best.get('currency')}/{next_best.get('unit')}"
                if next_best.get("price_per_unit") is not None
                else "price not mentioned"
            )
            lines.append(
                f"Next: {next_best.get('supplier_name')} at {next_price}."
            )
        return "\n".join(lines)

    def _display_item_name(self, row: dict[str, Any]) -> str:
        name = row.get("ingredient_name") or "item"
        return f"{name} (U)" if row.get("is_updated") else str(name)
