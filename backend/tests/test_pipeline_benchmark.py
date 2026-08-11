"""Benchmark harness for measuring Character Error Rate (CER) and Word Error Rate (WER) across document types.

Owned by: backend/tests/test_pipeline_benchmark.py
"""

import unittest
from pathlib import Path
import fitz

from backend.app.pipeline.pipeline import process_document


def levenshtein_distance(ref: list | str, hyp: list | str) -> int:
    """Compute Levenshtein edit distance between reference and hypothesis sequences."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,        # Deletion
                dp[i][j - 1] + 1,        # Insertion
                dp[i - 1][j - 1] + cost,  # Substitution
            )

    return dp[n][m]


def compute_cer(reference: str, hypothesis: str) -> float:
    """Calculate Character Error Rate (CER)."""
    ref_chars = list("".join(reference.split()))
    hyp_chars = list("".join(hypothesis.split()))
    if not ref_chars:
        return 0.0 if not hyp_chars else 1.0
    dist = levenshtein_distance(ref_chars, hyp_chars)
    return dist / float(len(ref_chars))


def compute_wer(reference: str, hypothesis: str) -> float:
    """Calculate Word Error Rate (WER)."""
    ref_words = reference.strip().split()
    hyp_words = hypothesis.strip().split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    dist = levenshtein_distance(ref_words, hyp_words)
    return dist / float(len(ref_words))


class PipelineBenchmarkTest(unittest.TestCase):
    """Benchmark harness gating pipeline accuracy."""

    def test_cer_wer_computation_accuracy(self) -> None:
        ref = "Paracetamol 500mg USD 10.50/kg"
        hyp = "Paracetamol 500mg USD 10.50/kg"
        self.assertEqual(compute_cer(ref, hyp), 0.0)
        self.assertEqual(compute_wer(ref, hyp), 0.0)

        hyp_err = "Paracetamol 500mg USD 10.50"
        self.assertGreater(compute_wer(ref, hyp_err), 0.0)
        self.assertLess(compute_wer(ref, hyp_err), 0.5)

    def test_native_pdf_benchmark(self) -> None:
        """Test extraction on clean native PDF."""
        doc = fitz.open()
        page = doc.new_page()
        expected_text = "Supplier Catalog 2026\nIngredient: Vitamin C 99%\nPrice: USD 12.50 per kg"
        page.insert_textbox(fitz.Rect(50, 50, 500, 300), expected_text)

        tmp_pdf = Path("test_native_benchmark.pdf")
        doc.save(tmp_pdf)
        doc.close()

        try:
            res = process_document(tmp_pdf)
            extracted = res.full_text()
            cer = compute_cer(expected_text, extracted)
            wer = compute_wer(expected_text, extracted)

            self.assertLessEqual(cer, 0.05, f"CER too high on native PDF: {cer:.3f}")
            self.assertLessEqual(wer, 0.05, f"WER too high on native PDF: {wer:.3f}")
        finally:
            if tmp_pdf.exists():
                tmp_pdf.unlink()


if __name__ == "__main__":
    unittest.main()
