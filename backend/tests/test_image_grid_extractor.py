import unittest

from backend.app.services.catalog_table_parser import parse_catalog_table_text
from backend.app.services.image_grid_extractor import (
    column_name_from_header,
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

        rows = parse_catalog_table_text("[TESSERACT TABLE OCR]\n" + table_text)

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

        rows = parse_catalog_table_text("[TESSERACT TABLE OCR]\n" + table_text)

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


if __name__ == "__main__":
    unittest.main()
