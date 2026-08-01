import logging
import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import and_, exists, func, nullslast, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from uuid import UUID

from backend.app.config import get_settings
from backend.app.db import get_db, get_supabase
from backend.app.models import CatalogEmail, CatalogItem, EmailAccount, EmailSyncSetting, Supplier
from backend.app.seed_mock_catalogs import build_catalogs
from backend.app.auth import get_current_user
from backend.app.schemas import clean_optional_text

router = APIRouter()
logger = logging.getLogger(__name__)


def nullable_float(value):
    return float(value) if value is not None else None


def display_value(raw_payload: dict | None, key: str):
    return clean_optional_text((raw_payload or {}).get(key))


def certificate_pdfs(raw_payload: dict | None) -> list[dict]:
    values = (raw_payload or {}).get("certificate_pdfs")
    if not isinstance(values, list):
        return []
    return [
        {
            "name": clean_optional_text(row.get("name")) or "Certificate PDF",
            "url": clean_optional_text(row.get("url")),
            "type": clean_optional_text(row.get("type")) or "Certificate",
        }
        for row in values
        if isinstance(row, dict) and clean_optional_text(row.get("url"))
    ]


def canonical_search_text(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def search_tokens(value: object) -> list[str]:
    return [
        token
        for token in canonical_search_text(value).split()
        if len(token) >= 2 and token not in {"price", "qty", "item", "supplier", "find", "show", "best", "for", "the", "and"}
    ]


def row_relevance(row: dict, query: str | None) -> float:
    if not query:
        return 0.0
    needle = canonical_search_text(query)
    name = canonical_search_text(row.get("ingredient_name"))
    spec = canonical_search_text(row.get("specification"))
    haystack = f"{name} {spec}".strip()
    if not needle or not haystack:
        return 0.0

    score = 0.0
    if name == needle:
        score += 1000
    if needle in name:
        score += 750
    elif needle in haystack:
        score += 600
    tokens = search_tokens(query)
    if tokens:
        haystack_tokens = set(haystack.split())
        name_tokens = set(name.split())
        matched = sum(1 for token in tokens if token in haystack_tokens or any(token in name_token or name_token in token for name_token in name_tokens))
        score += (matched / len(tokens)) * 300
        if matched == len(tokens):
            score += 150
    score += SequenceMatcher(None, needle, name).ratio() * 100
    return score


def _storage_object_path_from_public_url(url: str | None) -> str | None:
    if not url:
        return None
    marker = "/storage/v1/object/public/"
    parsed_path = urlparse(url).path
    if marker not in parsed_path:
        return None
    bucket_and_path = parsed_path.split(marker, 1)[1]
    bucket_prefix = f"{get_settings().supabase_storage_bucket}/"
    if not bucket_and_path.startswith(bucket_prefix):
        return None
    return unquote(bucket_and_path[len(bucket_prefix):])


def delete_storage_object(object_path: str | None) -> None:
    if not object_path:
        return
    try:
        get_supabase().storage.from_(get_settings().supabase_storage_bucket).remove([object_path])
    except Exception:
        logger.warning("Failed to delete catalog attachment object %s", object_path, exc_info=True)


def certificate_storage_paths(raw_payload: dict | None) -> list[str]:
    values = (raw_payload or {}).get("certificate_pdfs")
    if not isinstance(values, list):
        return []
    return [
        path
        for row in values
        if isinstance(row, dict)
        for path in [clean_optional_text(row.get("storage_path"))]
        if path
    ]


def mock_catalog_emails(limit: int) -> list[dict]:
    suppliers, emails, _ = build_catalogs()
    supplier_names = {supplier.id: supplier.name for supplier in suppliers}
    return [
        {
            "id": str(email.id),
            "supplier_name": supplier_names.get(email.supplier_id, "Mock supplier"),
            "email_domain": "",
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
    filtered_items = [item for item in items if not q or q.lower() in item.ingredient_name.lower()]
    return [
        {
            "id": str(item.id),
            "catalog_email_id": str(item.catalog_email_id) if getattr(item, "catalog_email_id", None) else None,
            "supplier_name": supplier_names.get(item.supplier_id, "Mock supplier"),
            "email_domain": "",
            "ingredient_name": item.ingredient_name,
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
            "certificate_pdfs": certificate_pdfs(item.raw_payload),
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
        select(CatalogEmail, Supplier.name, Supplier.email_domain)
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
                "email_domain": email_domain,
                "received_at": email.received_at,
                "subject": email.subject,
                "pdf_url": email.pdf_url,
                "processing_status": email.processing_status,
            }
            for email, supplier_name, email_domain in db.execute(stmt)
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
        select(CatalogItem, Supplier.name, Supplier.email_domain, CatalogEmail.received_at, None)
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
                        CatalogItem.ingredient_name,
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
                        CatalogItem.ingredient_name,
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
            select(CatalogItem, Supplier.name, Supplier.email_domain, CatalogEmail.received_at, latest_items.c.history_count)
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
        tokens = search_tokens(q)
        if tokens:
            stmt = stmt.where(
                or_(*[
                    or_(
                        CatalogItem.ingredient_name.ilike(f"%{token}%"),
                        CatalogItem.raw_payload["specification"].astext.ilike(f"%{token}%"),
                    )
                    for token in tokens
                ])
            )
        else:
            stmt = stmt.where(
                or_(
                    CatalogItem.ingredient_name.ilike(f"%{q}%"),
                    CatalogItem.raw_payload["specification"].astext.ilike(f"%{q}%"),
                )
            )
    stmt = stmt.order_by(CatalogItem.ingredient_name.asc()).limit(limit)
    try:
        rows = [
            {
                "id": str(item.id),
                "catalog_email_id": str(item.catalog_email_id) if item.catalog_email_id else None,
                "supplier_name": supplier_name,
                "email_domain": email_domain,
                "ingredient_name": item.ingredient_name,
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
                "certificate_pdfs": certificate_pdfs(item.raw_payload),
                "is_updated": bool((item.raw_payload or {}).get("is_updated")) or bool(history_count and history_count > 1),
                "received_at": received_at,
            }
            for item, supplier_name, email_domain, received_at, history_count in db.execute(stmt)
        ]
        if q:
            rows.sort(
                key=lambda row: (
                    -row_relevance(row, q),
                    str(row.get("ingredient_name") or "").lower(),
                    row.get("price_per_unit") if row.get("price_per_unit") is not None else float("inf"),
                )
            )
        return rows
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
    background_tasks: BackgroundTasks,
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
    object_path = _storage_object_path_from_public_url(email_record.pdf_url)
    certificate_paths = [
        path
        for (raw_payload,) in db.query(CatalogItem.raw_payload).filter(
            CatalogItem.catalog_email_id == email_id,
            CatalogItem.tenant_id == user_uuid,
        )
        for path in certificate_storage_paths(raw_payload)
    ]
    try:
        db.query(CatalogItem).filter(
            CatalogItem.catalog_email_id == email_id,
            CatalogItem.tenant_id == user_uuid,
        ).delete(synchronize_session=False)
        # Keep a tombstone so future inbox syncs do not re-import a user-deleted email.
        email_record.processing_status = "deleted"
        email_record.pdf_url = None
        db.commit()
        background_tasks.add_task(delete_storage_object, object_path)
        for certificate_path in dict.fromkeys(certificate_paths):
            background_tasks.add_task(delete_storage_object, certificate_path)
    except Exception as e:
        db.rollback()
        from fastapi import HTTPException
        raise HTTPException(
            status_code=500,
            detail="An error occurred while deleting the email record."
        )




