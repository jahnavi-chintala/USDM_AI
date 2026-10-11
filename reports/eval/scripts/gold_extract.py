"""Dump the MAIN protocol PDF text layer page by page (local scratch use only).

Usage: python gold_extract.py <out_dir>
Writes page_NNN.txt (raw text layer) and all_pages.txt (with page markers).
"""
import sys
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from project_paths import main_pdf  # noqa: E402

PDF = main_pdf()


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(PDF))
    parts = []
    for i in range(len(pdf)):
        txt = pdf[i].get_textpage().get_text_range()
        (out / f"page_{i + 1:03d}.txt").write_text(txt, encoding="utf-8")
        parts.append(f"\n\n=================== PAGE {i + 1} ===================\n{txt}")
    (out / "all_pages.txt").write_text("".join(parts), encoding="utf-8")
    print("pages:", len(pdf))


if __name__ == "__main__":
    main()
