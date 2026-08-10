import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.db import SessionLocal
from backend.app.services.email_ingestion import EmailIngestionService
from backend.app.services.pdf_extract import extract_pdf_text
from backend.app.services.terminal_sync_status import sync_notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="MediCORE Email Sync & PDF Extraction CLI with Rich Visual Status")
    parser.add_argument("--poll", action="store_true", help="Poll configured email inbox accounts")
    parser.add_argument(
        "--retry-skipped",
        action="store_true",
        help="Retry previously skipped certificate/catalogue candidates during this manual poll",
    )
    parser.add_argument("--reprocess", action="store_true", help="Reprocess stored attachments in database")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of stored attachments to reprocess")
    parser.add_argument("--file", type=str, help="Path to a local PDF catalog file to extract with GMFT and debug")

    args = parser.parse_args()

    sync_notifier.start_sync_banner(
        mode="Local CLI Runner",
        target=args.file if args.file else ("Reprocess Attachments" if args.reprocess else "Poll Inboxes"),
    )

    if args.file:
        pdf_path = Path(args.file)
        if not pdf_path.is_file():
            print(f"Error: File not found at {pdf_path}")
            sys.exit(1)

        sync_notifier.notify_pdf_found(pdf_path.name, pdf_path.stat().st_size / 1024.0)
        extracted_text = extract_pdf_text(pdf_path)

        with SessionLocal() as db:
            service = EmailIngestionService(db)
            items = service._extract_items_from_text(extracted_text, pdf_path.name)
            sync_notifier.notify_parsing_result(pdf_path.name, len(items), "GMFT + Deterministic Parser")
            sync_notifier.notify_item_preview(items, max_preview=10)

        sys.exit(0)

    with SessionLocal() as db:
        service = EmailIngestionService(db)
        if args.reprocess:
            processed = service.reprocess_stored_attachments(limit=args.limit, force=True)
            sync_notifier.sync_complete_summary(emails_checked=0, pdfs_processed=args.limit, total_items_extracted=processed, gmft_tables_found=processed)
        else:
            from backend.app.tasks import poll_inbox
            # --poll is a user-invoked command, so it must not be subject to
            # the background scheduler's poll interval.
            res = poll_inbox(force=True, retry_skipped=args.retry_skipped)
            sync_notifier.sync_complete_summary(
                emails_checked=res.get("checked", 0),
                pdfs_processed=res.get("processed", 0),
                total_items_extracted=res.get("processed", 0),
                gmft_tables_found=res.get("processed", 0),
            )


if __name__ == "__main__":
    main()
