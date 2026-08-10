import unittest
from unittest.mock import patch

from PIL import Image

from backend.app.services.catalog_table_parser import parse_catalog_table_text
from backend.app.services.ocr import OCRTextLine
from backend.app.services.image_grid_extractor import (
    column_name_from_header,
    extract_grid_table_from_pil_image,
    extract_price_parts,
    normalize_lead_time_text,
    rows_to_catalog_table_text,
)


class ImageGridExtractorTest(unittest.TestCase):
    def test_grid_rows_preserve_quantity_moq_and_inline_specification(self) -> None:
        table_text = rows_to_catalog_table_text(
            [
                {
                    "cells": {
                        "date": "26/5/14",
                        "customer": "CSN Pharma Inc.",
                        "product": "Zinc Gluconate 12% Zinc",
                        "quantity_kg": "4.66 MOQ:25kg",
                        "price": "CIF Vancouver $6.00/kg",
                        "lead_time": "40-50days",
                    }
                },
                {
                    "cells": {
                        "product": "Beta Carotene 1% Synthesis",
                        "quantity_kg": "66.57",
                        "price": "CIF Ve 5509 $1",
                        "lead_time": "40-50days",
                    }
                },
            ]
        )

        rows = parse_catalog_table_text("[RAPIDOCR TABLE OCR]\n" + table_text)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].ingredient_name, "Zinc Gluconate")
        self.assertEqual(rows[0].specification, "12% Zinc")
        self.assertEqual(rows[0].available_qty, 4.66)
        self.assertEqual(rows[0].unit, "kg")
        self.assertEqual(rows[0].moq, 25.0)
        self.assertEqual(rows[0].price_per_unit, 6.0)
        self.assertEqual(rows[1].ingredient_name, "Beta Carotene")
        self.assertEqual(rows[1].specification, "1% Synthesis")
        self.assertEqual(rows[1].available_qty, 66.57)
        self.assertIsNone(rows[1].price_per_unit)

    def test_quantity_kg_header_preserves_unit_when_header_ocr_is_available(self) -> None:
        self.assertEqual(column_name_from_header("Quantity(KG)", "quantity"), "quantity_kg")

    def test_ocr_garbage_price_text_is_not_preserved_as_price_display(self) -> None:
        table_text = rows_to_catalog_table_text(
            [
                {
                    "cells": {
                        "product": "Sea Moss Powder",
                        "quantity_kg": "446.02",
                        "price": "ost OOKy",
                        "lead_time": "40-50days",
                    }
                },
                {
                    "cells": {
                        "product": "Stevia Extract Reb A",
                        "specification": "98%",
                        "quantity_kg": "46.6",
                        "price": "oar come",
                        "lead_time": "40-50days",
                    }
                },
            ]
        )

        rows = parse_catalog_table_text("[RAPIDOCR TABLE OCR]\n" + table_text)

        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0].price_per_unit)
        self.assertNotIn("original_price", rows[0].notes or "")
        self.assertIsNone(rows[1].price_per_unit)
        self.assertNotIn("original_price", rows[1].notes or "")

    def test_ocr_lead_time_fragments_are_normalized_or_dropped(self) -> None:
        self.assertEqual(normalize_lead_time_text("40-50d"), "40-50 days")
        self.assertEqual(normalize_lead_time_text("40-50days"), "40-50days")
        self.assertEqual(normalize_lead_time_text("d"), "")

    def test_price_extraction_preserves_valid_text_prices_only(self) -> None:
        self.assertEqual(extract_price_parts("ost OOKy"), ("", ""))
        self.assertEqual(extract_price_parts("oar come"), ("", ""))
        self.assertEqual(extract_price_parts("On request"), ("On request", ""))

    def test_unbordered_product_spec_blocks_ignore_banner_and_footer_text(self) -> None:
        def line(text: str, left: float, top: float, right: float, bottom: float) -> OCRTextLine:
            return OCRTextLine(text=text, score=0.98, box=(left, top, right, bottom))

        ocr_lines = [
            line("Used For Sports Nutrition", 180, 30, 520, 62),
            line("Used For Dietary Supplements", 920, 30, 1290, 62),
            line("Product Name", 90, 115, 350, 140),
            line("Specification", 390, 115, 630, 140),
            line("Product Name", 780, 115, 1020, 140),
            line("Specification", 1050, 115, 1420, 140),
            line("Beta Alanine", 90, 155, 240, 178),
            line("All Grade", 400, 155, 500, 178),
            line("BCAA Instantized", 90, 190, 280, 214),
            line("Vegan; 2:1:1; 4:1:1; 8:1:1", 400, 190, 630, 214),
            line("Quercetin", 780, 155, 900, 178),
            line("95% HPLC", 1050, 155, 1150, 178),
            line("Rutin", 780, 190, 840, 214),
            line("NF II Grade", 1050, 190, 1160, 214),
            line("OEM Services(Hard Capsules,Tablets & Soft Gels Form)", 80, 690, 650, 720),
            line("OEM Services(Hard Capsules,Tablets & Soft Gels Form)", 820, 690, 1380, 720),
        ]

        with (
            patch("backend.app.services.image_grid_extractor.recognize_image", return_value=ocr_lines),
            patch("backend.app.services.image_grid_extractor.detect_table_grid", return_value=([], [])),
        ):
            result = extract_grid_table_from_pil_image(Image.new("RGB", (1450, 750), "white"), "sample.png")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.rows), 4)
        self.assertIn("Beta Alanine | All Grade", result.table_text)
        self.assertIn("BCAA Instantized | Vegan; 2:1:1; 4:1:1; 8:1:1", result.table_text)
        self.assertIn("Quercetin | 95% HPLC", result.table_text)
        self.assertIn("Rutin | NF II Grade", result.table_text)
        self.assertNotIn("Used For Sports Nutrition", result.table_text)
        self.assertNotIn("OEM Services", result.table_text)


if __name__ == "__main__":
    unittest.main()
