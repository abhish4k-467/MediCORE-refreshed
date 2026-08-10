import unittest

from backend.app.services.catalog_table_parser import parse_catalog_table_text, _extract_moq
from backend.app.services.image_grid_extractor import (
    column_name_from_header,
    extract_quantity_parts,
    rows_to_catalog_table_text,
)


class MOQPackingEquivalenceTest(unittest.TestCase):
    def test_column_header_mapping_detects_packing_as_moq(self) -> None:
        self.assertEqual(column_name_from_header("Packing", "fallback"), "moq")
        self.assertEqual(column_name_from_header("Packaging", "fallback"), "moq")
        self.assertEqual(column_name_from_header("Pack Size", "fallback"), "moq")
        self.assertEqual(column_name_from_header("MOQ / Packing", "fallback"), "moq")
        self.assertEqual(column_name_from_header("MOQ", "fallback"), "moq")

    def test_extract_quantity_parts_extracts_moq_from_packing(self) -> None:
        # MOQ and packing are the same thing
        qty, unit, moq, pack = extract_quantity_parts("100 kg Packing: 25kg drum")
        self.assertEqual(qty, "100")
        self.assertEqual(unit, "kg")
        self.assertEqual(moq, "25kg")
        self.assertEqual(pack, "25 kg drum")

        qty2, unit2, moq2, pack2 = extract_quantity_parts("50kg 25 kg packing")
        self.assertEqual(qty2, "50")
        self.assertEqual(unit2, "kg")
        self.assertEqual(moq2, "25kg")
        self.assertEqual(pack2, "25 kg packing")

        qty3, unit3, moq3, pack3 = extract_quantity_parts("MOQ: 50 kg")
        self.assertEqual(moq3, "50kg")
        self.assertEqual(pack3, "50 kg")

    def test_extract_moq_from_packing_text(self) -> None:
        val, unit = _extract_moq("25 kg packing")
        self.assertEqual(val, 25.0)
        self.assertEqual(unit, "kg")

        val2, unit2 = _extract_moq("Packing: 50 kg drum")
        self.assertEqual(val2, 50.0)
        self.assertEqual(unit2, "kg")

    def test_rows_to_catalog_table_text_populates_moq_from_packing_column(self) -> None:
        table_text = rows_to_catalog_table_text(
            [
                {
                    "cells": {
                        "product": "Ascorbic Acid (Vitamin C)",
                        "quantity_kg": "500",
                        "price": "$12.50/kg",
                        "pack": "25 kg drum",
                        "lead_time": "10 days",
                    }
                }
            ]
        )

        rows = parse_catalog_table_text("[RAPIDOCR TABLE OCR]\n" + table_text)

        self.assertEqual(len(rows), 1)
        self.assertIn("Ascorbic Acid", rows[0].ingredient_name)
        self.assertEqual(rows[0].available_qty, 500.0)
        self.assertEqual(rows[0].moq, 25.0)
        self.assertEqual(rows[0].price_per_unit, 12.5)


if __name__ == "__main__":
    unittest.main()
