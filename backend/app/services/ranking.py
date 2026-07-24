from datetime import datetime
from typing import Any
from sqlalchemy import Select, and_, func, nullslast, select
from sqlalchemy.orm import Session

from backend.app.models import CatalogItem, Supplier
from backend.app.schemas import QueryPlan
from backend.app.schemas import clean_optional_text


class SupplierRanker:
    def __init__(self, db: Session) -> None:
        self.db = db

    def ranked_items(self, plan: QueryPlan, tenant_id: Any | None = None) -> list[dict]:
        from uuid import UUID
        from backend.app.models import CatalogEmail
        from backend.app.config import get_settings
        from sqlalchemy import or_
        
        settings = get_settings()
        latest_items = (
            select(
                CatalogItem.id.label("item_id"),
                func.row_number().over(
                    partition_by=(CatalogItem.supplier_id, CatalogItem.normalized_name),
                    order_by=(
                        CatalogEmail.received_at.desc(),
                        CatalogItem.raw_payload["is_updated"].as_boolean().desc().nullslast(),
                        CatalogItem.id.desc(),
                    ),
                ).label("row_number"),
                func.count(CatalogItem.id).over(
                    partition_by=(CatalogItem.supplier_id, CatalogItem.normalized_name),
                ).label("history_count"),
            )
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .subquery()
        )
        stmt: Select = (
            select(CatalogItem, Supplier, CatalogEmail.received_at, latest_items.c.history_count)
            .join(Supplier, Supplier.id == CatalogItem.supplier_id)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .join(
                latest_items,
                and_(
                    latest_items.c.item_id == CatalogItem.id,
                    latest_items.c.row_number == 1,
                ),
            )
            .order_by(nullslast(CatalogItem.price_per_unit.asc()))
            .limit(plan.limit)
        )
        if not settings.mock_data_enabled:
            stmt = stmt.where(
                or_(
                    CatalogEmail.raw_email_id.is_(None),
                    CatalogEmail.raw_email_id.not_like("core-mock-catalog-%")
                )
            )
        if tenant_id:
            stmt = stmt.where(CatalogItem.tenant_id == (UUID(str(tenant_id)) if isinstance(tenant_id, str) else tenant_id))
        if plan.normalized_name:
            search_term = plan.normalized_name.lower()
            stmt = stmt.where(
                or_(
                    CatalogItem.normalized_name.ilike(f"%{search_term}%"),
                    CatalogItem.ingredient_name.ilike(f"%{search_term}%"),
                )
            )
        if plan.min_quantity:
            stmt = stmt.where(CatalogItem.available_qty >= plan.min_quantity)
        if plan.unit:
            stmt = stmt.where(CatalogItem.unit == plan.unit)

        rows = []
        for item, supplier, received_at, history_count in self.db.execute(stmt):
            price = float(item.price_per_unit) if item.price_per_unit is not None else None
            qty = float(item.available_qty) if item.available_qty is not None else None
            score = 100.0 - (price / 100.0) if price is not None else 0.0
            raw_payload = item.raw_payload or {}
            rows.append(
                {
                    "supplier_name": supplier.name,
                    "email_domain": supplier.email_domain,
                    "certifications": supplier.certifications,
                    "ingredient_name": item.ingredient_name,
                    "normalized_name": item.normalized_name,
                    "price_per_unit": price,
                    "currency": item.currency,
                    "available_qty": qty,
                    "unit": item.unit,
                    "price_display": clean_optional_text(raw_payload.get("price_display")),
                    "quantity_display": clean_optional_text(raw_payload.get("quantity_display")),
                    "lead_time_text": clean_optional_text(raw_payload.get("lead_time_text")),
                    "moq_display": clean_optional_text(raw_payload.get("moq_display")),
                    "is_updated": bool(raw_payload.get("is_updated")) or bool(history_count and history_count > 1),
                    "valid_until": item.valid_until.isoformat() if item.valid_until else None,
                    "received_at": received_at.isoformat() if received_at else None,
                    "recommendation_score": round(score, 4),
                }
            )
        return self._dedupe_supplier_item_rows(rows, plan.normalized_name)

    def _dedupe_supplier_item_rows(self, rows: list[dict], requested_item: str | None = None) -> list[dict]:
        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            supplier_key = str(row.get("email_domain") or row.get("supplier_name") or "").strip().lower()
            item_key = self._canonical_item_key(requested_item or row.get("normalized_name") or row.get("ingredient_name"))
            key = (
                supplier_key,
                item_key,
            )
            current = grouped.get(key)
            if current is None or self._row_is_newer(row, current):
                grouped[key] = row
        return list(grouped.values())

    def _canonical_item_key(self, value: Any) -> str:
        import re

        text = re.sub(r"\(u\)", "", str(value or ""), flags=re.IGNORECASE)
        text = re.sub(r"[^a-z0-9]+", " ", text.lower())
        return " ".join(text.split())

    def _row_is_newer(self, candidate: dict, current: dict) -> bool:
        if bool(candidate.get("is_updated")) != bool(current.get("is_updated")):
            return bool(candidate.get("is_updated"))
        candidate_time = self._row_time(candidate)
        current_time = self._row_time(current)
        if candidate_time != current_time:
            return candidate_time > current_time
        return self._display_richness(candidate) > self._display_richness(current)

    def _row_time(self, row: dict) -> datetime:
        value = row.get("received_at")
        if not value:
            return datetime.min
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return datetime.min

    def _display_richness(self, row: dict) -> int:
        value = f"{row.get('price_display') or ''} {row.get('quantity_display') or ''}"
        score = len(value)
        if any(token in value.upper() for token in ("USD", "INR", "EUR", "GBP", "$", "₹", "€", "£")):
            score += 30
        if "/" in value or any(token in value.lower() for token in ("kg", "g", "mg", "bag", "drum")):
            score += 20
        return score
