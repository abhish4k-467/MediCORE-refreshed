from fastapi import APIRouter, Depends
from sqlalchemy import exists, select
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
            "last_email_date": row.last_email_date,
            "certifications": row.certifications,
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
        stmt = (
            select(Supplier)
            .join(CatalogEmail, CatalogEmail.supplier_id == Supplier.id)
            .where(
                Supplier.tenant_id == user_uuid,
                CatalogEmail.processing_status == "completed",
                exists().where(CatalogItem.catalog_email_id == CatalogEmail.id),
            )
            .distinct()
        )
        if not settings.mock_data_enabled:
            stmt = stmt.where(Supplier.email_domain.not_like("%.example"))
            stmt = stmt.where(CatalogEmail.raw_email_id.not_like("core-mock-catalog-%"))
        rows = db.execute(stmt.order_by(Supplier.last_email_date.desc().nullslast())).scalars()
        return [
            {
                "id": str(row.id),
                "name": row.name,
                "email_domain": row.email_domain,
                "last_email_date": row.last_email_date,
                "certifications": row.certifications,
            }
            for row in rows
        ]
    except SQLAlchemyError:
        if not settings.mock_data_enabled:
            raise
        return mock_suppliers()
