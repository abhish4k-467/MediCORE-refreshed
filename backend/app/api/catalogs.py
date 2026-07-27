from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, exists, func, nullslast, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import CatalogEmail, CatalogItem, EmailAccount, EmailSyncSetting, Supplier
from backend.app.seed_mock_catalogs import build_catalogs
from backend.app.auth import get_current_user
from backend.app.schemas import clean_optional_text

router = APIRouter()


def nullable_float(value):
    return float(value) if value is not None else None


def display_value(raw_payload: dict | None, key: str):
    return clean_optional_text((raw_payload or {}).get(key))


def mock_catalog_emails(limit: int) -> list[dict]:
    suppliers, emails, _ = build_catalogs()
    supplier_names = {supplier.id: supplier.name for supplier in suppliers}
    return [
        {
            "id": str(email.id),
            "supplier_name": supplier_names.get(email.supplier_id, "Mock supplier"),
            "received_at": email.received_at,
            "subject": email.subject,
            "pdf_url": email.pdf_url,
            "processing_status": email.processing_status,
        }
        for email in sorted(emails, key=lambda row: row.received_at, reverse=True)[:limit]
    ]


def mock_catalog_items(q: str | None, limit: int) -> list[dict]:
    suppliers, emails, items = build_catalogs()
    supplier_names = {supplier.id: supplier.name for supplier in suppliers}
    email_received_dates = {email.id: email.received_at for email in emails}
    filtered_items = [item for item in items if not q or q.lower() in item.normalized_name.lower()]
    return [
        {
            "id": str(item.id),
            "catalog_email_id": str(item.catalog_email_id) if getattr(item, "catalog_email_id", None) else None,
            "supplier_name": supplier_names.get(item.supplier_id, "Mock supplier"),
            "ingredient_name": item.ingredient_name,
            "normalized_name": item.normalized_name,
            "specification": display_value(item.raw_payload, "specification"),
            "price_per_unit": nullable_float(item.price_per_unit),
            "currency": item.currency,
            "available_qty": nullable_float(item.available_qty),
            "unit": item.unit,
            "valid_until": item.valid_until,
            "lead_time_days": getattr(item, "lead_time_days", None) if getattr(item, "lead_time_days", None) is not None else (item.raw_payload or {}).get("lead_time_days"),
            "lead_time_text": display_value(item.raw_payload, "lead_time_text"),
            "moq": getattr(item, "moq", None) if getattr(item, "moq", None) is not None else (item.raw_payload or {}).get("moq"),
            "pack_size": display_value(item.raw_payload, "pack_size"),
            "price_display": display_value(item.raw_payload, "price_display"),
            "quantity_display": display_value(item.raw_payload, "quantity_display"),
            "moq_display": display_value(item.raw_payload, "moq_display"),
            "source_document": display_value(item.raw_payload, "source_document"),
            "is_updated": bool((item.raw_payload or {}).get("is_updated")),
            "received_at": email_received_dates.get(item.catalog_email_id) if getattr(item, "catalog_email_id", None) else None,
        }
        for item in sorted(filtered_items, key=lambda row: row.price_per_unit if row.price_per_unit is not None else float("inf"))[:limit]
    ]


@router.get("/emails")
def list_catalog_emails(
    db: Session = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
) -> list[dict]:
    settings = get_settings()
    user_uuid = UUID(current_user["tenant_id"])
    stmt = (
        select(CatalogEmail, Supplier.name)
        .join(Supplier, Supplier.id == CatalogEmail.supplier_id)
        .where(
            CatalogEmail.tenant_id == user_uuid,
            CatalogEmail.processing_status == "completed",
            exists().where(CatalogItem.catalog_email_id == CatalogEmail.id),
        )
    )
    if not settings.mock_data_enabled:
        stmt = stmt.where(CatalogEmail.raw_email_id.not_like("core-mock-catalog-%"))
    stmt = stmt.order_by(CatalogEmail.received_at.desc()).limit(limit)
    try:
        return [
            {
                "id": str(email.id),
                "supplier_name": supplier_name,
                "received_at": email.received_at,
                "subject": email.subject,
                "pdf_url": email.pdf_url,
                "processing_status": email.processing_status,
            }
            for email, supplier_name in db.execute(stmt)
        ]
    except SQLAlchemyError:
        if not settings.mock_data_enabled:
            raise
        return mock_catalog_emails(limit)


@router.get("/items")
def list_catalog_items(
    db: Session = Depends(get_db),
    q: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    latest_only: bool = Query(True),
    current_user: dict = Depends(get_current_user)
) -> list[dict]:
    settings = get_settings()
    user_uuid = UUID(current_user["tenant_id"])
    stmt = (
        select(CatalogItem, Supplier.name, CatalogEmail.received_at, None)
        .join(Supplier, Supplier.id == CatalogItem.supplier_id)
        .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
    )
    if latest_only:
        latest_items = (
            select(
                CatalogItem.id.label("item_id"),
                func.row_number().over(
                    partition_by=(
                        CatalogItem.supplier_id,
                        CatalogItem.normalized_name,
                        CatalogItem.raw_payload["specification"].astext,
                        CatalogItem.available_qty,
                        CatalogItem.unit,
                        CatalogItem.moq,
                    ),
                    order_by=(
                        CatalogEmail.received_at.desc(),
                        CatalogItem.raw_payload["is_updated"].as_boolean().desc().nullslast(),
                        CatalogItem.id.desc(),
                    ),
                ).label("row_number"),
                func.count(CatalogItem.id).over(
                    partition_by=(
                        CatalogItem.supplier_id,
                        CatalogItem.normalized_name,
                        CatalogItem.raw_payload["specification"].astext,
                        CatalogItem.available_qty,
                        CatalogItem.unit,
                        CatalogItem.moq,
                    ),
                ).label("history_count"),
            )
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .where(CatalogItem.tenant_id == user_uuid)
            .subquery()
        )
        stmt = (
            select(CatalogItem, Supplier.name, CatalogEmail.received_at, latest_items.c.history_count)
            .join(Supplier, Supplier.id == CatalogItem.supplier_id)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
        )
        stmt = stmt.join(
            latest_items,
            and_(
                latest_items.c.item_id == CatalogItem.id,
                latest_items.c.row_number == 1,
            ),
        )
    stmt = stmt.where(CatalogItem.tenant_id == user_uuid)
    if not settings.mock_data_enabled:
        source = CatalogItem.raw_payload["source"].astext
        stmt = stmt.where(or_(source.is_(None), source != "mock_extracted_catalogue"))
    if q:
        stmt = stmt.where(
            or_(
                CatalogItem.normalized_name.ilike(f"%{q}%"),
                CatalogItem.ingredient_name.ilike(f"%{q}%"),
                CatalogItem.raw_payload["specification"].astext.ilike(f"%{q}%"),
            )
        )
    stmt = stmt.order_by(nullslast(CatalogItem.price_per_unit.asc())).limit(limit)
    try:
        return [
            {
                "id": str(item.id),
                "catalog_email_id": str(item.catalog_email_id) if item.catalog_email_id else None,
                "supplier_name": supplier_name,
                "ingredient_name": item.ingredient_name,
                "normalized_name": item.normalized_name,
                "specification": display_value(item.raw_payload, "specification"),
                "price_per_unit": nullable_float(item.price_per_unit),
                "currency": item.currency,
                "available_qty": nullable_float(item.available_qty),
                "unit": item.unit,
                "valid_until": item.valid_until,
                "lead_time_days": getattr(item, "lead_time_days", None) if getattr(item, "lead_time_days", None) is not None else (item.raw_payload or {}).get("lead_time_days"),
                "lead_time_text": display_value(item.raw_payload, "lead_time_text"),
                "moq": getattr(item, "moq", None) if getattr(item, "moq", None) is not None else (item.raw_payload or {}).get("moq"),
                "pack_size": display_value(item.raw_payload, "pack_size"),
                "price_display": display_value(item.raw_payload, "price_display"),
                "quantity_display": display_value(item.raw_payload, "quantity_display"),
                "moq_display": display_value(item.raw_payload, "moq_display"),
                "source_document": display_value(item.raw_payload, "source_document"),
                "is_updated": bool((item.raw_payload or {}).get("is_updated")) or bool(history_count and history_count > 1),
                "received_at": received_at,
            }
            for item, supplier_name, received_at, history_count in db.execute(stmt)
        ]
    except SQLAlchemyError:
        if not settings.mock_data_enabled:
            raise
        return mock_catalog_items(q, limit)


@router.get("/sync-diagnostics")
def sync_diagnostics(
    db: Session = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    import json

    tenant_uuid = UUID(current_user["tenant_id"])
    user_uuid = UUID(current_user["id"])

    accounts = db.query(EmailAccount).filter(EmailAccount.user_id == user_uuid).all()
    sync_setting = db.query(EmailSyncSetting).filter(EmailSyncSetting.user_id == user_uuid).first()
    pending_approvals: list[dict] = []
    if sync_setting:
        try:
            parsed = json.loads(sync_setting.pending_approvals or "[]")
            pending_approvals = [item for item in parsed if isinstance(item, dict)]
        except Exception:
            pending_approvals = []

    email_rows = (
        db.query(
            CatalogEmail,
            Supplier.name.label("supplier_name"),
            func.count(CatalogItem.id).label("item_count"),
        )
        .join(Supplier, Supplier.id == CatalogEmail.supplier_id)
        .outerjoin(CatalogItem, CatalogItem.catalog_email_id == CatalogEmail.id)
        .filter(CatalogEmail.tenant_id == tenant_uuid)
        .group_by(CatalogEmail.id, Supplier.name)
        .order_by(CatalogEmail.received_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "tenant_id": str(tenant_uuid),
        "accounts": [
            {
                "id": str(account.id),
                "email_address": account.email_address,
                "sync_status": account.sync_status,
                "sync_error_msg": account.sync_error_msg,
                "last_synced_at": account.last_synced_at.isoformat() if account.last_synced_at else None,
            }
            for account in accounts
        ],
        "sync_settings": {
            "ingestion_approach": sync_setting.ingestion_approach if sync_setting else None,
            "trusted_suppliers": sync_setting.trusted_suppliers if sync_setting else None,
            "keyword_filters": sync_setting.keyword_filters if sync_setting else None,
            "pending_approval_count": len([item for item in pending_approvals if not item.get("ignored")]),
            "pending_approvals": pending_approvals[:limit],
        },
        "recent_emails": [
            {
                "id": str(email.id),
                "raw_email_id": email.raw_email_id,
                "supplier_name": supplier_name,
                "subject": email.subject,
                "received_at": email.received_at.isoformat() if email.received_at else None,
                "processing_status": email.processing_status,
                "item_count": int(item_count or 0),
                "visible_in_catalog": email.processing_status == "completed" and int(item_count or 0) > 0,
            }
            for email, supplier_name, item_count in email_rows
        ],
    }


@router.delete("/emails/{email_id}", status_code=204)
def delete_catalog_email(
    email_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """Delete a specific catalog email and all its extracted catalog items securely."""
    user_uuid = UUID(current_user["tenant_id"])
    email_record = db.query(CatalogEmail).filter(CatalogEmail.id == email_id, CatalogEmail.tenant_id == user_uuid).first()
    if not email_record:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail="Catalog email not found or access denied."
        )
    try:
        email_items = db.query(CatalogItem).filter(CatalogItem.catalog_email_id == email_id).all()
        for item in email_items:
            specification = (item.raw_payload or {}).get("specification")
            specification_filter = (
                CatalogItem.raw_payload["specification"].astext == str(specification)
                if specification
                else or_(
                    CatalogItem.raw_payload["specification"].astext.is_(None),
                    CatalogItem.raw_payload["specification"].astext == "",
                )
            )
            identity_filters = (
                CatalogItem.tenant_id == user_uuid,
                CatalogItem.supplier_id == item.supplier_id,
                CatalogItem.normalized_name == item.normalized_name,
                specification_filter,
                CatalogItem.available_qty == item.available_qty,
                CatalogItem.unit == item.unit,
                CatalogItem.moq == item.moq,
            )
            history_count = (
                db.query(CatalogItem.id)
                .filter(*identity_filters)
                .count()
            )
            if bool((item.raw_payload or {}).get("is_updated")) or history_count > 1:
                db.query(CatalogItem).filter(*identity_filters).delete(synchronize_session=False)
            else:
                db.delete(item)
        # Keep a tombstone so future inbox syncs do not re-import a user-deleted email.
        email_record.processing_status = "deleted"
        db.commit()
    except Exception as e:
        db.rollback()
        from fastapi import HTTPException
        raise HTTPException(
            status_code=500,
            detail="An error occurred while deleting the email record."
        )




