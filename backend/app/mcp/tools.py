import logging
from typing import Any
from uuid import UUID
from sqlalchemy.orm import Session

from backend.app.services.sql_executor import execute_readonly_sql
from backend.app.services.ranking import SupplierRanker
from backend.app.schemas import QueryPlan

logger = logging.getLogger(__name__)


def execute_readonly_sql_tool(
    db: Session,
    sql_query: str,
    tenant_id: str | UUID | None = None
) -> list[dict[str, Any]]:
    """
    MCP tool to execute validated read-only SQL queries against Supabase Cloud Postgres.
    Enforces tenant isolation and read-only safety restrictions.
    """
    return execute_readonly_sql(db=db, sql_query=sql_query, tenant_id=tenant_id)


def perform_catalog_update_tool(
    db: Session,
    catalog_item_id: str | UUID,
    update_data: dict[str, Any],
    tenant_id: str | UUID | None = None
) -> dict[str, Any]:
    """
    MCP tool to perform approved catalog update operations via validated backend logic.
    Does NOT allow direct unrestricted SQL writes.
    """
    from backend.app.models import CatalogItem

    item_uuid = UUID(str(catalog_item_id)) if isinstance(catalog_item_id, str) else catalog_item_id
    query = db.query(CatalogItem).filter(CatalogItem.id == item_uuid)

    if tenant_id:
        tenant_uuid = UUID(str(tenant_id)) if isinstance(tenant_id, str) else tenant_id
        query = query.filter(CatalogItem.tenant_id == tenant_uuid)

    item = query.first()
    if not item:
        raise ValueError(f"Catalog item {catalog_item_id} not found or access denied.")

    allowed_fields = {
        "ingredient_name", "price_per_unit",
        "currency", "available_qty", "unit", "valid_until",
        "lead_time_days", "moq"
    }

    for key, val in update_data.items():
        if key in allowed_fields:
            setattr(item, key, val)
        elif key == "raw_payload" and isinstance(val, dict):
            current_payload = dict(item.raw_payload or {})
            current_payload.update(val)
            item.raw_payload = current_payload

    # Flag item as manually updated in raw_payload
    raw_payload = dict(item.raw_payload or {})
    raw_payload["is_updated"] = True
    item.raw_payload = raw_payload

    db.commit()
    db.refresh(item)

    return {
        "id": str(item.id),
        "ingredient_name": item.ingredient_name,
        "price_per_unit": float(item.price_per_unit) if item.price_per_unit is not None else None,
        "currency": item.currency,
        "available_qty": float(item.available_qty) if item.available_qty is not None else None,
        "unit": item.unit,
        "updated": True
    }


def get_structured_query_results_tool(
    db: Session,
    sql_query: str,
    tenant_id: str | UUID | None = None
) -> dict[str, Any]:
    """
    MCP tool returning structured query results and metadata for front-end or LLM consumption.
    """
    rows = execute_readonly_sql(db=db, sql_query=sql_query, tenant_id=tenant_id)
    return {
        "count": len(rows),
        "query": sql_query,
        "rows": rows
    }


def get_catalog_comparison_data_tool(
    db: Session,
    ingredient_name: str,
    tenant_id: str | UUID | None = None
) -> list[dict[str, Any]]:
    """
    MCP tool returning structured catalog comparison data for a given chemical ingredient or catalog item.
    """
    ranker = SupplierRanker(db)
    plan = QueryPlan(
        operation="supplier_compare",
        ingredient_name=ingredient_name.lower().strip(),
        limit=50
    )
    return ranker.ranked_items(plan=plan, tenant_id=tenant_id)
