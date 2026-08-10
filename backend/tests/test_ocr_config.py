import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image


class RapidOCRConfigTest(unittest.TestCase):
    def test_rapidocr_lines_are_used_for_image_ocr(self) -> None:
        from backend.app.services import ocr

        class FakeRapidOCR:
            def __call__(self, image):
                return (
                    [
                        (
                            [(10, 20), (75, 20), (75, 35), (10, 35)],
                            "Vitamin",
                            0.92,
                        ),
                        (
                            [(82, 20), (100, 20), (100, 35), (82, 35)],
                            "C",
                            0.91,
                        ),
                        (
                            [(180, 20), (222, 20), (222, 35), (180, 35)],
                            "USD",
                            0.90,
                        ),
                        (
                            [(232, 20), (282, 20), (282, 35), (232, 35)],
                            "5/kg",
                            0.89,
                        ),
                    ],
                    None,
                )

        fake_module = types.SimpleNamespace(RapidOCR=lambda: FakeRapidOCR())

        with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_module}):
            if hasattr(ocr.recognize_image, "_rapidocr_engine"):
                delattr(ocr.recognize_image, "_rapidocr_engine")
            lines = ocr.recognize_image(Image.new("RGB", (320, 120), "white"), "sample.png")

        self.assertEqual([line.text for line in lines], ["Vitamin", "C", "USD", "5/kg"])


if __name__ == "__main__":
    unittest.main()
