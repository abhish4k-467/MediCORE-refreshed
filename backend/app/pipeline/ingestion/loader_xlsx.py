"""Spreadsheet (.xlsx, .xls, .csv) file loader using openpyxl / xlrd / pandas.

Owned by: pipeline/ingestion/loader_xlsx.py
"""

import csv
import io
from pathlib import Path
import pandas as pd


class ExcelWrapper:
    """Wrapper around spreadsheet data extraction."""

    def __init__(self, sheets_data: dict[str, pd.DataFrame], raw_text: str | None, file_path: Path):
        self.sheets_data = sheets_data
        self.raw_text = raw_text
        self.file_path = file_path


def format_df_to_markdown(df: pd.DataFrame) -> str:
    """Format DataFrame to clean, unpadded Markdown table."""
    headers = [str(c).strip() for c in df.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in df.itertuples(index=False):
        row_cells = [str(cell if pd.notna(cell) else "").strip() for cell in row]
        lines.append("| " + " | ".join(row_cells) + " |")
    return "\n".join(lines)


def load_xlsx(file_path: Path) -> ExcelWrapper:
    """Open spreadsheet file (.xlsx, .xls, .csv) and parse into DataFrames per sheet."""
    suffix = file_path.suffix.lower()
    sheets: dict[str, pd.DataFrame] = {}
    raw_text: str | None = None

    if suffix == ".csv":
        raw_bytes = file_path.read_bytes()
        raw_content = None
        for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "windows-1252", "latin1"):
            try:
                raw_content = raw_bytes.decode(enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if raw_content is None:
            raw_content = raw_bytes.decode("utf-8", errors="ignore")

        raw_text = raw_content
        lines = [line.strip() for line in raw_content.splitlines() if line.strip()]

        if lines:
            delims = ["|", ";", "\t", ","]
            best_delim = max(delims, key=lambda d: sum(line.count(d) for line in lines))

            try:
                reader = csv.reader(io.StringIO(raw_content), delimiter=best_delim)
                all_rows = [row for row in reader if any(cell.strip() for cell in row)]
            except Exception:
                all_rows = [line.split(best_delim) for line in lines]

            header_idx = 0
            for i, r in enumerate(all_rows):
                if sum(1 for c in r if c.strip()) >= 2:
                    header_idx = i
                    break

            table_rows = all_rows[header_idx:]
            max_cols = max((len(r) for r in table_rows), default=1)
            padded_rows = [
                [" ".join(cell.split()) for cell in r] + [""] * (max_cols - len(r)) for r in table_rows
            ]

            if len(padded_rows) > 1:
                df = pd.DataFrame(padded_rows[1:], columns=padded_rows[0])
            else:
                df = pd.DataFrame(padded_rows)

            sheets["CSV"] = df.fillna("")
    elif suffix == ".xls":
        excel_file = pd.ExcelFile(file_path, engine="xlrd")
        for sheet_name in excel_file.sheet_names:
            sheets[sheet_name] = pd.read_excel(excel_file, sheet_name=sheet_name, dtype=str).fillna("")
    else:  # .xlsx, .xlsm, etc.
        excel_file = pd.ExcelFile(file_path, engine="openpyxl")
        for sheet_name in excel_file.sheet_names:
            sheets[sheet_name] = pd.read_excel(excel_file, sheet_name=sheet_name, dtype=str).fillna("")

    return ExcelWrapper(sheets_data=sheets, raw_text=raw_text, file_path=file_path)
