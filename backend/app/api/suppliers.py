from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import CatalogEmail, CatalogItem, Supplier
from backend.app.seed_mock_catalogs import build_catalogs
from backend.app.auth import get_current_user

router = APIRouter()


def mock_suppliers() -> list[dict]:
    suppliers, _, _ = build_catalogs()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "email_domain": row.email_domain,
            "country": row.country or "Unknown",
            "last_email_date": row.last_email_date,
            "certifications": row.certifications,
            "item_count": 0,
        }
        for row in sorted(suppliers, key=lambda supplier: supplier.last_email_date, reverse=True)
    ]


@router.get("")
def list_suppliers(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> list[dict]:
    settings = get_settings()
    try:
        user_uuid = UUID(current_user["tenant_id"])
        item_count_subq = (
            select(func.count(CatalogItem.id))
            .where(
                CatalogItem.supplier_id == Supplier.id,
                CatalogItem.tenant_id == user_uuid,
            )
            .scalar_subquery()
        )
        last_catalog_subq = (
            select(func.max(CatalogEmail.received_at))
            .where(
                CatalogEmail.supplier_id == Supplier.id,
                CatalogEmail.tenant_id == user_uuid,
            )
            .scalar_subquery()
        )
        stmt = select(
            Supplier,
            item_count_subq.label("item_count"),
            last_catalog_subq.label("last_catalog_at"),
        ).where(Supplier.tenant_id == user_uuid)

        if not settings.mock_data_enabled:
            stmt = stmt.where(Supplier.email_domain.not_like("%.example"))

        rows = db.execute(stmt.order_by(Supplier.name.asc()))
        return [
            {
                "id": str(row.id),
                "name": row.name,
                "email_domain": row.email_domain,
                "country": row.country or "Unknown",
                "last_email_date": last_catalog_at or row.last_email_date,
                "certifications": row.certifications,
                "item_count": int(item_count or 0),
            }
            for row, item_count, last_catalog_at in rows
        ]
    except SQLAlchemyError:
        if not settings.mock_data_enabled:
            raise
        return mock_suppliers()
