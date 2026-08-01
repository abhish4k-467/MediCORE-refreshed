import email
import email.utils
import csv
from collections import defaultdict
from email.header import decode_header
from html.parser import HTMLParser
import io
import imaplib
import logging
import re
import tempfile

import httpx
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any
from uuid import uuid4, UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db import get_supabase
from backend.app.models import CatalogEmail, CatalogItem, Supplier
from backend.app.services.catalog_table_parser import (
    CATALOG_TABLE_PARSER_VERSION,
    _header_map,
    extract_pack_size,
    parse_catalog_table_text,
)
from backend.app.services.gmail_api import GmailApiClient
from backend.app.services.llm import OpenRouterClient
from backend.app.services.normalizer import normalize_item
from backend.app.services.pdf_extract import extract_pdf_text
from backend.app.schemas import clean_optional_text

logger = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 30 * 1024 * 1024

SUPPLIER_INTENT_TERMS = (
    "catalog",
    "catalogue",
    "price",
    "pricing",
    "quote",
    "quotation",
    "rfq",
    "offer",
    "coa",
    "certificate of analysis",
    "specification",
    "availability",
    "stock",
    "ingredient",
    "chemical",
    "api",
    "excipient",
    "raw material",
    "bulk",
)

IRRELEVANT_MAIL_TERMS = (
    "unsubscribe",
    "newsletter",
    "webinar",
    "event",
    "promotion",
    "promotional",
    "marketing",
    "sale ends",
    "limited time",
    "digest",
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
)


def get_supplier_domain(sender: str) -> str:
    if "@" not in sender:
        return sender.lower()
    return sender.strip().lower()


def _nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.parts.append(cleaned)

    def text(self) -> str:
        return "\n".join(self.parts)


class EmailIngestionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.llm = OpenRouterClient()
        logger.info("MediCORE extraction engine ready: parser=%s", CATALOG_TABLE_PARSER_VERSION)

    def _extract_sender(self, message: Message) -> tuple[str, str]:
        from_header = message.get("From", "")
        try:
            decoded_parts = decode_header(from_header)
            decoded_from = []
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    decoded_from.append(part.decode(encoding or "utf-8", errors="ignore"))
                else:
                    decoded_from.append(part)
            from_header_str = "".join(decoded_from)
        except Exception:
            from_header_str = from_header

        sender_pair = email.utils.parseaddr(from_header_str)
        display_name = sender_pair[0]
        sender = sender_pair[1]

        # Clean up display name
        if display_name:
            display_name = display_name.strip().strip('"').strip("'").strip()
            display_name = " ".join(display_name.split())

        # Check body for forwarded sender pattern if display name is empty or matches email
        body_text = self._get_email_body_text(message)
        if (not display_name or display_name.lower() == sender.lower()) and body_text:
            import re
            body_match = re.search(r"(?mi)^\s*(?:from|From):\s*([^\n<]+)<([^>@]+@[^>]+)>", body_text)
            if body_match:
                body_name = body_match.group(1).strip().strip('"').strip("'").strip()
                body_email = body_match.group(2).strip()
                if body_name:
                    display_name = body_name
                    if "@" in body_email:
                        sender = body_email

        return display_name, sender

    def preview_imap_inbox(
        self,
        imap_username: str | None = None,
        imap_password: str | None = None,
        imap_mailbox: str | None = None,
    ) -> dict:
        using_supplied_credentials = bool(imap_username and imap_password)
        if self.settings.email_mode != "imap" and not using_supplied_credentials:
            return {"email_mode": self.settings.email_mode, "unread_count": 0, "pdf_messages": []}

        username = imap_username or self.settings.imap_username
        password = imap_password or self.settings.imap_password
        mailbox = imap_mailbox or self.settings.imap_mailbox

        with imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, timeout=30) as client:
            client.login(username, password)
            client.select(mailbox)
            _, message_ids = client.uid("search", None, "UNSEEN")
            ids = message_ids[0].split() if message_ids and message_ids[0] else []
            pdf_messages = []
            for msg_id in ids:
                _, data = client.uid("fetch", msg_id, "(BODY.PEEK[])")
                if not data or not isinstance(data[0], tuple):
                    continue
                message = email.message_from_bytes(data[0][1])
                attachments = [att["filename"] for att in self._collect_attachments(message)]
                if attachments:
                    display_name, sender = self._extract_sender(message)
                    pdf_messages.append(
                        {
                            "raw_email_id": f"{username}:{mailbox}:{msg_id.decode()}",
                            "from": display_name or sender,
                            "email": sender,
                            "subject": message.get("Subject"),
                            "pdf_attachments": attachments,
                        }
                    )
            return {
                "email_mode": "imap" if using_supplied_credentials else self.settings.email_mode,
                "mailbox": mailbox,
                "unread_count": len(ids),
                "pdf_message_count": len(pdf_messages),
                "pdf_messages": pdf_messages,
            }

    def poll_imap_inbox(
        self,
        imap_username: str | None = None,
        imap_password: str | None = None,
        imap_mailbox: str | None = None,
    ) -> int:
        using_supplied_credentials = bool(imap_username and imap_password)
        if self.settings.email_mode != "imap" and not using_supplied_credentials:
            logger.info("Skipping IMAP poll because EMAIL_MODE=%s", self.settings.email_mode)
            return 0

        username = imap_username or self.settings.imap_username
        password = imap_password or self.settings.imap_password
        mailbox = imap_mailbox or self.settings.imap_mailbox

        processed = 0
        logger.info("Connecting to IMAP mailbox %s:%s/%s", self.settings.imap_host, self.settings.imap_port, mailbox)
        with imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, timeout=30) as client:
            client.login(username, password)
            client.select(mailbox)
            _, message_ids = client.uid("search", None, "UNSEEN")
            ids = message_ids[0].split() if message_ids and message_ids[0] else []
            logger.info("Found %s unread IMAP message(s)", len(ids))
            for msg_id in ids:
                logger.info("Fetching IMAP message id=%s", msg_id.decode())
                _, data = client.uid("fetch", msg_id, "(RFC822)")
                if not data or not isinstance(data[0], tuple):
                    logger.info("Skipping IMAP message id=%s because it had no RFC822 payload", msg_id.decode())
                    continue
                message = email.message_from_bytes(data[0][1])
                processed += self._process_message(
                    message,
                    raw_email_id=f"{username}:{mailbox}:{msg_id.decode()}",
                )
        logger.info("IMAP poll completed; extracted %s catalogue item(s)", processed)
        return processed

    def process_gmail_push_payload(self, payload: dict) -> int:
        if not self.settings.gmail_oauth_token:
            return 0

        processed = 0
        gmail = GmailApiClient()
        for message_id, message in gmail.fetch_unread_pdf_messages():
            processed += self._process_message(message, raw_email_id=message_id)
        return processed

    def _process_message(
        self,
        message: Message,
        raw_email_id: str,
        parse_targets: list[dict] | None = None,
        tenant_id: Any | None = None,
    ) -> int:
        if self._email_has_items(raw_email_id, tenant_id=tenant_id):
            logger.info("Skipping already-extracted email id=%s", raw_email_id)
            return 0

        display_name, sender = self._extract_sender(message)
        subject = message.get("Subject")
        email_date = self._message_received_at(message)

        if parse_targets is None:
            attachments = self._collect_attachments(message)
            body_text = self._get_email_body_text(message)
            parse_targets = []
            for att in attachments:
                parse_targets.append({
                    "name": att["filename"],
                    "payload": att["payload"],
                    "ext": att["ext"],
                    "mime_type": att["mime_type"],
                    "is_body": False
                })
            if body_text.strip():
                parse_targets.append({
                    "name": "email_body.txt",
                    "payload": body_text.encode("utf-8"),
                    "ext": ".txt",
                    "mime_type": "text/plain",
                    "is_body": True
                })

        logger.info("Processing email id=%s from=%s subject=%r parse_targets=%s", raw_email_id, sender, subject, len(parse_targets))
        if not parse_targets:
            return 0

        supplier = self._upsert_supplier(sender, display_name=display_name, tenant_id=tenant_id)
        count = 0
        active_tenant_id = tenant_id or supplier.tenant_id
        catalog_email = (
            self.db.query(CatalogEmail)
            .filter(CatalogEmail.raw_email_id == raw_email_id)
            .filter(CatalogEmail.tenant_id == active_tenant_id)
            .first()
        )
        if catalog_email:
            logger.info("Reprocessing existing source email record id=%s", raw_email_id)
            catalog_email.processing_status = "processing"
            catalog_email.subject = subject
        else:
            catalog_email = CatalogEmail(
                id=uuid4(),
                tenant_id=active_tenant_id,
                supplier_id=supplier.id,
                raw_email_id=raw_email_id,
                subject=subject,
                pdf_url=None,
                received_at=email_date,
                processing_status="processing",
            )
            self.db.add(catalog_email)
        self.db.flush()

        uploaded_object_paths: list[str] = []
        processing_errors: list[str] = []
        for target in parse_targets:
            target_name = str(target["name"]).replace("\\", "/").split("/")[-1].strip()
            if not target_name:
                target_name = "email_payload.txt" if target.get("is_body") else f"attachment-{uuid4()}"
            payload = target["payload"]
            ext = target["ext"]
            mime_type = target["mime_type"]
            if len(payload) > MAX_DOCUMENT_BYTES:
                logger.warning("Skipping %s because it exceeds the 30 MB processing limit", target_name)
                processing_errors.append(f"{target_name}: file exceeds 30 MB")
                continue

            logger.info("Processing target %s (%s bytes)", target_name, len(payload))
            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    file_path = Path(tmp_dir) / target_name
                    file_path.write_bytes(payload)
                    uploaded_url, object_path = self._upload_file(file_path, raw_email_id, mime_type)
                    uploaded_object_paths.append(object_path)
                    if not catalog_email.pdf_url:
                        catalog_email.pdf_url = uploaded_url

                    text = self._extract_text_from_file(file_path, ext)
                    logger.info("Extracted %s characters of text from %s", len(text), target_name)
                    extracted = self._extract_items_from_text(
                        text,
                        target_name,
                        reference_date=catalog_email.received_at,
                    )
                    count += self._store_catalog_items(
                        catalog_email,
                        supplier,
                        extracted,
                        text,
                        tenant_id=tenant_id,
                        source_name=target_name,
                    )
                except Exception as exc:
                    logger.exception("Failed processing target %s for email id=%s", target_name, raw_email_id)
                    processing_errors.append(f"{target_name}: {exc}")
        if count > 0:
            catalog_email.processing_status = "completed"
            self._touch_supplier_last_email(supplier, catalog_email.received_at)
            self._delete_uploaded_files(uploaded_object_paths)
            catalog_email.pdf_url = None
        else:
            if processing_errors:
                catalog_email.processing_status = f"failed: {'; '.join(processing_errors)}"[:50]
            else:
                catalog_email.processing_status = "empty"
            logger.warning("No catalogue rows were stored for email id=%s", raw_email_id)
        self.db.commit()
        logger.info("Committed %s catalogue item(s) for email id=%s", count, raw_email_id)
        return count

    def reprocess_empty_catalog_emails(self, limit: int = 25, force: bool = False) -> int:
        if force:
            empty_emails = (
                self.db.query(CatalogEmail)
                .filter(CatalogEmail.pdf_url.isnot(None))
                .order_by(CatalogEmail.received_at.desc())
                .limit(limit)
                .all()
            )
        else:
            empty_emails = (
                self.db.query(CatalogEmail)
                .outerjoin(CatalogItem, CatalogItem.catalog_email_id == CatalogEmail.id)
                .filter(CatalogItem.id.is_(None), CatalogEmail.pdf_url.isnot(None))
                .order_by(CatalogEmail.received_at.desc())
                .limit(limit)
                .all()
            )

        processed = 0
        for catalog_email in empty_emails:
            if not catalog_email.pdf_url or not catalog_email.pdf_url.startswith(("http://", "https://")):
                continue
            supplier = self.db.query(Supplier).filter(Supplier.id == catalog_email.supplier_id).first()
            if not supplier:
                continue
            logger.info("Reprocessing stored attachment for email id=%s", catalog_email.raw_email_id)
            with tempfile.TemporaryDirectory() as tmp_dir:
                attachment_name = catalog_email.raw_email_id.split(":")[-1]
                ext = Path(attachment_name.lower()).suffix if ":" in catalog_email.raw_email_id else ".pdf"
                if not ext:
                    ext = ".pdf"
                file_path = Path(tmp_dir) / f"{catalog_email.id}{ext}"
                response = httpx.get(catalog_email.pdf_url, timeout=60)
                response.raise_for_status()
                if len(response.content) > MAX_DOCUMENT_BYTES:
                    logger.warning("Skipping reprocess for %s because stored file exceeds 30 MB", catalog_email.raw_email_id)
                    continue
                file_path.write_bytes(response.content)
                catalog_email.processing_status = "processing"
                if force:
                    self.db.query(CatalogItem).filter(
                        CatalogItem.catalog_email_id == catalog_email.id
                    ).delete(synchronize_session=False)

                text = self._extract_text_from_file(file_path, ext)
                logger.info("Extracted %s characters while reprocessing email id=%s", len(text), catalog_email.raw_email_id)
                extracted = self._extract_items_from_text(
                    text,
                    str(catalog_email.id),
                    reference_date=catalog_email.received_at,
                )
                processed += self._store_catalog_items(catalog_email, supplier, extracted, text, tenant_id=catalog_email.tenant_id)
                catalog_email.processing_status = "completed"
                self._touch_supplier_last_email(supplier, catalog_email.received_at)
        self.db.commit()
        logger.info("Reprocessed %s catalogue item(s) from stored attachments", processed)
        return processed

    def _extract_items_from_text(
        self,
        text: str,
        source_name: str,
        reference_date: datetime | None = None,
    ):
        if not text.strip():
            logger.info("No text available for %s", source_name)
            return []

        parser_text = self._preferred_parser_text(text)
        parsed = [
            normalize_item(item)
            for item in parse_catalog_table_text(
                parser_text,
                reference_date=reference_date,
                dedupe="[EXCEL TABLE]" not in text and "[CSV TABLE]" not in text,
            )
        ]
        preserves_table_duplicates = "[EXCEL TABLE]" in text or "[CSV TABLE]" in text
        if not preserves_table_duplicates:
            parsed = self._dedupe_extracted_items(parsed)
        source_lower = source_name.lower()
        conversational_source = source_lower.endswith(".txt") or "email_body" in source_lower
        logger.info("Deterministic table parser extracted %s catalogue row(s) from %s", len(parsed), source_name)

        if len(parsed) >= (3 if conversational_source else 20):
            logger.info(
                "Using %s deterministic parser row(s) for catalogue %s; skipping LLM fallback",
                len(parsed),
                source_name,
            )
            return parsed

        if ("[EXCEL TABLE]" in text or "[CSV TABLE]" in text) and parsed:
            logger.info(
                "Using %s deterministic structured table parser row(s) for catalogue %s; skipping LLM fallback",
                len(parsed),
                source_name,
            )
            return parsed

        if not getattr(self, "llm", None):
            return parsed

        try:
            llm_items = [normalize_item(item) for item in self.llm.extract_catalog_items(text, reference_date=reference_date)]
            extracted = [*parsed, *llm_items] if preserves_table_duplicates else self._dedupe_extracted_items([*parsed, *llm_items])
            logger.info("LLM fallback extracted %s catalogue row(s) from %s", len(extracted), source_name)
            return extracted
        except Exception:
            logger.exception("LLM extraction failed for %s", source_name)
            return parsed

    def _preferred_parser_text(self, text: str) -> str:
        marker = "[GRID CELL TABLE OCR]\n"
        if marker not in text:
            return text
        grid_text = text.split(marker, 1)[1].split("\n\n", 1)[0].strip()
        return grid_text or text

    def _dedupe_extracted_items(self, items) -> list:
        deduped = []
        seen: set[tuple] = set()
        for item in items:
            key = (
                item.ingredient_name.strip().lower(),
                self._item_specification(item),
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
            deduped.append(item)
        return deduped

    def _store_catalog_items(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        items,
        text: str,
        tenant_id: Any | None = None,
        source_name: str | None = None,
    ) -> int:
        count = 0
        active_tenant_id = tenant_id or supplier.tenant_id
        prepared_items = []
        for item in items:
            item = self._with_source_note(item, text)
            if not self._has_required_grounded_values(item):
                logger.warning(
                    "Skipping extracted item with missing required grounded values: %s",
                    item.model_dump(mode="json"),
                )
                continue
            prepared_items.append(item)

        existing_by_identity = self._existing_supplier_items_by_identity(
            catalog_email,
            supplier,
            prepared_items,
            active_tenant_id,
        )

        for item in prepared_items:
            existing_candidates = existing_by_identity.get(self._item_identity_key(item), [])
            existing_item = existing_candidates[0] if len(existing_candidates) == 1 else None
            has_changed = True
            if existing_candidates:
                has_changed = self._catalog_item_values_changed(existing_candidates[0], item)

            if not has_changed:
                logger.info(
                    "Skipping unchanged catalogue item supplier=%s item=%s",
                    supplier.email_domain,
                    item.ingredient_name,
                )
                continue

            raw_payload = self._compact_payload(item.model_dump(mode="json"))
            raw_payload["source"] = "email_extracted_catalogue"
            if clean_optional_text(source_name):
                raw_payload["source_document"] = clean_optional_text(source_name)
            pack_size = clean_optional_text(self._pack_size_for_item(text, item.ingredient_name))
            if pack_size:
                raw_payload["pack_size"] = pack_size
            raw_payload.update(self._compact_payload(self._notes_payload(item.notes)))
            raw_payload.update(self._compact_payload(self._exact_display_payload(item, text)))
            if existing_item:
                logger.info(
                    "Updating existing catalogue item supplier=%s item=%s from email id=%s",
                    supplier.email_domain,
                    item.ingredient_name,
                    catalog_email.raw_email_id,
                )
                raw_payload["is_updated"] = True
                existing_item.catalog_email_id = catalog_email.id
                existing_item.ingredient_name = item.ingredient_name
                existing_item.price_per_unit = item.price_per_unit
                existing_item.currency = item.currency
                existing_item.available_qty = item.available_qty
                existing_item.unit = item.unit
                existing_item.valid_until = item.valid_until
                existing_item.lead_time_days = item.lead_time_days
                existing_item.moq = item.moq
                existing_item.raw_payload = raw_payload
            else:
                self.db.add(
                    CatalogItem(
                        id=uuid4(),
                        tenant_id=active_tenant_id,
                        catalog_email_id=catalog_email.id,
                        supplier_id=supplier.id,
                        ingredient_name=item.ingredient_name,
                        price_per_unit=item.price_per_unit,
                        currency=item.currency,
                        available_qty=item.available_qty,
                        unit=item.unit,
                        valid_until=item.valid_until,
                        lead_time_days=item.lead_time_days,
                        moq=item.moq,
                        raw_payload=raw_payload,
                    )
                )
            count += 1
        return count

    def _with_source_note(self, item, text: str):
        notes = item.notes or ""
        if "source=" in notes.lower() or "source:" in notes.lower():
            return item

        ingredient = (item.ingredient_name or "").lower().strip()

        for line in text.splitlines():
            normalized_line = " ".join(line.split())
            if not normalized_line:
                continue
            line_lower = normalized_line.lower()
            if ingredient and ingredient in line_lower:
                if item.price_per_unit is None or self._price_appears_in_line(item.price_per_unit, normalized_line):
                    safe_line = normalized_line[:500].replace("'", "")
                    joined_notes = f"{notes}; source='{safe_line}'" if notes else f"source='{safe_line}'"
                    return item.model_copy(update={"notes": joined_notes})

        # Fallback: if ingredient partially matches or any line contains price/qty
        for line in text.splitlines():
            normalized_line = " ".join(line.split())
            if not normalized_line:
                continue
            line_lower = normalized_line.lower()
            if ingredient and any(w in line_lower for w in ingredient.split() if len(w) > 3):
                safe_line = normalized_line[:500].replace("'", "")
                joined_notes = f"{notes}; source='{safe_line}'" if notes else f"source='{safe_line}'"
                return item.model_copy(update={"notes": joined_notes})

        # Ultimate fallback for valid items: tag with source line 1 if available
        first_line = next((l.strip() for l in text.splitlines() if l.strip()), "")
        if first_line:
            safe_line = first_line[:500].replace("'", "")
            joined_notes = f"{notes}; source='{safe_line}'" if notes else f"source='{safe_line}'"
            return item.model_copy(update={"notes": joined_notes})

        return item

    def _price_appears_in_line(self, value: Any, line: str) -> bool:
        try:
            number = float(value)
        except Exception:
            return False

        compact_line = line.replace(",", "")
        variants = {
            str(int(number)) if number.is_integer() else f"{number:g}",
            f"{number:.2f}",
            f"{number:.4f}".rstrip("0").rstrip("."),
        }
        return any(variant in compact_line for variant in variants if variant)

    def _has_required_grounded_values(self, item) -> bool:
        if not clean_optional_text(getattr(item, "ingredient_name", None)):
            return False
        if item.price_per_unit is not None and float(item.price_per_unit) <= 0:
            return False
        if item.available_qty is not None and float(item.available_qty) < 0:
            return False
        notes = (item.notes or "").lower()
        grounded_markers = ("source=", "source:", "original_price=", "original_quantity=", "lead_time=")
        return any(marker in notes for marker in grounded_markers)

    def _existing_supplier_items_by_identity(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        items,
        tenant_id: Any,
    ) -> dict[tuple, list[CatalogItem]]:
        ingredient_names = {
            item.ingredient_name.strip().lower()
            for item in items
            if clean_optional_text(item.ingredient_name)
        }
        if not ingredient_names:
            return {}

        previous_items = (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .filter(
                CatalogItem.tenant_id == tenant_id,
                CatalogItem.supplier_id == supplier.id,
                func.lower(CatalogItem.ingredient_name).in_(ingredient_names),
                CatalogItem.catalog_email_id != catalog_email.id,
            )
            .order_by(CatalogEmail.received_at.desc(), CatalogItem.id.desc())
            .all()
        )
        grouped: dict[tuple, list[CatalogItem]] = defaultdict(list)
        for previous in previous_items:
            grouped[self._item_identity_key(previous)].append(previous)
        return dict(grouped)

    def _catalog_item_values_changed(self, previous: CatalogItem, item) -> bool:
        return any(
            [
                _nullable_float(previous.price_per_unit) != _nullable_float(item.price_per_unit),
                (previous.currency or "").upper() != (item.currency or "").upper(),
                (previous.lead_time_days or None) != (item.lead_time_days or None),
                (previous.raw_payload or {}).get("lead_time_text") != (item.lead_time_text or None),
                self._item_specification(previous) != self._item_specification(item),
            ]
        )

    def _catalog_item_changed(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        item,
        tenant_id: Any,
    ) -> bool:
        ingredient_name = item.ingredient_name
        previous_candidates = (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .filter(
                CatalogItem.tenant_id == tenant_id,
                CatalogItem.supplier_id == supplier.id,
                CatalogItem.ingredient_name == ingredient_name,
                CatalogItem.catalog_email_id != catalog_email.id,
            )
            .order_by(CatalogEmail.received_at.desc())
            .all()
        )
        previous = next(
            (
                candidate
                for candidate in previous_candidates
                if self._same_item_identity(candidate, item)
            ),
            None,
        )
        if previous is None:
            return True

        return any(
            [
                _nullable_float(previous.price_per_unit) != _nullable_float(item.price_per_unit),
                (previous.currency or "").upper() != (item.currency or "").upper(),
                (previous.lead_time_days or None) != (item.lead_time_days or None),
                (previous.raw_payload or {}).get("lead_time_text") != (item.lead_time_text or None),
                self._item_specification(previous) != self._item_specification(item),
            ]
        )

    def _single_existing_supplier_item(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        item,
        tenant_id: Any,
    ) -> CatalogItem | None:
        ingredient_name = item.ingredient_name
        previous_items = [
            candidate
            for candidate in (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .filter(
                CatalogItem.tenant_id == tenant_id,
                CatalogItem.supplier_id == supplier.id,
                CatalogItem.ingredient_name == ingredient_name,
                CatalogItem.catalog_email_id != catalog_email.id,
            )
            .order_by(CatalogEmail.received_at.desc())
            .all()
            )
            if self._same_item_identity(candidate, item)
        ]
        return previous_items[0] if len(previous_items) == 1 else None

    def _same_item_identity(self, existing: CatalogItem, item) -> bool:
        return self._item_identity_key(existing) == self._item_identity_key(item)

    def _item_identity_key(self, item) -> tuple:
        return (
            str(getattr(item, "ingredient_name", "") or "").strip().lower(),
            self._item_specification(item),
            _nullable_float(getattr(item, "available_qty", None)),
            str(getattr(item, "unit", None) or "").strip().lower(),
            _nullable_float(getattr(item, "moq", None)),
        )

    def _item_specification(self, item) -> str:
        raw_payload = getattr(item, "raw_payload", None) or {}
        value = (
            getattr(item, "specification", None)
            or raw_payload.get("specification")
            or self._notes_payload(getattr(item, "notes", None)).get("specification")
        )
        return (clean_optional_text(value) or "").strip().lower()

    def _touch_supplier_last_email(self, supplier: Supplier, received_at: datetime) -> None:
        if supplier.last_email_date is None or received_at > supplier.last_email_date:
            supplier.last_email_date = received_at

    def _pack_size_for_item(self, text: str, ingredient_name: str) -> str | None:
        ingredient = ingredient_name.lower()
        for line in text.splitlines():
            if ingredient in line.lower():
                return extract_pack_size(line)
        return None

    def _notes_payload(self, notes: str | None) -> dict[str, str]:
        payload: dict[str, str] = {}
        if not notes:
            return payload
        for part in notes.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key and value:
                cleaned_value = clean_optional_text(value.strip().strip("'\""))
                if cleaned_value:
                    payload[key.strip()] = cleaned_value
        return payload

    def _compact_payload(self, payload: dict) -> dict:
        cleaned: dict = {}
        for key, value in payload.items():
            if isinstance(value, str):
                cleaned_value = clean_optional_text(value)
                if cleaned_value is not None:
                    cleaned[key] = cleaned_value
            elif value is not None:
                cleaned[key] = value
        return cleaned

    def _exact_display_payload(self, item, text: str) -> dict[str, str]:
        payload: dict[str, str] = {}
        notes_payload = self._notes_payload(item.notes)

        if item.lead_time_text:
            payload["lead_time_text"] = str(item.lead_time_text)
        elif notes_payload.get("lead_time"):
            payload["lead_time_text"] = notes_payload["lead_time"]

        source_price = self._source_number_text(text, item.ingredient_name, item.price_per_unit)
        payload["price_display"] = self._richer_display_value(notes_payload.get("original_price"), source_price)

        source_quantity = self._source_number_text(text, item.ingredient_name, item.available_qty)
        if notes_payload.get("original_quantity"):
            original_quantity = notes_payload["original_quantity"]
            if item.unit and not re.search(r"[A-Za-z]", original_quantity):
                note_quantity = f"{original_quantity} {item.unit}"
            else:
                note_quantity = original_quantity
            payload["quantity_display"] = self._richer_display_value(note_quantity, source_quantity)
        else:
            payload["quantity_display"] = source_quantity

        if item.moq is not None:
            payload["moq_display"] = notes_payload.get("moq") or str(item.moq)
        return {key: value for key, value in payload.items() if value}

    def _richer_display_value(self, preferred: str | None, fallback: str | None) -> str | None:
        preferred = clean_optional_text(preferred)
        fallback = clean_optional_text(fallback)
        if not preferred:
            return fallback
        if not fallback:
            return preferred

        def richness(value: str) -> int:
            score = len(value)
            if re.search(r"(?:USD|INR|EUR|GBP|AED|CNY|JPY|CAD|AUD|SGD|CHF|Rs\.?|₹|\$|€|£)", value, flags=re.IGNORECASE):
                score += 30
            if "/" in value or re.search(r"\b(?:kg|g|mg|lb|bag|drum|mt|ton)\b", value, flags=re.IGNORECASE):
                score += 20
            if "(" in value and ")" in value:
                score += 10
            return score

        return fallback if richness(fallback) > richness(preferred) else preferred

    def _source_number_text(self, text: str, ingredient_name: str, value: Any) -> str | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric == 0:
            exact_value = "0"
        elif numeric.is_integer():
            exact_value = str(int(numeric))
        else:
            exact_value = f"{numeric:g}"

        if not exact_value:
            return None

        for line in text.splitlines():
            if ingredient_name.lower() not in line.lower():
                continue
            compact = line.replace(",", "")
            match = re.search(rf"(?<!\d){re.escape(exact_value)}(?:\.0+)?(?!\d)", compact)
            if match:
                value_text = match.group(0)
                display_match = re.search(
                    rf"(?:(?:USD|INR|EUR|GBP|AED|CNY|JPY|CAD|AUD|SGD|CHF|Rs\.?|₹|\$|€|£)\s*)?"
                    rf"{re.escape(value_text)}"
                    rf"(?:\s*(?:/[A-Za-z][A-Za-z0-9-]*|[A-Za-z][A-Za-z0-9-]*))?"
                    rf"(?:\s*\([A-Za-z0-9 .,/+-]+\))?",
                    compact,
                    flags=re.IGNORECASE,
                )
                if display_match:
                    return " ".join(display_match.group(0).split())
                return value_text
        return exact_value

    def _email_has_items(self, raw_email_id: str, tenant_id: Any | None = None) -> bool:
        query = (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogItem.catalog_email_id == CatalogEmail.id)
            .filter(CatalogEmail.raw_email_id.like(f"{raw_email_id}%"))
        )
        if tenant_id:
            query = query.filter(CatalogEmail.tenant_id == tenant_id)
        return query.first() is not None

    def _message_received_at(self, message: Message) -> datetime:
        try:
            date_hdr = message.get("Date")
            if date_hdr:
                parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=UTC)
                return parsed_dt
        except Exception:
            logger.warning("Failed parsing Date header from email, falling back to current time")
        return datetime.now(UTC)

    def _message_fingerprint(
        self,
        message: Message,
        sender: str,
        subject: str,
        received_at: datetime | None = None,
    ) -> str:
        message_id = (message.get("Message-ID") or message.get("Message-Id") or "").strip().strip("<>")
        if message_id:
            return f"message-id:{message_id.lower()}"

        normalized_subject = " ".join((subject or "").lower().split())
        received_marker = received_at.isoformat() if received_at else ""
        return f"fallback:{sender.strip().lower()}|{normalized_subject}|{received_marker}"

    def _csv_terms(self, raw: str | None) -> list[str]:
        return [term.strip().lower() for term in (raw or "").split(",") if term.strip()]

    def _text_matches_any(self, text: str, terms: list[str]) -> bool:
        text_lower = text.lower()
        return any(term in text_lower for term in terms)

    def _sender_matches_any(self, sender: str, display_name: str, terms: list[str]) -> bool:
        sender_lower = sender.lower()
        display_lower = (display_name or "").lower()
        domain = get_supplier_domain(sender)
        return any(
            term in sender_lower or term in display_lower or term == domain
            for term in terms
        )

    def _is_irrelevant_or_marketing_email(
        self,
        *,
        message: Message,
        sender: str,
        subject: str,
        body_text: str,
        labels: str,
        list_unsubscribe: str,
        precedence: str,
    ) -> bool:
        sender_lower = sender.lower()
        subject_lower = subject.lower()
        labels_lower = labels.lower()
        body_sample = body_text[:4000].lower()
        combined = f"{sender_lower} {subject_lower} {body_sample}"
        strong_supplier_terms = [term for term in SUPPLIER_INTENT_TERMS if term not in {"offer", "price", "pricing"}]

        marketing_headers = (
            "promotions" in labels_lower
            or "category-promo" in labels_lower
            or precedence.lower() in {"bulk", "list"}
            or bool(list_unsubscribe)
            or bool(message.get("List-Id"))
        )
        has_supplier_intent = self._text_matches_any(combined, strong_supplier_terms)
        has_irrelevant_terms = self._text_matches_any(combined, list(IRRELEVANT_MAIL_TERMS))

        if marketing_headers and not has_supplier_intent:
            return True
        if has_irrelevant_terms and not has_supplier_intent:
            return True
        if sender_lower.startswith(("no-reply@", "noreply@", "do-not-reply@", "donotreply@")):
            return True
        return False

    def _has_supplier_catalogue_intent(
        self,
        subject: str,
        body_text: str,
        attachments: list[dict],
    ) -> bool:
        attachment_names = " ".join(str(att.get("filename", "")) for att in attachments)
        text = f"{subject} {attachment_names} {body_text[:8000]}".lower()
        if self._text_matches_any(text, list(SUPPLIER_INTENT_TERMS)):
            return True

        # Structured attachments from a supplier mailbox are often terse, e.g. "July rates.xlsx".
        return any(str(att.get("ext", "")).lower() in {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc"} for att in attachments)

    def _mark_seen(self, client: imaplib.IMAP4, msg_uid: bytes) -> None:
        logger.debug("Leaving IMAP message uid=%s unread in the employee mailbox", msg_uid)

    def _restore_unseen_after_processing(self, client: imaplib.IMAP4, msg_uid: bytes) -> None:
        try:
            client.uid("store", msg_uid, "-FLAGS.SILENT", "\\Seen")
            logger.debug("Restored IMAP message uid=%s to unread after MediCORE processing", msg_uid)
        except Exception:
            logger.warning("Unable to restore IMAP message uid=%s to unread", msg_uid, exc_info=True)

    def _semantic_supplier_subject_match(self, subject: str, keywords: list[str]) -> bool:
        if self._has_supplier_catalogue_intent(subject, "", []):
            return True
        try:
            return self.llm.classify_supplier_subject(subject, keywords)
        except Exception:
            logger.exception("Semantic supplier subject classification failed; using local heuristic")
            return False

    def _upsert_supplier(self, sender: str, display_name: str | None = None, tenant_id: Any | None = None) -> Supplier:
        domain = get_supplier_domain(sender)
        if tenant_id:
            supplier = self.db.query(Supplier).filter(
                Supplier.email_domain == domain,
                Supplier.tenant_id == tenant_id
            ).first()
        else:
            supplier = self.db.query(Supplier).filter(Supplier.email_domain == domain).first()

        cleaned_display_name = display_name.strip() if display_name else None

        if supplier:
            if cleaned_display_name and supplier.name != cleaned_display_name:
                supplier.name = cleaned_display_name
                self.db.add(supplier)
            return supplier

        supplier_name = cleaned_display_name or sender

        supplier = Supplier(
            id=uuid4(),
            tenant_id=tenant_id or uuid4(),
            name=supplier_name,
            email_domain=domain,
        )
        self.db.add(supplier)
        self.db.flush()
        return supplier

    def _collect_attachments(self, message: Message) -> list[dict]:
        attachments = []
        for part in message.walk():
            filename = part.get_filename()
            if not filename:
                continue

            # Decode file name if encoded
            try:
                decoded = decode_header(filename)
                filename = "".join(
                    [
                        t[0].decode(t[1] or "utf-8", errors="ignore") if isinstance(t[0], bytes) else t[0]
                        for t in decoded
                    ]
                )
            except Exception:
                pass

            filename_lower = filename.lower()
            file_ext = Path(filename_lower).suffix
            supported_exts = (".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".txt", ".csv")
            if not file_ext or file_ext not in supported_exts:
                continue
            filename = filename.replace("\\", "/").split("/")[-1].strip()
            if not filename:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if len(payload) > MAX_DOCUMENT_BYTES:
                logger.warning("Skipping attachment %s because it exceeds the 30 MB processing limit", filename)
                continue

            mime_type = part.get_content_type()
            attachments.append({
                "filename": filename,
                "payload": payload,
                "ext": file_ext,
                "mime_type": mime_type
            })
        return attachments

    def _get_email_body_text(self, message: Message) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if "attachment" in content_disposition:
                    continue
                if content_type in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        decoded = payload.decode(charset, errors="ignore")
                        if content_type == "text/plain":
                            plain_parts.append(decoded)
                        else:
                            html_parts.append(self._html_to_text(decoded))
        else:
            payload = message.get_payload(decode=True)
            if payload:
                charset = message.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="ignore")
                if message.get_content_type() == "text/html":
                    html_parts.append(self._html_to_text(decoded))
                else:
                    plain_parts.append(decoded)
        return "\n".join(part.strip() for part in [*plain_parts, *html_parts] if part.strip()).strip()

    def _html_to_text(self, html: str) -> str:
        parser = _HTMLTextExtractor()
        try:
            parser.feed(html)
            return parser.text()
        except Exception:
            return ""

    def _extract_docx_text(self, file_path: Path) -> str:
        markitdown_text = self._extract_with_markitdown(file_path)
        if markitdown_text:
            return markitdown_text

        try:
            import mammoth
            with file_path.open("rb") as docx_file:
                result = mammoth.extract_raw_text(docx_file)
            if result.value.strip():
                return result.value
        except Exception:
            logger.info("Mammoth DOCX extraction failed for %s; falling back to XML", file_path.name)

        import zipfile
        import xml.etree.ElementTree as ET
        try:
            with zipfile.ZipFile(file_path) as docx:
                xml_content = docx.read('word/document.xml')
                root = ET.fromstring(xml_content)
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text_nodes = root.findall('.//w:t', ns)
                return "\n".join(node.text for node in text_nodes if node.text)
        except Exception as e:
            logger.exception("Error extracting text from docx file %s: %s", file_path.name, e)
            return ""

    def _extract_spreadsheet_text(self, file_path: Path, ext: str) -> str:
        if ext == ".csv":
            csv_text = self._extract_csv_tables_text(file_path)
            if csv_text:
                return csv_text

        if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
            excel_text = self._extract_xlsx_tables_text(file_path)
            if excel_text:
                return excel_text

        markitdown_text = self._extract_with_markitdown(file_path)
        if markitdown_text:
            return markitdown_text

        try:
            import pandas as pd
            frames = pd.read_excel(file_path, sheet_name=None)

            lines: list[str] = []
            for sheet_name, frame in frames.items():
                lines.append(f"Sheet: {sheet_name}")
                frame = frame.dropna(how="all").dropna(axis=1, how="all")
                if frame.empty:
                    continue
                lines.append(frame.to_csv(index=False))
            return "\n".join(lines).strip()
        except Exception as e:
            logger.exception("Error extracting tabular text from %s: %s", file_path.name, e)
            return ""

    def _extract_csv_tables_text(self, file_path: Path) -> str:
        encoding = self._detect_csv_encoding(file_path)
        delimiter = self._detect_csv_delimiter(file_path, encoding)
        header: list[str] | None = None
        data_rows: list[list[str]] = []
        header_line_number = 0

        try:
            with file_path.open("r", encoding=encoding, errors="replace", newline="") as csv_file:
                reader = csv.reader(csv_file, delimiter=delimiter, quotechar='"', doublequote=True)
                for row_number, raw_row in enumerate(reader, start=1):
                    try:
                        row = self._clean_csv_row(raw_row)
                        if self._is_empty_csv_row(row):
                            continue
                        if header is None:
                            if self._is_csv_header(row):
                                header = row
                                header_line_number = row_number
                            continue
                        if self._is_csv_header(row) and self._normalized_header(row) == self._normalized_header(header):
                            continue
                        recovered = self._recover_csv_row(row, len(header), file_path.name, row_number)
                        if recovered and not self._looks_like_csv_non_data_row(recovered):
                            data_rows.append(recovered)
                    except Exception as exc:
                        logger.warning("Failed parsing CSV row file=%s row=%s: %s", file_path.name, row_number, exc)
                        continue
        except csv.Error as exc:
            logger.warning("CSV reader failed for %s using delimiter %r: %s", file_path.name, delimiter, exc)
            return self._extract_csv_tables_text_fallback(file_path, encoding)
        except Exception:
            logger.exception("Could not read CSV file %s", file_path.name)
            return ""

        if not header or not data_rows:
            return self._extract_csv_tables_text_fallback(file_path, encoding)

        return self._format_csv_table(file_path.name, encoding, delimiter, header_line_number, header, data_rows)

    def _extract_csv_tables_text_fallback(self, file_path: Path, encoding: str) -> str:
        try:
            lines = file_path.read_text(encoding=encoding, errors="replace").splitlines()
        except Exception:
            return ""

        best_text = ""
        best_score = 0
        for delimiter in (",", ";", "|", "\t"):
            header: list[str] | None = None
            header_line_number = 0
            rows: list[list[str]] = []
            for row_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                row = self._clean_csv_row(line.split(delimiter))
                if self._is_empty_csv_row(row):
                    continue
                if header is None:
                    if self._is_csv_header(row):
                        header = row
                        header_line_number = row_number
                    continue
                recovered = self._recover_csv_row(row, len(header), file_path.name, row_number)
                if recovered and not self._looks_like_csv_non_data_row(recovered):
                    rows.append(recovered)
            score = len(rows) * len(header or [])
            if header and rows and score > best_score:
                best_score = score
                best_text = self._format_csv_table(file_path.name, encoding, delimiter, header_line_number, header, rows, fallback=True)
        return best_text

    def _format_csv_table(
        self,
        file_name: str,
        encoding: str,
        delimiter: str,
        header_line_number: int,
        header: list[str],
        rows: list[list[str]],
        fallback: bool = False,
    ) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        fallback_text = " Fallback: true" if fallback else ""
        output.write(
            f"[CSV TABLE] File: {file_name} Encoding: {encoding} "
            f"Delimiter: {repr(delimiter)} HeaderRow: {header_line_number}{fallback_text}\n"
        )
        writer.writerow(header)
        writer.writerows(rows)
        return output.getvalue().strip()

    def _detect_csv_encoding(self, file_path: Path) -> str:
        sample = file_path.read_bytes()[:65536]
        if sample.startswith(b"\xff\xfe") or sample.startswith(b"\xfe\xff"):
            return "utf-16"
        if sample.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1"):
            try:
                sample.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                continue
        return "utf-8"

    def _detect_csv_delimiter(self, file_path: Path, encoding: str) -> str:
        sample = file_path.read_text(encoding=encoding, errors="replace")[:65536]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
            if dialect.delimiter in {",", ";", "|", "\t"}:
                return dialect.delimiter
        except Exception:
            pass

        best_delimiter = ","
        best_score = -1
        lines = [line for line in sample.splitlines() if line.strip()][:100]
        for delimiter in (",", ";", "|", "\t"):
            counts = []
            header_hits = 0
            for line in lines:
                try:
                    row = next(csv.reader([line], delimiter=delimiter), [])
                except Exception:
                    row = line.split(delimiter)
                counts.append(len(row))
                if self._is_csv_header(self._clean_csv_row(row)):
                    header_hits += 1
            useful_counts = [count for count in counts if count >= 2]
            if not useful_counts:
                continue
            common_count = max(set(useful_counts), key=useful_counts.count)
            score = useful_counts.count(common_count) * 10 + header_hits * 25 + common_count
            if score > best_score:
                best_score = score
                best_delimiter = delimiter
        return best_delimiter

    def _clean_csv_row(self, row: list[Any]) -> list[str]:
        return [" ".join(str(cell).replace("\ufeff", "").split()).strip() for cell in row]

    def _is_empty_csv_row(self, row: list[str]) -> bool:
        return not any(clean_optional_text(cell) for cell in row)

    def _is_csv_header(self, row: list[str]) -> bool:
        cleaned = [cell for cell in row if clean_optional_text(cell)]
        if len(cleaned) < 2 or self._looks_like_csv_metadata(cleaned):
            return False
        header = _header_map(cleaned)
        return "name" in header and any(
            key in header
            for key in ("price", "qty", "unit", "specification", "currency", "moq", "lead_time", "pack")
        )

    def _normalized_header(self, row: list[str]) -> tuple[str, ...]:
        return tuple(re.sub(r"[^a-z0-9]+", " ", cell.lower()).strip() for cell in row)

    def _recover_csv_row(self, row: list[str], expected_columns: int, file_name: str, row_number: int) -> list[str] | None:
        if expected_columns <= 0:
            return None
        if len(row) == expected_columns:
            return row
        if len(row) < expected_columns:
            logger.warning("CSV %s row %s has %s columns; padding to %s", file_name, row_number, len(row), expected_columns)
            return row + [""] * (expected_columns - len(row))
        logger.warning(
            "CSV %s row %s has %s columns; trimming extras after expected %s columns",
            file_name,
            row_number,
            len(row),
            expected_columns,
        )
        return row[: expected_columns - 1] + [", ".join(cell for cell in row[expected_columns - 1:] if cell)]

    def _looks_like_csv_metadata(self, row: list[str]) -> bool:
        text = " ".join(cell for cell in row if cell).strip().lower()
        if not text:
            return True
        metadata_patterns = (
            r"\b(?:tel|phone|mobile|email|e-mail|address|www\.|http|generated|date|note|terms|contact)\b",
            r"^[\w.+-]+@[\w.-]+\.[a-z]{2,}$",
            r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in metadata_patterns)

    def _looks_like_csv_non_data_row(self, row: list[str]) -> bool:
        if self._is_empty_csv_row(row):
            return True
        first_cell = next((cell for cell in row if clean_optional_text(cell)), "")
        if not first_cell:
            return True
        return self._looks_like_csv_metadata([first_cell]) and sum(1 for cell in row if clean_optional_text(cell)) <= 2

    def _extract_xlsx_tables_text(self, file_path: Path) -> str:
        try:
            from openpyxl import load_workbook
        except ImportError:
            logger.warning("openpyxl is not installed; falling back for Excel extraction")
            return ""

        try:
            workbook = load_workbook(file_path, read_only=True, data_only=True)
        except Exception:
            logger.exception("Could not open Excel workbook %s", file_path.name)
            return ""

        sections: list[str] = []
        try:
            for worksheet in workbook.worksheets:
                try:
                    sections.extend(self._extract_worksheet_tables(worksheet))
                except Exception:
                    logger.exception("Failed extracting tables from worksheet %s", worksheet.title)
                    continue
        finally:
            workbook.close()

        return "\n\n".join(sections).strip()

    def _extract_worksheet_tables(self, worksheet: Any) -> list[str]:
        rows_by_index: dict[int, dict[int, str]] = {}
        populated_columns: set[int] = set()

        for row_index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
            row_values: dict[int, str] = {}
            for column_index, value in enumerate(row, start=1):
                text = self._spreadsheet_cell_text(value)
                if text:
                    row_values[column_index] = text
                    populated_columns.add(column_index)
            if row_values:
                rows_by_index[row_index] = row_values

        if not rows_by_index or not populated_columns:
            return []

        column_bands = self._contiguous_bands(sorted(populated_columns), max_gap=1)
        sections: list[str] = []
        table_number = 0

        min_row = min(rows_by_index)
        max_row = max(rows_by_index)
        for column_band in column_bands:
            row_index = min_row
            while row_index <= max_row:
                header_cells = rows_by_index.get(row_index, {})
                header_columns = [column for column in column_band if column in header_cells]
                header_values = [header_cells.get(column, "") for column in header_columns]

                if not self._is_spreadsheet_header(header_values):
                    row_index += 1
                    continue

                table_columns = list(range(min(header_columns), max(header_columns) + 1))
                data_rows: list[list[str]] = []
                empty_streak = 0
                cursor = row_index + 1

                while cursor <= max_row:
                    cells = rows_by_index.get(cursor, {})
                    row_values = [cells.get(column, "") for column in table_columns]
                    non_empty_count = sum(1 for value in row_values if value)

                    if data_rows and self._is_spreadsheet_header(
                        [cells.get(column, "") for column in column_band if column in cells]
                    ):
                        break

                    if non_empty_count:
                        data_rows.append(row_values)
                        empty_streak = 0
                    else:
                        empty_streak += 1
                        if empty_streak >= 2 and data_rows:
                            break
                    cursor += 1

                valid_data_rows = [row for row in data_rows if sum(1 for value in row if value) >= 1]
                if valid_data_rows:
                    table_number += 1
                    try:
                        sections.append(
                            self._format_spreadsheet_table(
                                worksheet.title,
                                table_number,
                                [header_cells.get(column, "") for column in table_columns],
                                valid_data_rows,
                                row_index,
                                table_columns[0],
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed formatting worksheet=%s table=%s",
                            worksheet.title,
                            table_number,
                        )

                row_index = max(cursor, row_index + 1)

        return sections

    def _spreadsheet_cell_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.date().isoformat()
        return " ".join(str(value).split()).strip()

    def _is_spreadsheet_header(self, values: list[str]) -> bool:
        cleaned = [value for value in values if value]
        if len(cleaned) < 2:
            return False
        header = _header_map(cleaned)
        return "name" in header and any(
            key in header
            for key in ("price", "qty", "unit", "specification", "currency", "moq", "lead_time", "pack")
        )

    def _contiguous_bands(self, values: list[int], max_gap: int) -> list[list[int]]:
        if not values:
            return []
        bands: list[list[int]] = [[values[0]]]
        for value in values[1:]:
            if value - bands[-1][-1] <= max_gap:
                bands[-1].append(value)
            else:
                bands.append([value])
        return bands

    def _format_spreadsheet_table(
        self,
        sheet_name: str,
        table_number: int,
        header: list[str],
        rows: list[list[str]],
        start_row: int,
        start_column: int,
    ) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        output.write(
            f"[EXCEL TABLE] Sheet: {sheet_name} Table: {table_number} "
            f"Start: R{start_row}C{start_column}\n"
        )
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
        return output.getvalue().strip()

    def _extract_with_markitdown(self, file_path: Path) -> str:
        try:
            from markitdown import MarkItDown

            result = MarkItDown().convert(str(file_path))
            return (getattr(result, "markdown", "") or "").strip()
        except Exception:
            logger.debug("MarkItDown extraction failed for %s", file_path.name, exc_info=True)
            return ""

    def _extract_image_text(self, file_path: Path) -> str:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        try:
            import pytesseract
        except ImportError:
            pytesseract = None

        if pytesseract is None:
            logger.warning("pytesseract is not installed; skipping image OCR for %s", file_path.name)
            return ""

        try:
            grid_table_text = ""
            try:
                from backend.app.services.image_grid_extractor import extract_grid_table_from_image
                grid_result = extract_grid_table_from_image(file_path)
                if grid_result:
                    grid_table_text = "[GRID CELL TABLE OCR]\n" + grid_result.table_text
            except Exception:
                logger.debug("Grid-cell OCR failed for %s; continuing with regular OCR", file_path.name, exc_info=True)

            image = Image.open(file_path)
            image = ImageOps.exif_transpose(image)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            variants = []
            base = image.convert("L")
            variants.append(("gray", base))
            scale = 2 if max(base.size) < 2400 else 1
            if scale > 1:
                variants.append(("gray_2x", base.resize((base.width * scale, base.height * scale))))

            enhanced = ImageOps.autocontrast(base)
            enhanced = ImageEnhance.Contrast(enhanced).enhance(1.8)
            enhanced = enhanced.filter(ImageFilter.SHARPEN)
            variants.append(("enhanced", enhanced))
            variants.append(("threshold", enhanced.point(lambda px: 255 if px > 170 else 0)))

            texts: list[str] = []
            for name, variant in variants:
                for config in ("--oem 3 --psm 6", "--oem 3 --psm 11"):
                    try:
                        page_text = pytesseract.image_to_string(variant, config=config)
                        if page_text.strip():
                            texts.append(f"[OCR {name} {config}]\n{page_text.strip()}")
                    except Exception:
                        logger.debug("OCR variant failed for %s using %s", name, config, exc_info=True)

            if grid_table_text:
                texts.insert(0, grid_table_text)
            text = "\n\n".join(dict.fromkeys(texts))
            logger.info("OCR extracted %s characters from image %s", len(text), file_path.name)
            return text
        except Exception as e:
            logger.exception("Error doing OCR on image %s: %s", file_path.name, e)
            return ""

    def _extract_text_from_file(self, file_path: Path, ext: str) -> str:
        if ext == ".pdf":
            from backend.app.services.pdf_extract import extract_pdf_text
            return extract_pdf_text(file_path)

        elif ext in (".xlsx", ".xls", ".xlsm", ".xltx", ".xltm"):
            return self._extract_spreadsheet_text(file_path, ext)

        elif ext == ".csv":
            return self._extract_spreadsheet_text(file_path, ext)

        elif ext == ".docx":
            return self._extract_docx_text(file_path)

        elif ext == ".doc":
            return self._extract_with_markitdown(file_path)

        elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"):
            return self._extract_image_text(file_path)

        elif ext == ".txt":
            try:
                return file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return ""
        return ""

    def _upload_file(self, file_path: Path, raw_email_id: str, mime_type: str) -> tuple[str, str]:
        object_path = f"{raw_email_id}/{file_path.name}"
        supabase = get_supabase()
        supabase.storage.from_(self.settings.supabase_storage_bucket).upload(
            object_path,
            file_path.read_bytes(),
            {"content-type": mime_type, "upsert": "true"},
        )
        return supabase.storage.from_(self.settings.supabase_storage_bucket).get_public_url(object_path), object_path

    def _delete_uploaded_files(self, object_paths: list[str]) -> None:
        if not object_paths:
            return
        try:
            get_supabase().storage.from_(self.settings.supabase_storage_bucket).remove(list(dict.fromkeys(object_paths)))
        except Exception:
            logger.warning("Failed to delete extracted email attachment objects", exc_info=True)

    def _imap_search_args_for_approach(self, approach: str, account: Any) -> tuple[str, ...]:
        """Return IMAP UID SEARCH args without relying on the user's read/unread state."""
        if approach == "approach_1":
            # The Suppliers label is the employee's explicit review boundary. A seen
            # message added to that label is still new to MediCORE until we log it.
            return ("ALL",)

        created_at = getattr(account, "created_at", None)
        if not created_at:
            return ("ALL",)

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return ("SINCE", created_at.strftime("%d-%b-%Y"))

    def preview_account_sync(self, account_id: UUID) -> dict:
        from backend.app.auth import decrypt_password
        from backend.app.models import CatalogEmail, EmailAccount, EmailSyncSetting

        account = self.db.query(EmailAccount).filter(EmailAccount.id == account_id).first()
        if not account:
            return {"account_id": str(account_id), "error": "Email account not found."}

        try:
            password = decrypt_password(account.encrypted_password)
        except Exception as e:
            return {
                "account_id": str(account.id),
                "email_address": account.email_address,
                "error": f"Failed to decrypt app password: {str(e)}",
            }

        sync_setting = self.db.query(EmailSyncSetting).filter(EmailSyncSetting.user_id == account.user_id).first()
        approach = sync_setting.ingestion_approach if sync_setting else "approach_1"
        mailbox = "INBOX"

        try:
            if account.imap_port == 993:
                client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=8)
            else:
                client = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=8)

            with client:
                client.login(account.email_address, password)

                if approach == "approach_1":
                    matched_mailbox = None
                    try:
                        status, mailboxes = client.list()
                        if status == "OK":
                            for mb in mailboxes:
                                mb_str = mb.decode("utf-8", errors="ignore")
                                match = re.search(r'"([^"]+)"\s*$', mb_str)
                                mb_name = match.group(1) if match else mb_str.split()[-1]
                                mb_name_lower = mb_name.strip().lower()
                                if (
                                    mb_name_lower in ("supplier", "suppliers")
                                    or mb_name_lower.endswith("/supplier")
                                    or mb_name_lower.endswith("/suppliers")
                                ):
                                    matched_mailbox = mb_name.strip()
                                    break
                    except Exception:
                        matched_mailbox = None
                    mailbox = matched_mailbox or "suppliers"

                status, _ = client.select(mailbox)
                if status != "OK":
                    return {
                        "account_id": str(account.id),
                        "email_address": account.email_address,
                        "approach": approach,
                        "mailbox": mailbox,
                        "error": f"Mailbox '{mailbox}' could not be selected.",
                    }

                search_args = self._imap_search_args_for_approach(approach, account)
                _, message_ids = client.uid("search", None, *search_args)
                ids = [msg_id.decode() for msg_id in (message_ids[0].split() if message_ids and message_ids[0] else [])]

            account_prefix = f"{account.id}:"
            logged_rows = self.db.query(CatalogEmail.raw_email_id).filter(
                CatalogEmail.raw_email_id.like(f"{account_prefix}%")
            ).all()
            logged_raw_ids = {row[0] for row in logged_rows}
            candidate_raw_ids = [f"{account.id}:{mailbox}:{msg_id}" for msg_id in ids]
            new_candidate_count = len([raw_id for raw_id in candidate_raw_ids if raw_id not in logged_raw_ids])

            return {
                "account_id": str(account.id),
                "email_address": account.email_address,
                "approach": approach,
                "mailbox": mailbox,
                "search": " ".join(search_args),
                "candidate_count": len(ids),
                "already_logged_count": len(candidate_raw_ids) - new_candidate_count,
                "new_candidate_count": new_candidate_count,
            }
        except Exception as e:
            logger.exception("Failed IMAP sync preview for account %s", account.email_address)
            return {
                "account_id": str(account.id),
                "email_address": account.email_address,
                "approach": approach,
                "mailbox": mailbox,
                "error": str(e),
            }

    def poll_account_inbox(self, account_id: UUID, force_retry_failed: bool = False) -> int:
        from backend.app.models import EmailAccount, EmailFilter
        from backend.app.auth import decrypt_password

        account = self.db.query(EmailAccount).filter(EmailAccount.id == account_id).first()
        if not account:
            logger.error("EmailAccount %s not found for polling", account_id)
            return 0

        # Resolve active tenant_id from profiles
        from backend.app.models import Profile
        profile = self.db.query(Profile).filter(Profile.id == account.user_id).first()
        active_tenant_id = profile.tenant_id if (profile and profile.tenant_id) else account.user_id

        if force_retry_failed:
            from backend.app.models import CatalogEmail
            try:
                account_prefix = f"{account.id}:"
                self.db.query(CatalogEmail).filter(
                    CatalogEmail.raw_email_id.like(f"{account_prefix}%")
                ).filter(
                    (CatalogEmail.processing_status.like("failed%")) |
                    (CatalogEmail.processing_status.like("error%")) |
                    (CatalogEmail.processing_status.is_(None))
                ).delete(synchronize_session=False)
                self.db.commit()
                logger.info("Cleared failed/error catalog email logs to force retry for account %s", account.email_address)
            except Exception as e:
                self.db.rollback()
                logger.error("Failed to clean up failed catalog logs for retry: %s", e)

        # Decrypt password securely
        try:
            password = decrypt_password(account.encrypted_password)
        except Exception as e:
            logger.error("Failed to decrypt password for email account %s: %s", account_id, e)
            account.sync_status = "error"
            account.sync_error_msg = f"Failed to decrypt app password: {str(e)}"
            self.db.commit()
            return 0

        # Run IMAP connection
        processed = 0
        try:
            logger.info("Connecting to IMAP for %s at %s:%s", account.email_address, account.imap_host, account.imap_port)
            if account.imap_port == 993:
                client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=30)
            else:
                client = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=30)

            with client:
                client.login(account.email_address, password)

                # Fetch filters and global sync settings
                active_filter = self.db.query(EmailFilter).filter(EmailFilter.email_account_id == account.id).first()
                from backend.app.models import EmailSyncSetting
                sync_setting = self.db.query(EmailSyncSetting).filter(EmailSyncSetting.user_id == account.user_id).first()
                approach = sync_setting.ingestion_approach if sync_setting else "approach_1"
                pending_email_ids: set[str] = set()
                ignored_email_ids: set[str] = set()
                ignored_email_fingerprints: set[str] = set()
                ignored_email_keys: set[str] = set()
                if sync_setting:
                    try:
                        import json
                        approval_items = json.loads(sync_setting.pending_approvals or "[]")
                        pending_email_ids = {
                            str(item.get("email_id"))
                            for item in approval_items
                            if isinstance(item, dict) and item.get("email_id") and not item.get("ignored")
                        }
                        ignored_email_ids = {
                            str(item.get("email_id"))
                            for item in approval_items
                            if isinstance(item, dict) and item.get("email_id") and item.get("ignored")
                        }
                        ignored_email_fingerprints = {
                            str(item.get("fingerprint"))
                            for item in approval_items
                            if isinstance(item, dict) and item.get("fingerprint") and item.get("ignored")
                        }
                        ignored_email_keys = {
                            "|".join(
                                [
                                    str(item.get("sender") or "").strip().lower(),
                                    str(item.get("subject") or "").strip().lower(),
                                    str(item.get("date") or "").strip(),
                                ]
                            )
                            for item in approval_items
                            if isinstance(item, dict) and item.get("ignored")
                        }
                    except Exception:
                        pending_email_ids = set()
                        ignored_email_ids = set()
                        ignored_email_fingerprints = set()
                        ignored_email_keys = set()

                mailbox = "INBOX"
                if approach == "approach_1":
                    matched_mailbox = None
                    try:
                        status, mailboxes = client.list()
                        if status == "OK":
                            for mb in mailboxes:
                                mb_str = mb.decode("utf-8", errors="ignore")
                                import re
                                match = re.search(r'"([^"]+)"\s*$', mb_str)
                                if not match:
                                    mb_name = mb_str.split()[-1]
                                else:
                                    mb_name = match.group(1)

                                mb_name_lower = mb_name.strip().lower()
                                if mb_name_lower in ("supplier", "suppliers") or mb_name_lower.endswith("/supplier") or mb_name_lower.endswith("/suppliers"):
                                    matched_mailbox = mb_name.strip()
                                    break
                    except Exception as e:
                        logger.warning("Error listing mailboxes: %s", e)

                    if matched_mailbox:
                        mailbox = matched_mailbox
                        logger.info("Found matching supplier mailbox: %s", mailbox)
                    else:
                        mailbox = "suppliers"

                try:
                    status, _ = client.select(mailbox)
                    if status != "OK":
                        raise imaplib.IMAP4.error(f"Select failed for {mailbox}")
                except imaplib.IMAP4.error:
                    if approach == "approach_1":
                        fallbacks = ["suppliers", "supplier"]
                        selected = False
                        for fb in fallbacks:
                            if fb == mailbox:
                                continue
                            try:
                                status_fb, _ = client.select(fb)
                                if status_fb == "OK":
                                    logger.warning("Mailbox %s selection failed. Fell back to %s", mailbox, fb)
                                    mailbox = fb
                                    selected = True
                                    break
                            except imaplib.IMAP4.error:
                                pass
                        if not selected:
                            raise RuntimeError(
                                "Supplier label mailbox not found. Create or enable the Gmail IMAP label named 'suppliers'."
                            )
                    elif mailbox != "INBOX":
                        fallbacks = ["INBOX"]
                        selected = False
                        for fb in fallbacks:
                            try:
                                status_fb, _ = client.select(fb)
                                if status_fb == "OK":
                                    logger.warning("Mailbox %s selection failed. Fell back to %s", mailbox, fb)
                                    mailbox = fb
                                    selected = True
                                    break
                            except imaplib.IMAP4.error:
                                pass
                        if not selected:
                            raise RuntimeError("Failed to select INBOX")
                    else:
                        raise

                # Search by UID so stored message IDs remain stable even when mailbox sequence numbers change.
                search_args = self._imap_search_args_for_approach(approach, account)
                _, message_ids = client.uid("search", None, *search_args)
                ids = message_ids[0].split() if message_ids and message_ids[0] else []
                # Process newest first
                ids.reverse()
                logger.info(
                    "Account %s has %s candidate messages in %s (criteria: %s)",
                    account.email_address,
                    len(ids),
                    mailbox,
                    " ".join(search_args),
                )

                # Fetch already processed email IDs cache to optimize DB lookup
                processed_email_ids = set()
                from backend.app.models import CatalogEmail
                res = self.db.query(CatalogEmail.raw_email_id).filter(CatalogEmail.tenant_id == active_tenant_id).all()
                for r in res:
                    raw_stored_id = r[0]
                    account_prefix = f"{account.id}:"
                    if raw_stored_id.startswith(account_prefix):
                        parts = raw_stored_id.split(":")
                        base_id = ":".join(parts[:3]) if len(parts) >= 3 else raw_stored_id
                    else:
                        base_id = raw_stored_id.split(":")[0] if ":" in raw_stored_id else raw_stored_id
                    processed_email_ids.add(base_id)

                for msg_id in ids:
                    msg_id_str = msg_id.decode()
                    raw_id_str = f"{account.id}:{mailbox}:{msg_id_str}"
                    if raw_id_str in processed_email_ids:
                        continue
                    if raw_id_str in pending_email_ids:
                        continue

                    try:
                        logger.info("Fetching message id=%s for account %s", raw_id_str, account.email_address)
                        _, data = client.uid("fetch", msg_id, "(BODY.PEEK[])")
                        if not data or not isinstance(data[0], tuple):
                            continue
                        if len(data[0][1]) > MAX_DOCUMENT_BYTES:
                            logger.warning("Skipping email id=%s because raw RFC822 payload exceeds 30 MB", raw_id_str)
                            self._create_skipped_email_record(raw_id_str, "unknown@supplier.com", "Unknown", "Oversized email", "ignored: email exceeds 30 MB", active_tenant_id)
                            continue

                        message = email.message_from_bytes(data[0][1])

                        # Apply keyword / attachment filters
                        email_date = self._message_received_at(message)
                        display_name, sender = self._extract_sender(message)
                        subject = message.get("Subject") or ""
                        email_fingerprint = self._message_fingerprint(message, sender, subject, email_date)

                        labels = message.get("X-Gmail-Labels", "")
                        list_unsubscribe = message.get("List-Unsubscribe", "")
                        precedence = message.get("Precedence", "")

                        # Collect all attachments and email body text
                        attachments = self._collect_attachments(message)
                        body_text = self._get_email_body_text(message)

                        if self._is_irrelevant_or_marketing_email(
                            message=message,
                            sender=sender,
                            subject=subject,
                            body_text=body_text,
                            labels=labels,
                            list_unsubscribe=list_unsubscribe,
                            precedence=precedence,
                        ):
                            logger.info("Skipping non-supplier/marketing email id=%s from=%s subject=%r", raw_id_str, sender, subject)
                            self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: marketing or irrelevant", active_tenant_id, email_date)
                            self._mark_seen(client, msg_id)
                            continue

                        # Build parse targets
                        parse_targets = []
                        for att in attachments:
                            parse_targets.append({
                                "name": att["filename"],
                                "payload": att["payload"],
                                "ext": att["ext"],
                                "mime_type": att["mime_type"],
                                "is_body": False
                            })

                        if body_text.strip():
                            parse_targets.append({
                                "name": "email_body.txt",
                                "payload": body_text.encode("utf-8"),
                                "ext": ".txt",
                                "mime_type": "text/plain",
                                "is_body": True
                            })

                        # Filter: Require attachment
                        if active_filter and active_filter.require_attachment and not attachments:
                            logger.info("Skipping email id=%s because attachment is required but none found", raw_id_str)
                            self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: attachment required", active_tenant_id, email_date)
                            self._mark_seen(client, msg_id)
                            continue

                        if active_filter:
                            sender_terms = self._csv_terms(active_filter.sender_keywords)
                            if sender_terms and not self._sender_matches_any(sender, display_name, sender_terms):
                                logger.info("Skipping email id=%s because sender filter did not match", raw_id_str)
                                self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: sender filter", active_tenant_id, email_date)
                                self._mark_seen(client, msg_id)
                                continue

                            subject_terms = self._csv_terms(active_filter.subject_keywords)
                            if subject_terms and not self._text_matches_any(subject, subject_terms):
                                logger.info("Skipping email id=%s because subject filter did not match", raw_id_str)
                                self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: subject filter", active_tenant_id, email_date)
                                self._mark_seen(client, msg_id)
                                continue

                        approach2_keywords: list[str] = []
                        approach2_semantic_subject_match = False
                        if approach == "approach_2" and sync_setting:
                            approach2_keywords = self._csv_terms(sync_setting.keyword_filters)
                            approach2_semantic_subject_match = self._semantic_supplier_subject_match(subject, approach2_keywords)

                        if not self._has_supplier_catalogue_intent(subject, body_text, attachments) and not approach2_semantic_subject_match:
                            logger.info("Skipping email id=%s because no supplier catalogue intent was detected", raw_id_str)
                            self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: no supplier catalogue intent", active_tenant_id, email_date)
                            self._mark_seen(client, msg_id)
                            continue

                        # Check Ingestion Approach 2
                        if approach == "approach_2" and sync_setting:
                            domain = get_supplier_domain(sender)
                            trusted_list = self._csv_terms(sync_setting.trusted_suppliers)
                            email_approval_key = "|".join(
                                [
                                    sender.strip().lower(),
                                    subject.strip().lower(),
                                    email_date.isoformat(),
                                ]
                            )
                            if (
                                raw_id_str in ignored_email_ids
                                or email_fingerprint in ignored_email_fingerprints
                                or email_approval_key in ignored_email_keys
                            ):
                                logger.info("Skipping email id=%s because user denied processing previously", raw_id_str)
                                self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: denied by user", active_tenant_id, email_date)
                                self._mark_seen(client, msg_id)
                                continue
                            if not approach2_semantic_subject_match:
                                logger.info("Skipping email id=%s because approach-2 semantic subject check did not match", raw_id_str)
                                self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: semantic subject mismatch", active_tenant_id, email_date)
                                self._mark_seen(client, msg_id)
                                continue

                            is_trusted = (sender.lower() in trusted_list) or (domain in trusted_list)
                            supplier_exists = (
                                self.db.query(Supplier.id)
                                .join(CatalogEmail, CatalogEmail.supplier_id == Supplier.id)
                                .join(CatalogItem, CatalogItem.catalog_email_id == CatalogEmail.id)
                                .filter(
                                    Supplier.tenant_id == active_tenant_id,
                                    Supplier.email_domain == domain,
                                    CatalogEmail.processing_status == "completed",
                                )
                                .first()
                                is not None
                            )
                            if not is_trusted and not supplier_exists:
                                if parse_targets:
                                    # New supplier alert! Add to pending_approvals and DO NOT mark read
                                    import json
                                    try:
                                        pending_list = json.loads(sync_setting.pending_approvals or "[]")
                                    except Exception:
                                        pending_list = []

                                    if not any(
                                        isinstance(item, dict)
                                        and (item.get("email_id") == raw_id_str or item.get("fingerprint") == email_fingerprint)
                                        for item in pending_list
                                    ):
                                        pending_list.append({
                                            "email_id": raw_id_str,
                                            "fingerprint": email_fingerprint,
                                            "sender": sender,
                                            "supplier_name": display_name or sender,
                                            "subject": subject,
                                            "date": email_date.isoformat(),
                                            "reason": "Subject keyword matched; supplier approval required",
                                        })
                                        sync_setting.pending_approvals = json.dumps(pending_list)
                                        pending_email_ids.add(raw_id_str)
                                        self.db.commit()
                                        logger.info("Added email id=%s to pending_approvals for %s", raw_id_str, sender)
                                    continue
                                else:
                                    # Doesn't match keywords or has no supported content, skip and mark as seen
                                    logger.info("Skipping non-supplier email id=%s from=%s subject=%r", raw_id_str, sender, subject)
                                    self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: no parseable supplier content", active_tenant_id, email_date)
                                    self._mark_seen(client, msg_id)
                                    continue

                        # Process message if we have parse targets and matched everything
                        if parse_targets:
                            try:
                                processed += self._process_message(message, raw_email_id=raw_id_str, parse_targets=parse_targets, tenant_id=active_tenant_id)
                                self._restore_unseen_after_processing(client, msg_id)
                            except Exception as pe:
                                logger.exception("Failed processing email payload for raw_email_id=%s", raw_id_str)
                                self._create_failed_email_record(raw_id_str, sender, display_name, subject, f"Failed: {str(pe)}", tenant_id=active_tenant_id, email_date=email_date)

                        else:
                            logger.info("Skipping email id=%s because it had no parseable payload", raw_id_str)
                            self._create_skipped_email_record(raw_id_str, sender, display_name, subject, "ignored: no parseable payload", active_tenant_id, email_date)
                            self._mark_seen(client, msg_id)

                    except Exception as inner_e:
                        logger.exception("Error processing email msg_id=%s", msg_id)
                        try:
                            self._create_failed_email_record(raw_id_str, "unknown@supplier.com", "Unknown", "Extraction Failure", f"Failed: {str(inner_e)}", tenant_id=active_tenant_id)
                        except Exception:
                            pass

                # Update status
                account.sync_status = "ok"
                account.sync_error_msg = None
                account.last_synced_at = datetime.now(UTC)
                self.db.commit()
                logger.info("Successfully finished polling for %s; processed %s", account.email_address, processed)

        except Exception as e:
            logger.exception("Error polling account %s", account.email_address)
            account.sync_status = "error"
            account.sync_error_msg = f"IMAP connection failed: {str(e)}"
            self.db.commit()

        return processed

    def _create_failed_email_record(self, raw_email_id: str, sender: str, display_name: str, subject: str, error_msg: str, tenant_id: Any, email_date: datetime | None = None) -> None:
        try:
            self.db.rollback()
            from backend.app.models import CatalogEmail
            from uuid import uuid4

            existing = (
                self.db.query(CatalogEmail)
                .filter(CatalogEmail.raw_email_id == raw_email_id)
                .filter(CatalogEmail.tenant_id == tenant_id)
                .first()
            )
            if existing:
                existing.processing_status = f"failed: {error_msg}"[:50]
                existing.pdf_url = None
                if email_date:
                    existing.received_at = email_date
                self.db.commit()
                return

            supplier = self._upsert_supplier(sender, display_name=display_name, tenant_id=tenant_id)

            catalog_email = CatalogEmail(
                id=uuid4(),
                tenant_id=tenant_id or supplier.tenant_id,
                supplier_id=supplier.id,
                raw_email_id=raw_email_id,
                subject=subject,
                pdf_url=None,
                received_at=email_date or datetime.now(UTC),
                processing_status=f"failed: {error_msg}"[:50],
            )
            self.db.add(catalog_email)
            self.db.commit()
            logger.info("Saved sync fallback/failed email record for id=%s: %s", raw_email_id, error_msg)
        except Exception as e:
            self.db.rollback()
            logger.error("Failed to write fallback/failed email record to DB: %s", e)

    def _create_skipped_email_record(
        self,
        raw_email_id: str,
        sender: str,
        display_name: str,
        subject: str,
        reason: str,
        tenant_id: Any,
        email_date: datetime | None = None,
    ) -> None:
        try:
            existing = (
                self.db.query(CatalogEmail)
                .filter(CatalogEmail.raw_email_id == raw_email_id)
                .filter(CatalogEmail.tenant_id == tenant_id)
                .first()
            )
            if existing:
                return

            supplier = self._upsert_supplier(sender, display_name=display_name, tenant_id=tenant_id)
            self.db.add(
                CatalogEmail(
                    id=uuid4(),
                    tenant_id=tenant_id or supplier.tenant_id,
                    supplier_id=supplier.id,
                    raw_email_id=raw_email_id,
                    subject=subject,
                    pdf_url=None,
                    received_at=email_date or datetime.now(UTC),
                    processing_status=reason[:50],
                )
            )
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.error("Failed to write skipped email tombstone to DB: %s", e)

