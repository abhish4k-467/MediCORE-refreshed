import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.services import pdf_extract


class PdfInspectorExtractionTest(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("pdf_inspector", None)

    def test_text_pdf_uses_pdf_inspector_markdown_without_ocr(self) -> None:
        sys.modules["pdf_inspector"] = types.SimpleNamespace(
            process_pdf=lambda path: SimpleNamespace(
                markdown="Ingredient | Price\n--- | ---\nVitamin C | USD 5/kg",
                pdf_type="text_based",
                confidence=0.98,
                page_count=1,
                pages_needing_ocr=[],
                has_encoding_issues=False,
            )
        )

        with patch.object(pdf_extract, "_extract_with_ocr", side_effect=AssertionError("OCR should not run")):
            text = pdf_extract.extract_pdf_text(Path("catalogue.pdf"))

        self.assertIn("[PDF INSPECTOR MARKDOWN]", text)
        self.assertIn("Vitamin C | USD 5/kg", text)

    def test_mixed_pdf_ocrs_only_pages_needing_ocr(self) -> None:
        sys.modules["pdf_inspector"] = types.SimpleNamespace(
            process_pdf=lambda path: SimpleNamespace(
                markdown="Native page text",
                pdf_type="mixed",
                confidence=0.84,
                page_count=3,
                pages_needing_ocr=[2],
                has_encoding_issues=False,
            )
        )
        captured = {}

        def fake_ocr(path, pages=None):
            captured["pages"] = pages
            return "[RAPIDOCR OCR]\nScanned page text"

        with patch.object(pdf_extract, "_extract_with_ocr", fake_ocr):
            text = pdf_extract.extract_pdf_text(Path("mixed.pdf"))

        self.assertEqual(captured["pages"], [2])
        self.assertIn("Native page text", text)
        self.assertIn("Scanned page text", text)

    def test_scanned_pdf_routes_all_pages_to_ocr(self) -> None:
        sys.modules["pdf_inspector"] = types.SimpleNamespace(
            process_pdf=lambda path: SimpleNamespace(
                markdown=None,
                pdf_type="scanned",
                confidence=0.97,
                page_count=2,
                pages_needing_ocr=[1, 2],
                has_encoding_issues=False,
            )
        )
        captured = {}

        def fake_ocr(path, pages=None):
            captured["pages"] = pages
            return "[RAPIDOCR OCR]\nScanned document text"

        with patch.object(pdf_extract, "_extract_with_ocr", fake_ocr):
            text = pdf_extract.extract_pdf_text(Path("scan.pdf"))

        self.assertIsNone(captured["pages"])
        self.assertIn("Scanned document text", text)

    def test_gmft_extraction_included_in_output(self) -> None:
        with patch.object(pdf_extract, "_extract_with_gmft", return_value="[GMFT TABLE MARKDOWN Page 1 Table 1]\n| Ingredient | Price |\n| --- | --- |\n| Paracetamol | 10 USD |"):
            with patch.object(pdf_extract, "_extract_with_pdf_inspector", return_value=None):
                with patch.object(pdf_extract, "_extract_with_ocr", return_value=""):
                    text = pdf_extract.extract_pdf_text(Path("catalogue.pdf"))

        self.assertIn("[GMFT TABLE MARKDOWN Page 1 Table 1]", text)
        self.assertIn("Paracetamol | 10 USD", text)


if __name__ == "__main__":
    unittest.main()

