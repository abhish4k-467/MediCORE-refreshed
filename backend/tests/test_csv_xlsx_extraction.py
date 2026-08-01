import unittest
import tempfile
from pathlib import Path

from backend.app.services.catalog_table_parser import (
    parse_catalog_table_text,
    _split_table_line,
    _header_map,
)
from backend.app.services.email_ingestion import EmailIngestionService
from backend.app.schemas import ExtractedCatalogItem


class TestCsvXlsxExtraction(unittest.TestCase):

    def test_csv_line_splitting_with_quotes_and_commas(self):
        csv_line = '"Amoxicillin 500mg, USP","Purity 99%, USP grade","1,000","25.50"'
        parts = _split_table_line(csv_line)
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "Amoxicillin 500mg, USP")
        self.assertEqual(parts[1], "Purity 99%, USP grade")
        self.assertEqual(parts[2], "1,000")
        self.assertEqual(parts[3], "25.50")

    def test_csv_extraction_pipeline_multiple_rows(self):
        csv_text = """Product Name,Specification,Quantity (KG),Price (USD)
"Amoxicillin 500mg, USP","Purity >= 98%, Mesh 200","1,000","25.50"
"Paracetamol 500mg, BP","BP Grade, White Powder","5,000","4.20"
"Ibuprofen 400mg, IP","Pharma Grade","2,500","18.00"
"Aspirin 100mg, USP","Fine Powder","10,000","3.10"
"Ciprofloxacin 500mg, USP","USP Grade","750","45.00"
"""
        items = parse_catalog_table_text(csv_text)
        self.assertEqual(len(items), 5)

        # Check Row 1
        self.assertEqual(items[0].ingredient_name, "Amoxicillin 500mg, USP")
        self.assertEqual(items[0].available_qty, 1000.0)
        self.assertEqual(items[0].unit, "kg")
        self.assertEqual(items[0].price_per_unit, 25.50)

        # Check Row 4
        self.assertEqual(items[3].ingredient_name, "Aspirin 100mg, USP")
        self.assertEqual(items[3].available_qty, 10000.0)
        self.assertEqual(items[3].unit, "kg")
        self.assertEqual(items[3].price_per_unit, 3.10)

    def test_quantity_column_variations(self):
        csv_text = """Item Name,Stock Qty,Rate/Unit
Paracetamol,500 kgs,4.50
Ibuprofen,1200 kgs,12.00
"""
        items = parse_catalog_table_text(csv_text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].available_qty, 500.0)
        self.assertEqual(items[0].unit, "kg")
        self.assertEqual(items[1].available_qty, 1200.0)
        self.assertEqual(items[1].unit, "kg")

    def test_description_header_fallback(self):
        csv_text = """Description,Qty,Price
"Cetirizine HCl 10mg",500,8.50
"Loratadine 10mg",1000,14.00
"""
        items = parse_catalog_table_text(csv_text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].ingredient_name, "Cetirizine HCl 10mg")
        self.assertEqual(items[0].available_qty, 500.0)
        self.assertEqual(items[0].price_per_unit, 8.50)

    def test_two_column_csv_table(self):
        csv_text = """Product,Quantity
Aspirin 100mg,5000 kg
Paracetamol 500mg,10000 kg
"""
        items = parse_catalog_table_text(csv_text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].ingredient_name, "Aspirin 100mg")
        self.assertEqual(items[0].available_qty, 5000.0)
        self.assertEqual(items[0].unit, "kg")

    def test_csv_extractor_detects_encoding_delimiter_header_and_preserves_duplicates(self):
        content = (
            "Great River Biosciences Inventory July\n"
            "sales@greatriverbio.com\n"
            "Tel: XXXXXXX\n"
            "\n"
            "Product Name;Stock;Unit;Notes\n"
            "\"Vitamin D3 Powder, 100,000 IU/g\";\"1,000.50\";\"μg\";\"Supplier said \"\"In Stock\"\"\"\n"
            "\"Vitamin Blend\nPremium Grade\";0.25;kg;25%\n"
            "\"Vitamin D3 Powder, 100,000 IU/g\";1000;kg;duplicate offer\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "great-river.csv"
            csv_path.write_bytes(content.encode("utf-16"))

            service = object.__new__(EmailIngestionService)
            text = service._extract_csv_tables_text(csv_path)

        self.assertIn("[CSV TABLE]", text)
        self.assertIn("Delimiter: ';'", text)
        self.assertIn("Vitamin Blend Premium Grade", text)
        self.assertIn('Supplier said ""In Stock""', text)

        items = parse_catalog_table_text(text, dedupe=False)
        names = [item.ingredient_name for item in items]
        self.assertEqual(names.count("Vitamin D3 Powder, 100,000 IU/g"), 2)
        self.assertIn("Vitamin Blend Premium Grade", names)
        self.assertEqual(items[0].available_qty, 1000.50)

    def test_csv_extractor_recovers_uneven_rows_and_pipe_delimiter(self):
        content = (
            "Generated date: 2026-08-01\n"
            "Product|Stock|Unit|Price\n"
            "Aspirin 100mg|5000|kg|3.10|extra commercial note\n"
            "Paracetamol 500mg|10000|kg\n"
            "||||\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "pipe.csv"
            csv_path.write_text(content, encoding="windows-1252")

            service = object.__new__(EmailIngestionService)
            text = service._extract_csv_tables_text(csv_path)

        self.assertIn("Delimiter: '|'", text)
        items = parse_catalog_table_text(text, dedupe=False)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].ingredient_name, "Aspirin 100mg")
        self.assertEqual(items[1].ingredient_name, "Paracetamol 500mg")
        self.assertIsNone(items[1].price_per_unit)

    def test_xlsx_extraction_detects_side_by_side_and_stacked_tables(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Inventory"
        sheet["A1"] = "ABC Pharma Inventory July"

        tables = [
            ("A3", [("Product", "Stock", "Unit"), ("Aspirin 100mg", 5000, "kg")]),
            ("E3", [("Product", "Stock", "Unit"), ("Paracetamol 500mg", 10000, "kg")]),
            ("A8", [("Product", "Price", "Unit"), ("Vitamin D3 Powder (Lichen) 100,000 IU/g", 25.5, "kg")]),
            ("E8", [("Product", "Stock", "Unit"), ("Ibuprofen 400mg", 2500, "kg")]),
        ]
        for start_cell, rows in tables:
            start = sheet[start_cell]
            for row_offset, row in enumerate(rows):
                for col_offset, value in enumerate(row):
                    sheet.cell(start.row + row_offset, start.column + col_offset, value=value)

        with tempfile.TemporaryDirectory() as tmp_dir:
            workbook_path = Path(tmp_dir) / "multi-table.xlsx"
            workbook.save(workbook_path)

            service = object.__new__(EmailIngestionService)
            text = service._extract_xlsx_tables_text(workbook_path)

        self.assertEqual(text.count("[EXCEL TABLE]"), 4)
        items = parse_catalog_table_text(text)
        names = [item.ingredient_name for item in items]
        self.assertIn("Aspirin 100mg", names)
        self.assertIn("Paracetamol 500mg", names)
        self.assertIn("Ibuprofen 400mg", names)
        self.assertIn("Vitamin D3 Powder (Lichen) 100,000 IU/g", names)


if __name__ == "__main__":
    unittest.main()
