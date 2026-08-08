import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image


class TesseractOCRConfigTest(unittest.TestCase):
    def test_tesseract_ocr_uses_table_friendly_config_and_boxes(self) -> None:
        from backend.app.services import ocr

        captured: dict[str, object] = {}

        class FakePytesseractModule:
            tesseract_cmd = ""

        def fake_image_to_osd(*args, **kwargs):
            return "Rotate: 0\n"

        def fake_image_to_data(*args, **kwargs):
            captured.update(kwargs)
            return {
                "text": ["Vitamin", "C", "USD", "5/kg"],
                "conf": ["92", "91", "90", "89"],
                "left": [10, 82, 180, 232],
                "top": [20, 20, 20, 20],
                "width": [65, 18, 42, 50],
                "height": [15, 15, 15, 15],
                "block_num": [1, 1, 1, 1],
                "par_num": [1, 1, 1, 1],
                "line_num": [1, 1, 1, 1],
            }

        fake_module = types.SimpleNamespace(
            Output=types.SimpleNamespace(DICT="dict"),
            pytesseract=FakePytesseractModule(),
            image_to_osd=fake_image_to_osd,
            image_to_data=fake_image_to_data,
        )

        with patch.dict(sys.modules, {"pytesseract": fake_module}):
            lines = ocr.recognize_image(Image.new("RGB", (320, 120), "white"), "sample.png")

        self.assertEqual([line.text for line in lines], ["Vitamin", "C", "USD", "5/kg"])
        self.assertEqual(captured["lang"], "eng")
        self.assertIn("--oem 1", captured["config"])
        self.assertIn("--psm 6", captured["config"])
        self.assertIn("preserve_interword_spaces=1", captured["config"])
        self.assertIn("textord_tablefind_recognize_tables=1", captured["config"])


if __name__ == "__main__":
    unittest.main()
