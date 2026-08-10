import logging
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger("medicore.sync_status")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Global console instance
console = Console(force_terminal=True if sys.stdout and sys.stdout.isatty() else None, legacy_windows=False)


class SyncStatusNotifier:
    """Beautiful terminal status reporter and visual debug tracker for email sync and GMFT table extraction."""

    def __init__(self) -> None:
        self.console = console

    def _safe_print(self, *args: Any, **kwargs: Any) -> None:
        try:
            self.console.print(*args, **kwargs)
        except Exception:
            try:
                # Strip formatting tags for plain fallback logging
                msg = " ".join(str(a) for a in args)
                logger.info("[TERMINAL STATUS] %s", msg)
            except Exception:
                pass

    def start_sync_banner(self, mode: str = "IMAP Poll", target: str = "All Accounts") -> None:
        grid = Table.grid(expand=True)
        grid.add_column(justify="center")
        grid.add_row(Text(" MediCORE Email Ingestion & Extraction Engine ", style="bold cyan"))
        grid.add_row(Text(f"Mode: {mode} | Target: {target} | Engine: GMFT (Table Transformer) + PDF Inspector", style="dim white"))
        self._safe_print()
        self._safe_print(Panel(grid, border_style="cyan", title="[bold yellow]EMAIL SYNC STARTED[/bold yellow]"))

    def notify_fetching_emails(self, account_email: str) -> None:
        self._safe_print(f"[bold blue][EMAIL SYNC][/bold blue] Connected to [bold white]{account_email}[/bold white] — checking for new messages...")

    def notify_pdf_found(self, filename: str, file_size_kb: float) -> None:
        self._safe_print(
            f"  [bold yellow][ATTACHMENT FOUND][/bold yellow] File: [bold white]{filename}[/bold white] ({file_size_kb:.1f} KB)"
        )

    def notify_gmft_start(self, filename: str) -> None:
        self._safe_print(
            f"    [bold magenta][GMFT TABLE TRANSFORMER][/bold magenta] Scanning [bold white]{filename}[/bold white] for tables (Microsoft TATR)..."
        )

    def notify_gmft_success(self, filename: str, table_count: int, page_details: list[str]) -> None:
        details_str = ", ".join(page_details) if page_details else f"{table_count} table(s)"
        self._safe_print(
            f"    [bold green]  [GMFT SUCCESS][/bold green] Extracted [bold green]{table_count} table(s)[/bold green] from [bold white]{filename}[/bold white] ({details_str})"
        )

    def notify_gmft_info(self, filename: str, reason: str = "No tables detected") -> None:
        self._safe_print(
            f"    [dim cyan]  [GMFT INFO][/dim cyan] GMFT completed for [bold white]{filename}[/bold white] ({reason})"
        )

    def notify_pdf_inspector(self, filename: str, pdf_type: str, confidence: float, pages_needing_ocr: list[int]) -> None:
        ocr_info = f"OCR required for page(s): {pages_needing_ocr}" if pages_needing_ocr else "No page OCR required"
        self._safe_print(
            f"    [bold cyan][PDF INSPECTOR][/bold cyan] Type: [bold yellow]{pdf_type}[/bold yellow] | Confidence: [bold green]{confidence * 100:.0f}%[/bold green] | {ocr_info}"
        )

    def notify_ocr_start(self, filename: str, pages: list[int] | None) -> None:
        target = f"page(s) {pages}" if pages else "all pages"
        self._safe_print(
            f"    [bold orange3][RAPIDOCR][/bold orange3] Running image OCR on [bold white]{filename}[/bold white] ({target})..."
        )

    def notify_classification(self, filename: str, category: str, confidence: float) -> None:
        color = "green" if category == "catalogue" else "yellow"
        self._safe_print(
            f"  [bold {color}][CLASSIFICATION][/bold {color}] Document [bold white]{filename}[/bold white] classified as [bold {color}]{category.upper()}[/bold {color}] (conf: {confidence:.2f})"
        )

    def notify_parsing_result(self, source_name: str, item_count: int, parser_used: str) -> None:
        style = "bold green" if item_count > 0 else "dim yellow"
        self._safe_print(
            f"  [{style}][CATALOG PARSER][/{style}] Extracted [bold white]{item_count} catalog item(s)[/bold white] from [bold white]{source_name}[/bold white] via [bold cyan]{parser_used}[/bold cyan]"
        )

    def notify_item_preview(self, items: list[Any], max_preview: int = 3) -> None:
        if not items:
            return
        table = Table(title="Catalog Items Preview", show_header=True, header_style="bold magenta", border_style="dim white")
        table.add_column("Ingredient / Product", style="cyan")
        table.add_column("Price", style="green", justify="right")
        table.add_column("Currency", style="yellow")
        table.add_column("Unit", style="magenta")
        table.add_column("MOQ", style="blue", justify="right")

        for item in items[:max_preview]:
            name = getattr(item, "ingredient_name", "") or str(item.get("ingredient_name", ""))
            price = getattr(item, "price_per_unit", None) if hasattr(item, "price_per_unit") else item.get("price_per_unit")
            curr = getattr(item, "currency", "") if hasattr(item, "currency") else item.get("currency", "")
            unit = getattr(item, "unit", "") if hasattr(item, "unit") else item.get("unit", "")
            moq = getattr(item, "moq", None) if hasattr(item, "moq") else item.get("moq")

            table.add_row(
                name[:40],
                f"{price:.2f}" if isinstance(price, (int, float)) else "-",
                str(curr or "-"),
                str(unit or "-"),
                f"{moq}" if moq is not None else "-",
            )

        if len(items) > max_preview:
            table.add_row(f"... and {len(items) - max_preview} more item(s)", "", "", "", "")

        self._safe_print(table)

    def sync_complete_summary(self, emails_checked: int, pdfs_processed: int, total_items_extracted: int, gmft_tables_found: int) -> None:
        summary_table = Table(title="[bold green]SYNC & EXTRACTION SUMMARY[/bold green]", border_style="green", expand=False)
        summary_table.add_column("Metric", style="bold white")
        summary_table.add_column("Value", style="bold cyan", justify="right")

        summary_table.add_row("Emails Checked", str(emails_checked))
        summary_table.add_row("PDF Attachments Ingested", str(pdfs_processed))
        summary_table.add_row("GMFT Tables Extracted", f"[bold green]{gmft_tables_found}[/bold green]")
        summary_table.add_row("Total Catalog Items Saved", f"[bold yellow]{total_items_extracted}[/bold yellow]")

        self._safe_print()
        self._safe_print(summary_table)
        self._safe_print()


sync_notifier = SyncStatusNotifier()
