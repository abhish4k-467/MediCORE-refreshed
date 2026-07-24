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
        cache_key = f"chat:answer:v9:{tenant_id}:{question.strip().lower()}"
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

        validate_operation(plan.operation)
        plan = self._ground_plan_in_catalog(question, plan, tenant_id=tenant_id)
        self._log_query(question, tenant_id=tenant_id, user_id=user_id, operation_type=plan.operation)
        rows = self._execute_plan(plan, tenant_id=tenant_id)
        rows = self.ranker._dedupe_supplier_item_rows(rows, plan.normalized_name)
        try:
            answer = self.llm.summarize_answer(question, rows)
        except Exception:
            answer = self._fallback_summary(question, rows)
        if rows and self._looks_like_false_negative(answer):
            answer = self._fallback_summary(question, rows)
        response = ChatResponse(answer=answer, rows=rows)
        self._cache_set(cache_key, response.model_dump_json())
        return response

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
        matched_item = self._match_catalog_item_name(question, tenant_id=tenant_id)
        if matched_item:
            return plan.model_copy(update={"normalized_name": matched_item, "operation": plan.operation if plan.operation != "supplier_activity" else "catalog_search"})
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

        query = self.db.query(CatalogItem.normalized_name, CatalogItem.ingredient_name).distinct()
        if tenant_id:
            query = query.filter(CatalogItem.tenant_id == (UUID(str(tenant_id)) if isinstance(tenant_id, str) else tenant_id))

        best_name: str | None = None
        best_score = 0
        for normalized_name, ingredient_name in query.limit(500):
            candidates = [normalized_name or "", ingredient_name or ""]
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
                    best_name = normalized_name or ingredient_name

        return best_name if best_score >= 10 else None

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
        return QueryPlan(operation=operation, normalized_name=item, min_quantity=min_quantity, limit=10)

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
        name = row.get("normalized_name") or row.get("ingredient_name") or "item"
        return f"{name} (U)" if row.get("is_updated") else str(name)
