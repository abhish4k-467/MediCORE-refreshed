import json
import re
from typing import Any
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from backend.app.schemas import ChatResponse
from backend.app.services.llm import OpenRouterClient
from backend.app.services.query_whitelist import validate_operation
from backend.app.services.ranking import SupplierRanker
from backend.app.services.sql_executor import execute_readonly_sql


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
            cache_key = f"chat:answer:v10:{tenant_id}:{question.strip().lower()}"
            cached = self._cache_get(cache_key)
            if cached:
                payload = json.loads(cached)
                self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type="cached")
                return ChatResponse(**payload)

            try:
                plan = self.llm.plan_query(question)
            except Exception:
                plan = self._fallback_plan(question)

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

            plan = self._ground_plan_in_catalog(question, plan, tenant_id=tenant_id)
            self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type=plan.operation)

            # 1. Attempt AI Read-Only SQL Generation & Execution against Supabase Cloud
            rows: list[dict[str, Any]] = []
            try:
                generated_sql = self.llm.generate_sql(question)
                if generated_sql:
                    sql_rows = execute_readonly_sql(self.db, generated_sql, tenant_id=tenant_id)
                    if sql_rows:
                        rows = self._normalize_sql_rows(sql_rows)
            except Exception:
                rows = []

            # 2. Fallback to structured QueryPlan execution if AI SQL produced no results
            if not rows:
                try:
                    rows = self._execute_plan(plan, tenant_id=tenant_id)
                except Exception:
                    rows = []

            try:
                rows = self.ranker._dedupe_supplier_item_rows(rows, plan.ingredient_name)
                rows = self._sort_rows_for_question(question, rows)
            except Exception:
                pass

            try:
                answer = self.llm.summarize_answer(question, rows)
            except Exception:
                answer = self._fallback_summary(question, rows)

            if rows and self._looks_like_false_negative(answer):
                answer = self._fallback_summary(question, rows)

            response = ChatResponse(answer=answer, rows=rows)
            self._cache_set(cache_key, response.model_dump_json())
            return response
        except Exception as e:
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

            if not norm.get("price_display") and norm.get("price_per_unit") is not None:
                currency = norm.get("currency") or "INR"
                unit = norm.get("unit") or "kg"
                norm["price_display"] = f"{currency} {norm['price_per_unit']}/{unit}"

            if not norm.get("quantity_display") and norm.get("available_qty") is not None:
                unit = norm.get("unit") or "kg"
                norm["quantity_display"] = f"{norm['available_qty']} {unit}"

            normalized_list.append(norm)
        return normalized_list

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
            return plan
        matched_item = self._match_catalog_item_name(question, tenant_id=tenant_id)
        if matched_item:
            return plan.model_copy(update={"ingredient_name": matched_item, "operation": plan.operation if plan.operation != "supplier_activity" else "catalog_search"})
        return plan

    def _match_catalog_item_name(self, question: str, tenant_id: Any | None = None) -> str | None:
        from uuid import UUID
        from backend.app.models import CatalogItem

        normalized_question = re.sub(r"[^a-z0-9\s]+", " ", question.lower())
        query_tokens = {
            token
            for token in normalized_question.split()
            if len(token) >= 3 and token not in {"find", "give", "supplier", "suppliers", "price", "sort", "show", "best", "for", "and", "the"}
        }
        if not query_tokens:
            return None

        from sqlalchemy import or_

        query = self.db.query(
            CatalogItem.ingredient_name,
            CatalogItem.raw_payload["specification"].astext.label("specification"),
        ).distinct()
        if tenant_id:
            query = query.filter(CatalogItem.tenant_id == (UUID(str(tenant_id)) if isinstance(tenant_id, str) else tenant_id))

        token_filters = []
        for token in query_tokens:
            token_filters.append(CatalogItem.ingredient_name.ilike(f"%{token}%"))
            token_filters.append(CatalogItem.raw_payload["specification"].astext.ilike(f"%{token}%"))
        if token_filters:
            query = query.filter(or_(*token_filters))

        best_name: str | None = None
        best_score = 0
        for ingredient_name, specification in query.limit(500):
            candidates = [ingredient_name or "", specification or ""]
            for candidate in candidates:
                candidate_lower = candidate.lower()
                candidate_tokens = {
                    token
                    for token in re.sub(r"[^a-z0-9\s]+", " ", candidate_lower).split()
                    if len(token) >= 3
                }
                overlap = query_tokens & candidate_tokens
                score = len(overlap) * 10
                if candidate_lower and candidate_lower in normalized_question:
                    score += 100
                if any(token in candidate_lower for token in query_tokens):
                    score += 25
                if score > best_score:
                    best_score = score
                    best_name = ingredient_name

        if best_score < 10:
            return None
        matched_query_tokens = [
            token
            for token in query_tokens
            if best_name and token in re.sub(r"[^a-z0-9\s]+", " ", best_name.lower()).split()
        ]
        return " ".join(matched_query_tokens) if matched_query_tokens else best_name

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
