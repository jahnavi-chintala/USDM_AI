"""Settings for protocol post-processing (protocol_postprocess.py, table_merge.py).

Defaults auto-detect from content and are safe for protocols from any sponsor;
a profile only overrides. A profile is an optional JSON or YAML file holding a
flat mapping of field names to values, e.g. ``{"join_cut_rows": "off"}``.
Unknown keys are rejected so a typo cannot silently fall back to a default.
"""

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class ProtocolSettings:
    heading_levels: str = "multi_signal"
    """multi_signal | legacy. multi_signal (structure.assign_heading_levels): the PDF outline when it
    matches, else numbering-scheme detection (Arabic, Roman, alpha, "Section N", appendix) + TOC entries
    + level-1 consensus; legacy: protocol_postprocess.fix_heading_levels only."""
    heading_source: str = "auto"
    """auto | outline | numbering. auto uses the PDF bookmarks when at least
    `outline_min_match` of them match a section header, else the numbering."""
    outline_min_match: float = 0.8
    """Share of bookmarks (table/figure titles excluded) that must match headings."""
    successor_max_gap: int = 3
    """A section number is accepted only if it follows the previous accepted one:
    same prefix with a component +1..+gap, or a new child ".1"."""
    appendix_pattern: str = r"^(?:Appendix|APPENDIX)\s+(?:[A-Z]|\d+)\b"
    """Unnumbered appendix headings; level 1, or 2 under an "...APPENDICES" section."""

    table_title_pattern: str = r"(?i)^\s*table\s+([A-Z]?\d+(?:[.\-]\d+)*)\s*([.:\-–—]?)\s+\S"
    """Table titles; group 2 is the separator (a title without one must be a
    heading or caption, so "Table 5 shows ..." body text is not a title)."""
    title_similarity: float = 0.9
    """Normalised title similarity for a title to count as a repeat of an earlier one."""
    caption_position: str = "auto"
    """auto | above | below: where table titles sit; auto = majority vote."""
    footnote_markers: str | list[str] = "auto"
    """auto, or a list of regexes. auto keeps the default families that occur
    as the first item directly below a table in this document."""
    footnote_max_gap_lines: float = 3.0
    """Max vertical gap, in body line heights, between a table (or the previous
    footnote) and the next footnote."""
    join_cut_rows: str = "auto"
    """auto | on | off. Join a label-less first row on a continuation page onto
    the previous row; auto needs geometric and textual evidence of a cut row."""
    collapse_split_columns: bool = False
    """Merge a page whose header splits one column in two by joining the halves.
    Off: on real Schedules of Activities the "split" page was the correct one
    (TableFormer fused column pairs on the other pages), so collapsing hid the
    error. The fragments are linked as a continuation chain instead, until a
    ruling-line check can tell which page is right."""

    unknown_pua: str = "keep"
    """keep | replace (with U+FFFD). Unknown private-use glyphs are never deleted."""
    toc_delete_scope: str = "entries"
    """entries | pages. On TOC pages delete only TOC-entry-like loose items,
    or (legacy) every loose text/list item on them."""
    furniture_band: float = 0.08
    """Top/bottom page fraction where running headers/footers live."""
    furniture_min_pages: int = 3
    """A header/footer text seen on this many pages may be demoted anywhere."""
    reconcile_text: bool = True
    """Repair text from pdfium character geometry (text_reconcile.py): superscripts as "^",
    dropped line-end hyphens, merged words."""
    reconcile_min_alignment: float = 0.9
    """Minimum Docling-text-to-PDF-characters alignment for an item to be repaired (else skipped, logged)."""
    pair_label_values: bool = True
    """Join "Label:" items with the value on the same baseline (cover pages, lists) into one item."""
    label_pairs_max_page: int | None = None
    """Only pair on pages up to this one (None: all pages)."""
    figure_reocr: bool = True
    """OCR each bitmap figure once as a whole crop (figure_ocr.py) instead of keeping Docling's
    per-region OCR, which misreads and jumbles diagrams."""
    table_lattice: str = "correct"
    """off | verify | correct: check table fragments against the PDF's ruling lines (table_lattice.py);
    "correct" rebuilds fully ruled fragments whose model grid disagrees. Unruled tables are only flagged."""
    lattice_min_rule_coverage: float = 0.9
    """Share of grid edges that must be drawn rules for a fragment to count as fully ruled."""
    lattice_min_text_match: float = 0.9
    """Minimum cell-text agreement below which a ruled fragment counts as disagreeing."""
    lattice_row_ruled: bool = True
    """Also check tables ruled with horizontal lines only: rows from the rules, columns from TableFormer
    placed in the whitespace between the columns' words (table_lattice row-ruled mode)."""
    lattice_min_row_rule_coverage: float = 0.9
    """Share of vertically adjacent cell pairs that horizontal rules must separate for a row-ruled
    rebuild (booktabs and row-group rules stay below it)."""
    lattice_max_col_rule_coverage: float = 0.1
    """At most this share of side-by-side cell pairs may have a vertical rule for row-ruled mode."""
    lattice_text_rows: bool = True
    """In tables without rules between body rows (booktabs), join a one-cell TableFormer row to the row
    above when the text layer shows it is that row's wrapped continuation (same style, aligned, tighter
    than the row spacing, not numbered; table_lattice.join_text_rows)."""
    lattice_text_rows_unruled: bool = False
    """Also apply lattice_text_rows to tables with no rules at all (more often lists or TOCs)."""
    header_note_gap: float = 1.0
    """Vertical gap (in line heights) that splits a note out of a header cell; 0 turns it off."""
    figure_ocr_order: str = "auto"
    """auto | columns | rows: reading order of OCR fragments inside figures."""
    join_hyphen_splits: bool = True
    """Join a body paragraph or list item the layout model split into two items at a line that ends with
    a hyphen (protocol_postprocess.join_hyphen_split_items)."""
    reading_order_repair: bool = True
    """Move text the converter read before a numbered heading although it lies below that heading on the
    same page (structure.repair_reading_order), so chunks, parents and protocol_api see the same order."""

    @classmethod
    def load(cls, path: str | Path | None) -> "ProtocolSettings":
        """Settings from a JSON/YAML profile; defaults when `path` is None."""
        if path is None:
            return cls()
        text = Path(path).read_text(encoding="utf-8")
        if str(path).endswith((".yaml", ".yml")):
            import yaml  # optional dependency, only needed for YAML profiles

            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown protocol settings: {sorted(unknown)}")
        return cls(**data)
