import email
import email.utils
from email.header import decode_header
from html.parser import HTMLParser
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

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db import get_supabase
from backend.app.models import CatalogEmail, CatalogItem, Supplier
from backend.app.services.catalog_table_parser import (
    CATALOG_TABLE_PARSER_VERSION,
    extract_pack_size,
    parse_catalog_table_text,
)
from backend.app.services.embeddings import embed_catalog_item_text
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
    local_part, domain = sender.split("@", 1)
    domain = domain.lower()
    generic_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
        "mail.com", "protonmail.com", "proton.me", "icloud.com", "zoho.com",
        "gmx.com", "yandex.com", "live.com"
    }
    if domain in generic_domains:
        return sender.lower()
    return domain


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

        for target in parse_targets:
            target_name = str(target["name"]).replace("\\", "/").split("/")[-1].strip()
            if not target_name:
                target_name = "email_payload.txt" if target.get("is_body") else f"attachment-{uuid4()}"
            payload = target["payload"]
            ext = target["ext"]
            mime_type = target["mime_type"]
            if len(payload) > MAX_DOCUMENT_BYTES:
                logger.warning("Skipping %s because it exceeds the 30 MB processing limit", target_name)
                continue

            logger.info("Processing target %s (%s bytes)", target_name, len(payload))
            with tempfile.TemporaryDirectory() as tmp_dir:
                file_path = Path(tmp_dir) / target_name
                file_path.write_bytes(payload)
                uploaded_url = self._upload_file(file_path, raw_email_id, mime_type)
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
        if count > 0:
            catalog_email.processing_status = "completed"
            self._touch_supplier_last_email(supplier, catalog_email.received_at)
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

        # If it is a conversational email body or unstructured text file, run LLM directly
        if source_name.lower().endswith(".txt") or "email_body" in source_name.lower():
            try:
                extracted = [normalize_item(item) for item in self.llm.extract_catalog_items(text, reference_date=reference_date)]
                logger.info("LLM extracted %s catalogue row(s) from conversational source %s", len(extracted), source_name)
                return extracted
            except Exception:
                logger.exception("LLM extraction failed for %s", source_name)
                return []

        parser_text = self._preferred_parser_text(text)

        # Otherwise, try the OCR regex table parser first for structured catalogs
        parsed = [
            normalize_item(item)
            for item in parse_catalog_table_text(parser_text, reference_date=reference_date)
        ]
        parsed = self._dedupe_extracted_items(parsed)
        logger.info("OCR table parser extracted %s catalogue row(s) from %s", len(parsed), source_name)

        if len(parsed) >= 20:
            logger.info(
                "Using %s deterministic parser row(s) for structured catalogue %s; skipping LLM fallback",
                len(parsed),
                source_name,
            )
            return parsed

        if not getattr(self, "llm", None):
            return parsed

        try:
            llm_items = [normalize_item(item) for item in self.llm.extract_catalog_items(text, reference_date=reference_date)]
            extracted = self._dedupe_extracted_items([*parsed, *llm_items])
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
                (item.normalized_name or item.ingredient_name).strip().lower(),
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
        for item in items:
            item = self._with_source_note(item, text)
            if not self._has_required_grounded_values(item):
                logger.warning(
                    "Skipping extracted item with missing required grounded values: %s",
                    item.model_dump(mode="json"),
                )
                continue

            if not self._catalog_item_changed(catalog_email, supplier, item, active_tenant_id):
                logger.info(
                    "Skipping unchanged catalogue item supplier=%s item=%s",
                    supplier.email_domain,
                    item.normalized_name or item.ingredient_name,
                )
                continue

            item_text = (
                f"{item.normalized_name} {item.ingredient_name} "
                f"{item.available_qty} {item.unit} {item.price_per_unit} {item.currency}"
            )
            raw_payload = self._compact_payload(item.model_dump(mode="json"))
            raw_payload["source"] = "email_extracted_catalogue"
            if clean_optional_text(source_name):
                raw_payload["source_document"] = clean_optional_text(source_name)
            pack_size = clean_optional_text(self._pack_size_for_item(text, item.ingredient_name))
            if pack_size:
                raw_payload["pack_size"] = pack_size
            raw_payload.update(self._compact_payload(self._notes_payload(item.notes)))
            raw_payload.update(self._compact_payload(self._exact_display_payload(item, text)))
            existing_item = self._single_existing_supplier_item(catalog_email, supplier, item, active_tenant_id)
            if existing_item:
                logger.info(
                    "Updating existing catalogue item supplier=%s item=%s from email id=%s",
                    supplier.email_domain,
                    item.normalized_name or item.ingredient_name,
                    catalog_email.raw_email_id,
                )
                raw_payload["is_updated"] = True
                existing_item.catalog_email_id = catalog_email.id
                existing_item.ingredient_name = item.ingredient_name
                existing_item.normalized_name = item.normalized_name or item.ingredient_name.lower()
                existing_item.price_per_unit = item.price_per_unit
                existing_item.currency = item.currency
                existing_item.available_qty = item.available_qty
                existing_item.unit = item.unit
                existing_item.valid_until = item.valid_until
                existing_item.lead_time_days = item.lead_time_days
                existing_item.moq = item.moq
                existing_item.embedding = self._safe_embedding(item_text)
                existing_item.raw_payload = raw_payload
            else:
                self.db.add(
                    CatalogItem(
                        id=uuid4(),
                        tenant_id=active_tenant_id,
                        catalog_email_id=catalog_email.id,
                        supplier_id=supplier.id,
                        ingredient_name=item.ingredient_name,
                        normalized_name=item.normalized_name or item.ingredient_name.lower(),
                        price_per_unit=item.price_per_unit,
                        currency=item.currency,
                        available_qty=item.available_qty,
                        unit=item.unit,
                        valid_until=item.valid_until,
                        lead_time_days=item.lead_time_days,
                        moq=item.moq,
                        embedding=self._safe_embedding(item_text),
                        raw_payload=raw_payload,
                    )
                )
            count += 1
        return count

    def _with_source_note(self, item, text: str):
        notes = item.notes or ""
        if "source=" in notes.lower() or "source:" in notes.lower():
            return item

        ingredient = (item.ingredient_name or item.normalized_name or "").lower()
        if item.price_per_unit is None:
            for line in text.splitlines():
                normalized_line = " ".join(line.split())
                if ingredient and ingredient in normalized_line.lower():
                    safe_line = normalized_line[:500].replace("'", "")
                    joined_notes = f"{notes}; source='{safe_line}'" if notes else f"source='{safe_line}'"
                    return item.model_copy(update={"notes": joined_notes})
            return item
        for line in text.splitlines():
            normalized_line = " ".join(line.split())
            line_lower = normalized_line.lower()
            if ingredient and ingredient in line_lower and self._price_appears_in_line(item.price_per_unit, normalized_line):
                safe_line = normalized_line[:500].replace("'", "")
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
            str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip("."),
            f"{number:.2f}",
            f"{number:.4f}".rstrip("0").rstrip("."),
        }
        return any(variant in compact_line for variant in variants)

    def _has_required_grounded_values(self, item) -> bool:
        if not (item.ingredient_name or "").strip():
            return False
        if item.price_per_unit is not None and float(item.price_per_unit) <= 0:
            return False
        if item.available_qty is not None and float(item.available_qty) < 0:
            return False
        if item.available_qty is not None and not (item.unit or "").strip():
            return False
        notes = (item.notes or "").lower()
        grounded_markers = ("source=", "source:", "original_price=", "original_quantity=", "lead_time=")
        return any(marker in notes for marker in grounded_markers)

    def _catalog_item_changed(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        item,
        tenant_id: Any,
    ) -> bool:
        normalized_name = item.normalized_name or item.ingredient_name.lower()
        previous = (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .filter(
                CatalogItem.tenant_id == tenant_id,
                CatalogItem.supplier_id == supplier.id,
                CatalogItem.normalized_name == normalized_name,
                CatalogItem.catalog_email_id != catalog_email.id,
            )
            .order_by(CatalogEmail.received_at.desc())
            .first()
        )
        if previous is None:
            return True

        return any(
            [
                _nullable_float(previous.price_per_unit) != _nullable_float(item.price_per_unit),
                (previous.currency or "").upper() != (item.currency or "").upper(),
                _nullable_float(previous.available_qty) != _nullable_float(item.available_qty),
                (previous.unit or "").lower() != (item.unit or "").lower(),
                (previous.lead_time_days or None) != (item.lead_time_days or None),
                (previous.raw_payload or {}).get("lead_time_text") != (item.lead_time_text or None),
                _nullable_float(previous.moq) != _nullable_float(item.moq),
            ]
        )

    def _single_existing_supplier_item(
        self,
        catalog_email: CatalogEmail,
        supplier: Supplier,
        item,
        tenant_id: Any,
    ) -> CatalogItem | None:
        normalized_name = item.normalized_name or item.ingredient_name.lower()
        previous_items = (
            self.db.query(CatalogItem)
            .join(CatalogEmail, CatalogEmail.id == CatalogItem.catalog_email_id)
            .filter(
                CatalogItem.tenant_id == tenant_id,
                CatalogItem.supplier_id == supplier.id,
                CatalogItem.normalized_name == normalized_name,
                CatalogItem.catalog_email_id != catalog_email.id,
            )
            .order_by(CatalogEmail.received_at.desc())
            .limit(2)
            .all()
        )
        return previous_items[0] if len(previous_items) == 1 else None

    def _touch_supplier_last_email(self, supplier: Supplier, received_at: datetime) -> None:
        if supplier.last_email_date is None or received_at > supplier.last_email_date:
            supplier.last_email_date = received_at

    def _safe_embedding(self, item_text: str) -> list[float] | None:
        try:
            return embed_catalog_item_text(item_text)
        except Exception:
            logger.exception("Embedding failed; storing catalogue item without vector")
            return None

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
        exact_value = str(numeric).rstrip("0").rstrip(".")
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

        if cleaned_display_name:
            supplier_name = cleaned_display_name
        else:
            # Check if domain is a generic domain
            generic_domains = {
                "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
                "mail.com", "protonmail.com", "proton.me", "icloud.com", "zoho.com",
                "gmx.com", "yandex.com", "live.com"
            }
            email_domain_part = sender.split("@")[1].lower() if "@" in sender else domain

            if email_domain_part in generic_domains:
                # Use the full email address, don't format the name from the email ID
                supplier_name = sender
            else:
                # Custom domain: format the domain name prefix
                domain_prefix = email_domain_part.split(".")[0]
                supplier_name = domain_prefix.replace("-", " ").replace(".", " ").title()

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
            supported_exts = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".txt", ".csv")
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
        try:
            import pandas as pd
            if ext == ".csv":
                frames = {"csv": pd.read_csv(file_path)}
            else:
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
            if ext == ".csv":
                try:
                    return file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    return ""
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

        elif ext in (".xlsx", ".xls"):
            return self._extract_spreadsheet_text(file_path, ext)

        elif ext == ".csv":
            return self._extract_spreadsheet_text(file_path, ext)

        elif ext == ".docx":
            return self._extract_docx_text(file_path)

        elif ext == ".doc":
            try:
                from markitdown import MarkItDown
                md = MarkItDown()
                result = md.convert(str(file_path))
                return result.markdown
            except Exception as e:
                logger.exception("Error extracting text using markitdown from %s: %s", file_path.name, e)
                return ""

        elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"):
            return self._extract_image_text(file_path)

        elif ext == ".txt":
            try:
                return file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return ""
        return ""

    def _upload_file(self, file_path: Path, raw_email_id: str, mime_type: str) -> str:
        object_path = f"{raw_email_id}/{file_path.name}"
        supabase = get_supabase()
        supabase.storage.from_(self.settings.supabase_storage_bucket).upload(
            object_path,
            file_path.read_bytes(),
            {"content-type": mime_type, "upsert": "true"},
        )
        return supabase.storage.from_(self.settings.supabase_storage_bucket).get_public_url(object_path)

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
                    except Exception:
                        pending_email_ids = set()
                        ignored_email_ids = set()
                        ignored_email_fingerprints = set()

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
                            if raw_id_str in ignored_email_ids or email_fingerprint in ignored_email_fingerprints:
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
            from backend.app.models import CatalogEmail
            from uuid import uuid4

            existing = (
                self.db.query(CatalogEmail)
                .filter(CatalogEmail.raw_email_id == raw_email_id)
                .filter(CatalogEmail.tenant_id == tenant_id)
                .first()
            )
            if existing:
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

