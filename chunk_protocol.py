"""Chunk the converted protocol (lossless DoclingDocument JSON) for RAG.

Runs on the saved JSON only, so it can be re-run whenever the conversion is
regenerated. See reports/chunking_design.md for the reasoning. Choices:
  * docling-core HybridChunker: one structural chunk per paragraph / list /
    table / figure under its heading path, oversized ones split (lists at item
    boundaries, prose with semchunk), undersized neighbours with the same
    heading path merged (merge_peers).
  * Tokenizer is pluggable and must match the embedding model (HF name, or
    "tiktoken:<encoding>"); default Qwen/Qwen3-Embedding-8B, the tokenizer of
    the embedding model used by embed_chunks.py (qwen/qwen3-embedding-8b via
    OpenRouter), so token counts are exact. The model accepts 32K tokens; the
    512-token limit is a retrieval-precision choice, not a model limit.
    The limit applies to the embedding text = contextualize(): protocol prefix
    + heading breadcrumb + chunk text.
  * Tables (custom serializer): "rows" (default) writes one self-describing
    line per row ("Row: Column = value; ...", blank cells dropped, merged
    header rows flattened, footnotes appended), so a large table such as the
    Schedule of Activities is split only between rows and each part repeats
    the caption and a "Columns:" line (a row longer than a chunk is split at
    sentence boundaries with its row label repeated); "markdown" writes compact
    GFM with the header row repeated per part. Full tables also go to tables.jsonl for small-to-big
    display.
  * Figures (custom serializer): caption + type + description / summary /
    Mermaid (picture meta, legacy annotations or child items, as a VLM step
    may add) + OCR "Text in figure"; image path kept in metadata; only small,
    confidently classified logos/icons without any text are dropped.
  * Document metadata (id, version, date, acronym) is detected from content
    (doc_metadata, built on detect_metadata, which also names the rule and the
    source item of each value; identity.py keeps those); the prefix holds
    only fields found, never the file name. Chunk ids are
    "{protocol_id}-A{amendment}-NNNN" when both are known, else
    "doc-<source sha>-NNNN". section_number uses only section numbers the
    heading-level step accepted. --quiet-text logs counts only.
  * TOC and List of Tables (DOCUMENT_INDEX) excluded; page headers/footers are
    furniture and never enter. The abbreviation glossary (glossary.py: list
    tables / text lists found by content, plus inline "long form (SF)"
    definitions) is exported for query expansion; chunks of an abbreviations
    table get chunk_type "glossary".
  * Outputs (output/chunks/): chunks.jsonl, tables.jsonl, abbreviations.json
    ({abbr: definition}), glossary.json (source, page, ref, alternatives per
    entry), stats.json; quality checks are logged and stored in stats.json.
  * Parents (parents.py): sections.json (the section tree: structure.py's
    index, or the fallback built from headings) and parents.jsonl (the
    non-overlapping structural partition, 300-1,500 tokens, Markdown text);
    each chunk record gets section_id, role and parent_id (metadata only:
    text, embed_text, content_hash and chunk ids never change).
  * stats.json "document"."identifiers": registry ids found by pattern
    anywhere, furniture included (NCT, EudraCT, EU CT, IND, WHO UTN, sponsor
    protocol id), each with its verbatim source text and page.
"""

import argparse
import hashlib
import json
import logging
import re
import statistics
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from docling_core.transforms.chunker import DocChunk, HybridChunker
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.transforms.chunker.line_chunker import LineBasedTokenChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.serializer.base import (
    BaseDocSerializer,
    BaseSerializerProvider,
    SerializationResult,
)
from docling_core.transforms.serializer.common import _DEFAULT_LABELS, create_ser_result
from docling_core.transforms.serializer.markdown import (
    MarkdownParams,
    MarkdownPictureSerializer,
    MarkdownTableSerializer,
    OrigListItemMarkerMode,
    _count_header_rows,
    _flatten_header_rows,
)
from docling_core.types.doc import (
    CodeItem,
    ContentLayer,
    DocItem,
    DocItemLabel,
    DoclingDocument,
    ImageRefMode,
    PictureItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)
from docling_core.types.doc.document import DescriptionAnnotation, RichTableCell
from pydantic import ConfigDict
import semchunk

from figure_vlm import skip_figure
from glossary import build_glossary

log = logging.getLogger("chunk_protocol")

_SECTION_NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
TABLE_LABELS = {DocItemLabel.TABLE, DocItemLabel.DOCUMENT_INDEX}
INFO_CHECKS = {"headings_without_content", "long_rows_split_with_label"}


# --------------------------------------------------------------------------- tokenizer

class BudgetTokenizer(BaseTokenizer):
    """Wraps a tokenizer with a smaller limit (room left after the context prefix)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    inner: BaseTokenizer
    limit: int

    def count_tokens(self, text: str) -> int:
        return self.inner.count_tokens(text)

    def get_max_tokens(self) -> int:
        return self.limit

    def get_tokenizer(self) -> Any:
        return self.inner.get_tokenizer()


def build_tokenizer(spec: str, max_tokens: int) -> BaseTokenizer:
    if spec.startswith("tiktoken:"):  # needs `pip install tiktoken`
        import tiktoken
        from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer

        return OpenAITokenizer(tokenizer=tiktoken.get_encoding(spec.split(":", 1)[1]),
                               max_tokens=max_tokens)
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    return HuggingFaceTokenizer.from_pretrained(model_name=spec, max_tokens=max_tokens)


# --------------------------------------------------------------------------- serializers

def _cell_text(cell, doc: DoclingDocument, doc_serializer: BaseDocSerializer) -> str:
    if isinstance(cell, RichTableCell):
        text = doc_serializer.serialize(item=cell.ref.resolve(doc), _nested_in_table=True).text
    else:
        text = cell.text or ""
    return " ".join(text.split())


LONG_COLUMN_NAME = 60  # characters; longer column names get a letter alias in row lines
LONG_ROW_LABEL = 150  # characters; longer row labels are shortened when repeated on a continuation


def short_label(label: str) -> str:
    """The row label repeated on continuation pieces: whole up to LONG_ROW_LABEL,
    else its first words (≤ 60 characters) followed by "…"."""
    if len(label) <= LONG_ROW_LABEL:
        return label
    cut = label[:60].rsplit(" ", 1)[0] if " " in label[:60] else label[:60]
    return cut.rstrip(" ,;:") + "…"


def row_label_matches(base: str, row: str) -> bool:
    """Is `base` (a label as written on a row piece) the label of full row `row`?"""
    if base.endswith("…"):
        return row.startswith(base[:-1]) and len(row.partition(": ")[0]) > LONG_ROW_LABEL
    return row.startswith(base + ": ")
ROWS_LINE_BUDGET = 40  # tokens reserved for the "Rows: ..." line of each split table part (enforced)


_NUMERIC_CELL = re.compile(r"[\d.,:%<>=≤≥±/\s\-–]*\d[\d.,:%<>=≤≥±/\s\-–]*")


def first_row_is_header(texts: list[list[str]]) -> bool:
    """For tables TableFormer flagged no header in: is row 0 a header anyway?

    Strict, because a wrong guess turns a data row into every row's column
    names: >= 3 columns and >= 3 rows (2-column tables are key/value lists whose
    row 0 is data, and print no column names anyway), and every row-0 cell a
    short, distinct, non-numeric label that does not end like a sentence.
    """
    if len(texts) < 3 or len(texts[0]) < 3:
        return False
    first = texts[0]
    return len(set(first)) == len(first) and all(
        t and len(t) <= 40 and not _NUMERIC_CELL.fullmatch(t) and not t.endswith((".", ";", ","))
        for t in first)


_SOA_ROWS: list[tuple[Any, dict[str, set[int]]]] = []  # (document, rows) for the last two documents


def soa_section_rows(doc: DoclingDocument) -> dict[str, set[int]]:
    """Table ref -> grid rows that soa.py reads as section headings although they hold only a label
    (Schedules of Activities without rows across the table). Computed once per document; {} when soa.py
    finds nothing or fails."""
    for d, rows in _SOA_ROWS:
        if d is doc:
            return rows
    rows: dict[str, set[int]] = {}
    try:
        from soa import _index, extract_soa

        for s in extract_soa(doc, _index(doc)):
            for x in s.get("section_rows", []):
                rows.setdefault(x["table_id"], set()).add(x["row"])
    except Exception:  # noqa: BLE001 - the serializer works without it (label-only rows stay plain lines)
        rows = {}
    _SOA_ROWS[:] = [*_SOA_ROWS[-1:], (doc, rows)]
    return rows


class ProtocolTableSerializer(MarkdownTableSerializer):
    """Tables as self-describing row lines ("rows") or compact Markdown.

    Only the table itself is the span source, so a table stays a single doc
    item (captions/footnotes ride along) and HybridChunker treats it as one
    unit to split row-wise.

    Row lines ("Label: Column = value; ...") are kept compact without losing
    structure:
      * a column group (a header cell spanning several columns with column
        names under it) is written once per row around its columns' values,
        "Group [Day 1 = X; Day 8 = X]", and once on the Columns line,
        "Group [Day 1 | Day 8]";
      * a header cell holding a name and a note below it (two text blocks, a
        newline in the cell text) contributes only the name to column names;
        the note is stated once on the Columns line ("(header note: ...)");
      * a full-width body row is a section: written once as "Section: ...",
        and its short name prefixes the labels of the rows beneath it until
        the next section ("Section name › Row label: ..."); so is a label-only
        row of a Schedule of Activities that soa.py reads as a section heading
        (soa_section_rows);
      * a body row repeating the header's row-label cell starts a new block of
        column labels (stacked header): it is written as "Label: a | b | ..."
        and the rows below use its labels.
    """

    HEADER_SEP = " - "  # between the levels of a column's header path (as Docling flattens it)
    SECTION_SEP = " › "  # between a carried section name and a row label
    SECTION_NAME = 60  # characters; longer section rows are named by their first phrase
    CARRY_SECTIONS = True  # prefix row labels with the section they sit under

    def __init__(self, fmt: str = "rows"):
        self.fmt = fmt

    def serialize(self, *, item: TableItem, doc_serializer: BaseDocSerializer,
                  doc: DoclingDocument, **kwargs: Any) -> SerializationResult:
        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result()
        caption = doc_serializer.serialize_captions(item=item, **kwargs).text
        notes = doc_serializer.serialize_footnotes(item=item, **kwargs).text
        if self.fmt == "markdown":
            body = super().serialize(item=item, doc_serializer=doc_serializer, doc=doc,
                                     **{**kwargs, "compact_tables": True}).text
            if caption and body.startswith(caption):
                body = body[len(caption):].lstrip("\n")
            parts = [caption, body]
        else:
            cols, rows = self.columns_and_rows(item, doc, doc_serializer)
            parts = [self.rows_prefix(caption, cols).rstrip("\n"), "\n".join(rows)]
        parts.append(f"Notes: {notes}" if notes else "")
        return create_ser_result(text="\n\n".join(p for p in parts if p), span_source=item)

    @staticmethod
    def rows_prefix(caption: str, cols: list[str]) -> str:
        named = [c for c in cols if c]
        return "".join(f"{line}\n" for line in (
            caption, f"Columns: {' | '.join(named)}" if named else "") if line)

    @classmethod
    def _header_model(cls, rows: list[list], n_cols: int, doc: DoclingDocument,
                      doc_serializer: BaseDocSerializer) -> dict[str, list]:
        """Column names from header rows (grid rows; a spanning cell repeats in them).

        Lists per column: "group" (the header path above the leaf, levels joined
        by HEADER_SEP), "leaf" (the lowest name), "cells" (the header cells of the
        path); "notes" maps a header cell's id to its note (a header cell's text
        before its first newline is its name, the rest a note); "titles" holds
        header cells spanning the whole table.
        """
        out: dict = {"group": [], "leaf": [], "cells": [], "split": [], "notes": {}, "titles": []}
        for col in range(n_cols):
            parts, cells, seen = [], [], set()
            for row in rows:
                cell = row[col] if col < len(row) else None
                if cell is None or id(cell) in seen:
                    continue
                seen.add(id(cell))
                if isinstance(cell, RichTableCell):
                    blocks = [_cell_text(cell, doc, doc_serializer)]
                else:
                    blocks = [" ".join(b.split()) for b in (cell.text or "").split("\n") if b.strip()]
                if not blocks or not blocks[0]:
                    continue
                if n_cols > 1 and cell.start_col_offset_idx == 0 and cell.end_col_offset_idx >= n_cols:
                    if col == 0:  # a title across the whole table, not a column group
                        out["titles"].append(" ".join(blocks))
                    continue
                if len(blocks) > 1:
                    out["notes"][id(cell)] = " ".join(blocks[1:])
                if not parts or parts[-1] != blocks[0]:
                    parts.append(blocks[0])
                    cells.append(cell)
            # The group is the path's cells spanning several columns above its first
            # single-column cell; the rest is the column's own name. Without spanning
            # cells, the last level is the name and the levels above it the group.
            split = next((k for k, c in enumerate(cells) if c.col_span == 1), len(cells) - 1)
            if split <= 0:
                split = max(len(parts) - 1, 0)
            out["group"].append(cls.HEADER_SEP.join(parts[:split]))
            out["leaf"].append(cls.HEADER_SEP.join(parts[split:]))
            out["cells"].append(cells)
            out["split"].append(split)
        # A group is worth writing once only over two or more distinct columns; otherwise
        # (a header wrapped over two header rows) the whole path is the column name.
        groups, leaves, cells = out["group"], out["leaf"], out["cells"]
        j = 0
        while j < n_cols:
            k = j + 1
            while k < n_cols and groups[j] and groups[k] == groups[j]:
                k += 1
            if groups[j] and len({id(cells[c][-1]) for c in range(j, k) if cells[c]}) < 2:
                for c in range(j, k):
                    leaves[c] = cls.HEADER_SEP.join(p for p in (groups[c], leaves[c]) if p)
                    groups[c], out["split"][c] = "", 0
            j = k
        return out

    @classmethod
    def _section_name(cls, text: str) -> str:
        """Short name of a section row carried into the labels below it: the whole text
        up to SECTION_NAME characters, else its first phrase."""
        if len(text) <= cls.SECTION_NAME:
            return text
        cuts = [i for sep in (" – ", " — ", " - ", ": ", "; ", ". ", " (")
                if 3 <= (i := text.find(sep)) <= cls.SECTION_NAME]
        if cuts:
            return text[:min(cuts)]
        cut = text[:cls.SECTION_NAME].rsplit(" ", 1)[0]
        return cut.rstrip(" ,;:") + "…"

    @staticmethod
    def columns_and_rows(item: TableItem, doc: DoclingDocument,
                         doc_serializer: BaseDocSerializer) -> tuple[list[str], list[str]]:
        """Entries of the 'Columns:' line and one 'Row label: Column = value; ...' line per
        row (column groups, header notes, sections and stacked headers: see the class
        docstring)."""
        cls = ProtocolTableSerializer
        grid = item.data.grid
        if not grid:
            return [], []
        n_cols = len(grid[0])
        texts = [[_cell_text(c, doc, doc_serializer) for c in row] for row in grid]
        has_flags = any(c.column_header for c in item.data.table_cells)
        n_head = _count_header_rows(item) if has_flags else int(first_row_is_header(texts))
        head = cls._header_model(grid[:n_head], n_cols, doc, doc_serializer)
        # Sentence-length column or group names (e.g. Appendix 13's stopping-rule
        # tables) would be repeated in every row and make each part's embedding
        # mostly header; give them a short alias, defined once on the Columns line.
        long_names = list(dict.fromkeys(n for n in [*head["leaf"][1:], *head["group"][1:]]
                                        if len(n) > LONG_COLUMN_NAME))
        alias = {name: chr(ord("A") + k) for k, name in enumerate(long_names)}
        entries = cls._columns_entries(head, n_cols, alias)

        stub = "".join("".join(t.split()).lower() for t in dict.fromkeys(texts[r][0] for r in range(n_head)) if t)
        section_rows = soa_section_rows(doc).get(item.self_ref, set())
        lines, section, r = [], "", n_head
        while r < len(grid):
            row = grid[r]
            seen: set[int] = set()
            starting = [c for c in row if c.start_row_offset_idx == r and c.text.strip()
                        and not (id(c) in seen or seen.add(id(c)))]
            if not starting:  # every filled slot continues a cell from above: nothing new
                r += 1
                continue
            label = texts[r][0]
            # A stacked header block: the row label repeats the header's row-label cell.
            if (len(stub) >= 3 and "".join(label.split()).lower() == stub
                    and any(texts[r][c] and texts[r][c] != label for c in range(1, n_cols))):
                end = max(row[0].end_row_offset_idx, r + 1)
                head = cls._header_model(grid[r:end], n_cols, doc, doc_serializer)
                for rr in range(r, end):
                    vals = [texts[rr][c] for c in range(1, n_cols)
                            if texts[rr][c] and grid[rr][c] is not grid[rr][c - 1]]
                    lines.append(f"{label}: {' | '.join(vals)}" if vals else label)
                r = end
                continue
            # A section row: one cell across every column, or a label-only row that soa.py reads as the
            # heading of the activities below it (a Schedule of Activities without rows across it).
            if (n_cols > 1 and len(starting) == 1 and starting[0].start_col_offset_idx == 0 and label
                    and (starting[0].end_col_offset_idx >= n_cols or r in section_rows)):
                lines.append(f"Section: {label}")
                section = cls._section_name(label)
                r += 1
                continue
            fields, last = [], None  # fields: [first col, last col, value]
            for c in range(1, n_cols):
                value, cell = texts[r][c], row[c]
                if not value or value == label or value == head["leaf"][c]:  # blank / span echo
                    continue
                if cell is last:  # horizontally merged cell: widen its column range
                    fields[-1][1] = c
                    continue
                last = cell
                fields.append([c, c, value])
            body = cls._render_fields(fields, head, alias, n_cols)
            if label and section and cls.CARRY_SECTIONS:
                label = f"{section}{cls.SECTION_SEP}{label}"
            line = f"{label}: {body}" if label and body else (label or body)
            if line:
                lines.append(line)
            r += 1
        return entries, lines

    @staticmethod
    def _name(name: str, alias: dict, cells: list, notes: dict, define: bool) -> str:
        """A column/group name as written: its alias (defined as "[A] name" on the Columns
        line), plus there the header notes of its cells."""
        if name in alias:
            name = f"[{alias[name]}] {name}" if define else alias[name]
        note = " ".join(notes[id(c)] for c in cells if id(c) in notes) if define else ""
        return f"{name} (header note: {note})" if note else name

    @classmethod
    def _columns_entries(cls, head: dict, n_cols: int, alias: dict) -> list[str]:
        """Columns line entries: a run of columns under one group as "Group [a | b]"; a
        name whose cell spans several columns once."""
        groups, leaves, cells, notes, split = head["group"], head["leaf"], head["cells"], head["notes"], head["split"]

        def same_leaf(a: int, b: int) -> bool:
            return bool(cells[a]) and bool(cells[b]) and cells[a][-1] is cells[b][-1]

        entries, j = [], 0
        while j < n_cols:
            if groups[j]:
                k = j
                while k < n_cols and groups[k] == groups[j]:
                    k += 1
                subs = [cls._name(leaves[c], alias, cells[c][split[c]:], notes, True) for c in range(j, k)
                        if leaves[c] and (c == j or not same_leaf(c, c - 1))]
                group_cells = list({id(x): x for c in range(j, k) for x in cells[c][:split[c]]}.values())
                entries.append(f"{cls._name(groups[j], alias, group_cells, notes, True)} [{' | '.join(subs)}]")
                j = k
                continue
            if leaves[j] and (j == 0 or groups[j - 1] or not same_leaf(j, j - 1)):
                entries.append(cls._name(leaves[j], alias, cells[j], notes, True))
            j += 1
        if head["titles"]:  # "Columns: Title: a | b | c"
            title = " - ".join(head["titles"])
            entries = [f"{title}: {entries[0]}" if entries else title] + entries[1:]
        return entries

    @classmethod
    def _render_fields(cls, fields: list, head: dict, alias: dict, n_cols: int) -> str:
        """'Column = value' parts; consecutive values under one column group are written
        inside it, 'Group [Leaf = value; Leaf = value]'; a value spanning columns names
        its range ('A / B', 'A to C')."""
        groups, leaves = head["group"], head["leaf"]

        def named(n: str) -> str:
            return cls._name(n, alias, [], {}, False)

        def span(names: list[str]) -> str:
            names = [n for n in names if n]
            return f"{names[0]} to {names[-1]}" if len(names) > 2 else " / ".join(names)

        def full(c: int) -> str:
            return cls.HEADER_SEP.join(named(n) for n in (groups[c], leaves[c]) if n)

        parts, i = [], 0
        while i < len(fields):
            c0, c1, value = fields[i]
            group = groups[c0] if all(groups[c] == groups[c0] for c in range(c0, c1 + 1)) else ""
            if group and n_cols > 2:
                sub = []
                while i < len(fields) and all(groups[c] == group for c in range(fields[i][0], fields[i][1] + 1)):
                    a, b, v = fields[i]
                    col = span([named(leaves[c]) for c in range(a, b + 1)])
                    sub.append(f"{col} = {v}" if col else v)
                    i += 1
                parts.append(f"{named(group)} [{'; '.join(sub)}]")
                continue
            col = span([full(c) for c in range(c0, c1 + 1)])
            # Two-column tables (glossary, dose levels): the Columns line says it all.
            parts.append(f"{col} = {value}" if col and n_cols > 2 else value)
            i += 1
        return "; ".join(parts)

    def split_prefix(self, text: str, item: TableItem, doc: DoclingDocument,
                     doc_serializer: BaseDocSerializer) -> tuple[str, list[str]]:
        """(lines to repeat on every part, row lines) of a serialized table."""
        if self.fmt == "markdown":
            header, body = self.get_header_and_body_lines(table_text=text)
            if header:
                start = text.find(header[0])
                return text[:start] + "".join(header), body
            return "", text.splitlines(True)
        cols, _ = self.columns_and_rows(item, doc, doc_serializer)
        prefix = self.rows_prefix(item.caption_text(doc), cols)
        if prefix and text.startswith(prefix):
            text = text[len(prefix):]
        else:
            prefix = ""
        return prefix, [ln for ln in text.splitlines(True) if ln.strip()]


def figure_parts(item: PictureItem, doc: DoclingDocument) -> dict[str, Any]:
    """Collect everything textual known about a figure, wherever it was stored."""
    meta = item.meta
    cls = None
    if meta and meta.classification and meta.classification.predictions:
        cls = meta.classification.get_main_prediction().class_name
    description, mermaid = [], []
    if meta:
        for name in ("description", "summary"):
            if (f := getattr(meta, name, None)) is not None and getattr(f, "text", ""):
                description.append(f.text)
        if meta.code is not None and meta.code.text:
            mermaid.append(meta.code.text)
        for key, val in meta.get_custom_part().items():  # e.g. "vlm__mermaid"
            text = val.get("text") if isinstance(val, dict) else val if isinstance(val, str) else None
            if text:
                (mermaid if "mermaid" in key.lower() else description).append(text)
    if not description:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            description += [a.text for a in item.annotations if isinstance(a, DescriptionAnnotation)]
    captions = {c.cref for c in item.captions} | {f.cref for f in item.footnotes}
    child_texts = []
    for child, _ in doc.iterate_items(root=item, traverse_pictures=True, with_groups=False):
        if child is item or child.self_ref in captions or not isinstance(child, TextItem):
            continue
        text = child.text.strip()
        if not text:
            continue
        if isinstance(child, CodeItem) or text.startswith(("```mermaid", "flowchart", "graph ")):
            mermaid.append(text)
        else:
            child_texts.append(text)
    return {"class": cls, "caption": item.caption_text(doc), "description": description,
            "mermaid": mermaid, "texts": child_texts}


def dropped_figure(item: PictureItem, doc: DoclingDocument, f: dict[str, Any]) -> bool:
    """A confidently classified small logo/icon/... with no text of any kind; never a figure with OCR text."""
    return skip_figure(item, doc) and not (f["caption"] or f["description"] or f["mermaid"] or f["texts"])


def figure_title(item: PictureItem, f: dict[str, Any]) -> str:
    page = item.prov[0].page_no if item.prov else "?"
    title = f"Figure: {f['caption']}" if f["caption"] else f"Figure (page {page})"
    return f"{title} [{f['class'].replace('_', ' ')}]" if f["class"] else title


_GENERATED_LABEL = re.compile(r"^\*?Figure description generated by (.+?);.*?\*?\s*$")


def figure_blocks(f: dict[str, Any]) -> list[tuple[str, str]]:
    """A figure's content as labelled blocks, so every chunk says what its text is."""
    blocks = [("Diagram (Mermaid, AI-generated)",
               m if m.startswith("```") else f"```mermaid\n{m}\n```") for m in f["mermaid"]]
    for d in f["description"]:
        first, _, rest = d.partition("\n")
        if m := _GENERATED_LABEL.match(first.strip()):
            label, d = f"Description (AI-generated by {m.group(1)}; verify against the image)", rest
        else:
            label = "Description"
        blocks.append((label, d.strip()))
    for t in f["texts"]:
        blocks.append(("Text in figure (OCR)", t.removeprefix("Text in figure:").strip()))
    return blocks


class ProtocolPictureSerializer(MarkdownPictureSerializer):
    """A figure as one text block: title, type, description, Mermaid, OCR text."""

    def serialize(self, *, item: PictureItem, doc_serializer: BaseDocSerializer,
                  doc: DoclingDocument, **kwargs: Any) -> SerializationResult:
        if item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return create_ser_result()
        f = figure_parts(item, doc)
        if dropped_figure(item, doc, f):
            return create_ser_result()
        parts = [figure_title(item, f), *f["description"]]
        parts += [m if m.startswith("```") else f"```mermaid\n{m}\n```" for m in f["mermaid"]]
        parts += f["texts"]
        if visited := kwargs.get("visited"):
            visited.update(c.cref for c in item.children)
        return create_ser_result(text="\n".join(parts), span_source=item)


class ProtocolDocSerializer(ChunkingDocSerializer):
    def _item_wraps_meta(self, item) -> bool:
        # The picture serializer renders picture meta itself (no stray "Flow chart" line).
        return isinstance(item, PictureItem) or super()._item_wraps_meta(item)


class ProtocolSerializerProvider(BaseSerializerProvider):
    def __init__(self, table_format: str = "rows"):
        self.table_format = table_format

    def get_serializer(self, doc: DoclingDocument) -> ProtocolDocSerializer:
        return ProtocolDocSerializer(
            doc=doc,
            table_serializer=ProtocolTableSerializer(self.table_format),
            picture_serializer=ProtocolPictureSerializer(),
            params=MarkdownParams(
                labels=set(_DEFAULT_LABELS) - {DocItemLabel.DOCUMENT_INDEX},  # TOC, List of Tables
                layers={ContentLayer.BODY},
                image_mode=ImageRefMode.PLACEHOLDER,
                image_placeholder="",
                escape_underscores=False,
                escape_html=False,
                compact_tables=True,
                # Keep the protocol's own markers ("4.", "a."); never renumber unmarked
                # bullets, which would invent criterion numbers.
                orig_list_item_marker_mode=OrigListItemMarkerMode.ALWAYS,
                ensure_valid_list_item_marker=False,
            ),
        )


# --------------------------------------------------------------------------- chunker

class ProtocolChunker(HybridChunker):
    """HybridChunker with a document prefix and row-safe, budget-aware table splitting."""

    doc_prefix: str = ""

    def contextualize(self, chunk) -> str:
        parts = [self.doc_prefix] if self.doc_prefix else []
        if chunk.meta.headings:
            parts.append(" > ".join(chunk.meta.headings))
        parts.append(chunk.text)
        return "\n".join(parts)

    def segment(self, doc_chunk: DocChunk, available_length: int,
                doc_serializer: BaseDocSerializer) -> list[str]:
        items = doc_chunk.meta.doc_items
        if len(items) == 1 and items[0].label == DocItemLabel.PICTURE:
            pic = items[0].get_ref().resolve(doc_serializer.doc)
            return self._segment_figure(pic, doc_serializer.doc, available_length)
        ts = getattr(doc_serializer, "table_serializer", None)
        if not (len(items) == 1 and items[0].label in TABLE_LABELS
                and isinstance(ts, ProtocolTableSerializer)):
            return super().segment(doc_chunk, available_length, doc_serializer)
        # Unlike the stock table path, every part keeps the caption + header and the
        # budget leaves room for the prefix/headings added by contextualize().
        doc = doc_serializer.doc
        table = items[0].get_ref().resolve(doc)
        prefix, rows = ts.split_prefix(doc_chunk.text, table, doc, doc_serializer)
        # In "rows" format, leave room for the "Rows:" line added to each part.
        budget = available_length - (ROWS_LINE_BUDGET if ts.fmt == "rows" else 0)
        if ts.fmt == "rows":
            rows = self._fit_rows(rows, budget - self.tokenizer.count_tokens(prefix))
        line_chunker = LineBasedTokenChunker(
            tokenizer=BudgetTokenizer(inner=self.tokenizer, limit=budget),
            prefix=prefix,
            serializer_provider=self.serializer_provider,
        )
        parts = [s.strip("\n") for s in line_chunker.chunk_text(lines=rows)]
        if ts.fmt != "rows":
            return parts
        known = {r.partition(": ")[0].removesuffix(" (cont.)").strip() for r in rows}
        return [self._with_rows_line(p, known) for p in parts]

    def _with_rows_line(self, part: str, known: set[str]) -> str:
        """Name the rows a table part holds, right after its caption.

        Parts of one table share the caption and Columns line, so their
        embeddings barely differ by which rows they hold ("Amendment 1" vs
        "Amendment 2"); stating the row labels up front makes that explicit.
        Only the table's real row labels count, not "label: text" inside a cell.
        """
        labels = []
        for line in part.splitlines():
            label = line.partition(": ")[0].removesuffix(" (cont.)").strip()
            if label in known and len(label) <= 60 and label not in labels:
                labels.append(label)
        if not labels:
            return part
        lines = part.split("\n")
        at = 1 if lines and not lines[0].startswith("Columns:") else 0  # after the caption
        return "\n".join(lines[:at] + [self._rows_line(labels)] + lines[at:])

    def _rows_line(self, labels: list[str]) -> str:
        """The "Rows:" line, capped to the ROWS_LINE_BUDGET reserved for it (newline included).

        Long labels in another document could otherwise push a part past the
        chunk limit; shorten them (then just count rows) only when needed.
        """
        fits = lambda s: self.tokenizer.count_tokens(s + "\n") <= ROWS_LINE_BUDGET  # noqa: E731
        line = "Rows: " + (", ".join(labels) if len(labels) <= 4
                           else f"{labels[0]} … {labels[-1]} ({len(labels)} rows)")
        if fits(line):
            return line
        for width in (40, 25, 12):
            cut = [lb if len(lb) <= width else lb[:width].rstrip() + "…" for lb in (labels[0], labels[-1])]
            line = (f"Rows: {cut[0]}" if len(labels) == 1
                    else f"Rows: {cut[0]} … {cut[1]} ({len(labels)} rows)")
            if fits(line):
                return line
        return f"Rows: {len(labels)} rows"

    def _segment_figure(self, pic: PictureItem, doc: DoclingDocument, limit: int) -> list[str]:
        """Split a large figure into parts that each start with the figure title.

        Blocks (Mermaid, AI description, OCR text) are kept whole when they fit;
        a long block is split at sentence boundaries, and every piece names its
        block, so a retrieved part never presents generated text unlabelled.
        """
        f = figure_parts(pic, doc)
        title = figure_title(pic, f)
        budget = limit - self.tokenizer.count_tokens(title) - 8
        pieces = []
        for label, body in figure_blocks(f):
            block = f"{label}:\n{body}"
            if self.tokenizer.count_tokens(block) <= budget:
                pieces.append(block)
                continue
            head = f"{label} (part {{}}):\n"
            size = max(budget - self.tokenizer.count_tokens(head.format(99)) - 4, 16)
            split = semchunk.chunkerify(self.tokenizer.get_tokenizer(), chunk_size=size)(body)
            pieces += [head.format(k) + s.strip() for k, s in enumerate(split, start=1)]
        out, cur = [], []
        for piece in pieces:
            candidate = "\n\n".join([title, *cur, piece])
            if cur and self.tokenizer.count_tokens(candidate) > limit:
                out.append("\n\n".join([title, *cur]))
                cur = []
            cur.append(piece)
        if cur:
            out.append("\n\n".join([title, *cur]))
        return out

    def _fit_rows(self, rows: list[str], limit: int) -> list[str]:
        """Split rows longer than a chunk at sentence boundaries, repeating the row label.

        A very long label (a whole sentence in the first column) is repeated in
        shortened form ("first words… (cont.)"), so no piece is left unnamed.
        """
        out = []
        for line in rows:
            if self.tokenizer.count_tokens(line) <= limit:
                out.append(line)
                continue
            label, sep, rest = line.partition(": ")
            if not sep:
                label, rest = "", line
            cont = f"{short_label(label)} (cont.): " if label else ""
            head = f"{label}: " if label else ""
            size = max(limit - max(self.tokenizer.count_tokens(cont), self.tokenizer.count_tokens(head)) - 4, 16)
            pieces = semchunk.chunkerify(self.tokenizer.get_tokenizer(), chunk_size=size)(rest)
            for k, piece in enumerate(pieces):
                out.append((head if k == 0 else cont) + piece.strip() + "\n")
        return out


# --------------------------------------------------------------------------- metadata

META_FIELDS = ("protocol_id", "acronym", "title", "amendment", "version_label", "version_date")
_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
          r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)")
_DATE = re.compile("|".join(rf"(?:{p})" for p in (
    rf"\b\d{{1,2}}\s+{_MONTH}\.?,?\s+\d{{4}}\b",     # 5 January 2021, 5 Jan. 2021
    rf"\b\d{{1,2}}[-/ ]{_MONTH}[-/ ]\d{{4}}\b",      # 05-Jan-2021
    rf"\b\d{{1,2}}{_MONTH}\d{{4}}\b",                # 05Jan2021
    rf"\b{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}}\b",     # March 14, 2021
    r"\b\d{4}-\d{2}-\d{2}\b",                        # 2021-01-05
)), re.I)
# "Protocol No.: MK-3475-001", "Study 20180123", "Clinical Study Protocol D9480C00002"
_LABELLED_ID = re.compile(r"\b(?i:Protocol|Study|Trial)\s*(?i:No\.?|Number|ID|Code|Identifier)?\s*[:#]?\s*"
                          r"((?=[A-Z0-9\-_/]*\d)[A-Z0-9][A-Z0-9\-_/]{4,})")
_VERSION = re.compile(r"\b(Amendment|Version|Edition)\s*(?:No\.?|Number)?\s*[:#]?\s*(\d+(?:\.\d+)*)\b", re.I)
# An unlabelled id in a running header: letters and digits, e.g. "GO29436", "CLEE011A2301".
_BARE_ID = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
_ACRONYM = re.compile(r"[A-Z][A-Za-z0-9]*(?:[-‐–][A-Za-z0-9]+)*(?:\s\d+[A-Za-z]?)?")
_NOT_ACRONYM = {"PROTOCOL", "CONFIDENTIAL", "CLINICAL", "STUDY", "AMENDMENT", "FINAL", "DRAFT", "SYNOPSIS",
                "TITLE", "SPONSOR", "CONTENTS", "APPENDIX", "APPENDICES", "ABBREVIATIONS", "SUMMARY"}


def looks_like_acronym(text: str) -> bool:
    """A study acronym heading ("NOVA-3", "KEYNOTE-189"), not a banner like "CLINICAL PROTOCOL"."""
    t = text.strip()
    return (len(t) <= 30 and bool(_ACRONYM.fullmatch(t)) and sum(c.isupper() for c in t) >= 2
            and t.upper() not in _NOT_ACRONYM)


def _most_frequent(counts: Counter, first_seen: list[str], min_count: int = 1) -> str | None:
    """Highest count; ties go to the candidate seen first."""
    best = max(first_seen, key=lambda c: (counts[c], -first_seen.index(c)), default=None)
    return best if best is not None and counts[best] >= min_count else None


def _candidates(texts: list[tuple[int | None, str, Any]], find) -> tuple[Counter, list[str], dict[str, tuple]]:
    """Candidate -> number of distinct (page, text-source) places it occurs; first-seen order; and where each
    candidate was first seen, (page, text, item, start, end).

    `texts` are (page, text, item); find(text) -> [(candidate, start, end)] with the span in `text`."""
    counts, order, seen, where = Counter(), [], set(), {}
    for page, text, item in texts:
        for cand, a, b in find(text):
            if (page, cand) not in seen:
                seen.add((page, cand))
                counts[cand] += 1
            if cand not in order:
                order.append(cand)
                where[cand] = (page, text, item, a, b)
    return counts, order, where


def _row_text(row: list) -> str:
    """The text of one table grid row: cells whitespace-normalised, a spanning cell (repeated along the row) once."""
    return " ".join(" ".join((c.text or "").split()) for c in {id(c): c for c in row}.values() if c.text)


def _item_source(item: Any, text: str, start: int, end: int, page: int | None) -> dict[str, Any]:
    """Where a value was read: text[start:end] of a text item (the text around it, whitespace-normalised, when
    the item is long): page, layer, text, ref (the item), refs (every item the text spans), row (tables only)."""
    layer = "furniture" if item.content_layer == ContentLayer.FURNITURE else "body"
    return {"page": page, "layer": layer, "text": " ".join(_snippet(text, start, end).split()),
            "ref": item.self_ref, "refs": [item.self_ref], "row": None}


_HISTORY_ROW = re.compile(r"(?i)^\s*(original\s+protocol|(amendment|version|edition|revision)\s*"
                          r"(?:no\.?|number)?\s*[:#]?\s*v?(\d+(?:\.\d+)*))")


def _history_version(doc: DoclingDocument, max_page: int = 15
                     ) -> tuple[tuple[str, str] | None, str | None, dict[str, Any] | None]:
    """(version, date, source) from the last row of an early document-history table, if any.

    A history table is recognised by first-column labels such as "Original
    protocol" or "Amendment 3"; the last such row is the current version.
    The source is that row: layer "table", ref = the table, row = its index in table.data.grid.
    """
    for table in doc.tables:
        if not table.prov or table.prov[0].page_no > max_page or not table.data.grid:
            continue
        rows = [(r, m, " ".join(c.text for c in row)) for r, row in enumerate(table.data.grid)
                if (m := _HISTORY_ROW.match(row[0].text))]
        if not rows or not any(m.group(1).lower().startswith("original") for _, m, _ in rows):
            continue
        r, m, row_text = rows[-1]
        version = ("Original", "protocol") if m.group(2) is None else (m.group(2).title(), m.group(3))
        date = _DATE.search(row_text)
        text = _row_text(table.data.grid[r])  # the label and the date, with the row around them when it is long
        in_row = _DATE.search(text)
        end = max(len(" ".join(m.group(0).split())), in_row.end() if in_row else 0)
        source = {"page": table.prov[0].page_no, "layer": "table", "text": " ".join(_snippet(text, 0, end).split()),
                  "ref": table.self_ref, "refs": [table.self_ref], "row": r}
        return version, date.group(0) if date else None, source
    return None, None, None


def detect_metadata(doc: DoclingDocument) -> dict[str, dict[str, Any] | None]:
    """The values doc_metadata detects from content, each with the rule that gave it and the item it came from.

    Keys: protocol_id, acronym, title, version (value ("Amendment", "4")), date; None when not found. Each is
    {"value", "method", "source"}. method: "title page" (acronym, title), "running header" (the header
    patterns on pp. 1-3), "labelled pattern" (a labelled id / version / date on the title page or in a
    header/footer), "header/footer frequency" (an unlabelled id repeated in the headers/footers) or "history
    table". source: {"page", "layer", "text", "ref", "refs", "row"}, the item (a table: its ref and the grid
    row) the value was read from, with the text around it.
    doc_metadata unwraps the values; identity.py keeps the methods and sources (no search for them later).
    """
    def found(value: Any, method: str, source: dict[str, Any] | None) -> dict[str, Any] | None:
        return {"value": value, "method": method, "source": source} if value else None

    # The running header may be split into several items ("PF-00000000 ...",
    # "Protocol C1234567", "Final Protocol Amendment 4, 5 January 2021").
    header_items: dict[str, Any] = {}  # header text -> the first item holding it
    for t in doc.texts:
        if t.label == DocItemLabel.PAGE_HEADER and t.prov and t.prov[0].page_no <= 3:
            header_items.setdefault(t.text, t)
    header = " ".join(header_items)

    def header_source(start: int, end: int) -> dict[str, Any]:
        """The header item(s) the span header[start:end] lies in (the joined header has one space between items)."""
        parts, pos = [], 0
        for text, item in header_items.items():
            if text and pos < end and pos + len(text) > start:
                parts.append((item, text, max(start - pos, 0), min(end - pos, len(text))))
            pos += len(text) + 1
        item, text, a, b = parts[0]
        src = _item_source(item, text, a, b, item.prov[0].page_no)
        if len(parts) > 1:
            src["refs"] = [p[0].self_ref for p in parts]
            src["text"] = " / ".join(" ".join(p[1].split()) for p in parts)
        return src

    def pick(texts: list[tuple[int | None, str, Any]], find, method: str, min_count: int = 1
             ) -> dict[str, Any] | None:
        """The most frequent candidate of `find` over `texts` (see _candidates), with where it was first seen."""
        counts, order, where = _candidates(texts, find)
        best = _most_frequent(counts, order, min_count)
        if best is None:
            return None
        page, text, item, a, b = where[best]
        return found(best, method, _item_source(item, text, a, b, page))

    first_page = [t for t in doc.texts if t.prov and t.prov[0].page_no == 1
                  and t.content_layer == ContentLayer.BODY]
    heads = [i for i, t in enumerate(first_page) if isinstance(t, SectionHeaderItem | TitleItem)]
    acr = next((first_page[i] for i in heads if looks_like_acronym(first_page[i].text)), None)
    # Full title: the first long text after the first heading on the title page.
    ttl = next((t for t in first_page[(heads[0] if heads else -1) + 1:] if len(t.text) > 40), None)
    acronym = found(acr.text.strip(), "title page", _item_source(acr, acr.text, 0, len(acr.text), 1)) if acr else None
    title = found(ttl.text, "title page", _item_source(ttl, ttl.text, 0, len(ttl.text), 1)) if ttl else None

    # 1. The original running-header patterns first, so those documents keep identical values.
    m_id = re.search(r"Protocol\s+([A-Z]\d{5,})", header)
    m_am = re.search(r"Amendment\s+(\d+)", header)
    m_dt = re.search(r"(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})", header)
    pid = found(m_id.group(1), "running header", header_source(*m_id.span(1))) if m_id else None
    version = found(("Amendment", m_am.group(1)), "running header", header_source(*m_am.span())) if m_am else None
    date = found(m_dt.group(1), "running header", header_source(*m_dt.span(1))) if m_dt else None

    # 2./3. Title page + every running header/footer, counted per page.
    furniture = [(t.prov[0].page_no if t.prov else None, t.text, t) for t in doc.texts
                 if t.label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER)]
    labelled = furniture + [(1, t.text, t) for t in first_page]
    if pid is None:
        pid = pick(labelled, lambda s: [(m.group(1).rstrip("-_/"), m.start(1), m.end(1))
                                        for m in _LABELLED_ID.finditer(s)], "labelled pattern")
    if pid is None:
        pages = len({p for p, _, _ in furniture})
        pid = pick(furniture, lambda s: [(m.group(0), m.start(), m.end()) for m in _BARE_ID.finditer(s)
                                         if len(m.group(0)) >= 6 and not _DATE.fullmatch(m.group(0))],
                   "header/footer frequency", min_count=max(2, round(0.3 * pages)))
    if version is None:
        version = pick(labelled, lambda s: [(f"{m.group(1).title()} {m.group(2)}", m.start(), m.end())
                                            for m in _VERSION.finditer(s)], "labelled pattern")
        if version:
            version["value"] = tuple(version["value"].split(" ", 1))
    if version is None:
        # 4. The latest row of a document-history table ("Original protocol" /
        #    "Amendment N" rows with dates): an unamended protocol has no version
        #    number anywhere else, only an "Original protocol" row.
        hist_version, hist_date, hist_source = _history_version(doc)
        version = found(hist_version, "history table", hist_source)
        date = date or found(hist_date, "history table", hist_source)
    if date is None:
        date = pick(labelled, lambda s: [(m.group(0), m.start(), m.end()) for m in _DATE.finditer(s)],
                    "labelled pattern")
    return {"protocol_id": pid, "acronym": acronym, "title": title, "version": version, "date": date}


def doc_metadata(doc: DoclingDocument, json_path: Path,
                 explicit: dict[str, str] | None = None) -> dict[str, Any]:
    """Document identity, from content: explicit values > "Protocol <id> ... Amendment <n>" running
    header (pp. 1-3) > labelled fields on the title page and in headers/footers > most frequent
    header/footer candidate (the rules and the item each value comes from: detect_metadata).

    Fields not found stay None (never printed: see doc_prefix / chunk_id_base).
    """
    detected = detect_metadata(doc)
    pid, acronym, title, version, date = (detected[k]["value"] if detected[k] else None
                                          for k in ("protocol_id", "acronym", "title", "version", "date"))

    sha = hashlib.sha256(json_path.read_bytes()).hexdigest()[:16]
    # The document key comes from the source PDF (Docling's binary_hash, recorded by convert_protocol): the same
    # PDF gets the same chunk ids on every platform and in every run. A JSON without an origin (converted before
    # it was recorded) falls back to the JSON's hash.
    pdf_hash = f"{doc.origin.binary_hash:016x}" if doc.origin is not None else None
    meta = {
        "doc_name": doc.name,
        "protocol_id": pid,
        "acronym": acronym or None,
        "title": title or None,
        "amendment": version[1] if version and version[0] == "Amendment" else None,
        "version_date": date,
        "source_json": json_path.name,
        "source_sha256": sha,
        "source_pdf_hash": pdf_hash,
        "version_label": " ".join(version) if version else None,
        "doc_key": f"doc-{(pdf_hash or sha)[:12]}",
    }
    for key, value in (explicit or {}).items():
        if key not in META_FIELDS:
            raise ValueError(f"unknown metadata field {key!r}; expected one of {META_FIELDS}")
        meta[key] = value or None
    if explicit and "amendment" in explicit and "version_label" not in explicit and meta["amendment"]:
        meta["version_label"] = f"Amendment {meta['amendment']}"
    return meta


# Registry identifiers, found by pattern anywhere in the document: body, tables, figure text and the
# furniture (running headers/footers often carry the NCT number). EU CT before EudraCT: an EU CT number
# starts like a EudraCT number.
IDENTIFIER_PATTERNS = {
    "nct": re.compile(r"\b(NCT\d{8})(?!\d)"),
    "eu_ct": re.compile(r"(?<![\d-])(\d{4}-\d{6}-\d{2}-\d{2}(?:-\d{2})?)(?![\d-])"),
    "eudract": re.compile(r"(?<![\d-])(\d{4}-\d{6}-\d{2})(?!\d|-\d)"),
    # only after an "IND" label: "IND Number: 119006", "US IND No. 140,453", "IND #: 123456"
    "ind": re.compile(r"\bIND\b[ \t]*(?:(?i:No\.?|Number|Num\.?|#)[ \t]*)?[:#]?[ \t]*(\d{1,3}(?:,\d{3})+|\d{5,6})(?![\d,])"),
    "who_utn": re.compile(r"\b(U\d{4}-\d{4}-\d{4})\b"),
}
# A label just before a value marks it as this study's identifier wherever it stands.
IDENTIFIER_LABELS = {
    "nct": re.compile(r"(?i)clinicaltrials\.gov|\bNCT\s*(?:#|no\.?|number|id\b)|\bregistr|\bidentifier"),
    "eu_ct": re.compile(r"(?i)\bEU\s*CT\b|\bEU trial\b|\bCTIS\b"),
    "eudract": re.compile(r"(?i)eudract"),
    "ind": re.compile(r"\bIND\b"),
    "who_utn": re.compile(r"(?i)\bUTN\b|universal trial"),
    "sponsor_protocol_id": re.compile(r"(?i)\bprotocol\b"),
}
IDENTIFIER_EARLY_PAGES = 3  # title pages: a value there (or in a header/footer) is the study's own
_SPONSOR_ID = re.compile(r"\bProtocol\s*(?i:No\.?|Number|ID|Identifier|Code)\s*[:#]?\s*"
                         r"((?=[A-Z0-9\-_/.]*\d)[A-Z0-9][A-Z0-9\-_/.]{3,}[A-Z0-9])")
IDENTIFIER_SNIPPET = 160  # characters of source text kept around a found identifier


def _identifier_sources(doc: DoclingDocument) -> list[tuple[int, str, str, str, int | None]]:
    """(page, layer, text, ref, row) of every text item (body, furniture, figure text) and table row, by page.

    ref is the item's ref (a table row: the table's), row the index in table.data.grid (None for text items)."""
    out = []
    for t in doc.texts:
        if t.text and t.text.strip():
            layer = "furniture" if t.content_layer == ContentLayer.FURNITURE else "body"
            out.append((t.prov[0].page_no if t.prov else 10 ** 6, layer, " ".join(t.text.split()), t.self_ref, None))
    for table in doc.tables:
        page = table.prov[0].page_no if table.prov else 10 ** 6
        for r, row in enumerate(table.data.grid):
            text = _row_text(row)
            if text.strip():
                out.append((page, "table", text, table.self_ref, r))
    out.sort(key=lambda x: x[0])  # stable: document order within a page
    return out


def _snippet(text: str, start: int, end: int) -> str:
    if len(text) <= IDENTIFIER_SNIPPET:
        return text
    pad = max(0, (IDENTIFIER_SNIPPET - (end - start)) // 2)
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def registry_identifiers(doc: DoclingDocument, meta: dict[str, Any] | None = None, *,
                         with_refs: bool = False) -> dict[str, Any]:
    """Study identifiers found by pattern anywhere in the document, including headers/footers.

    A value counts as the study's own when it stands in a header/footer, on the first pages or right
    after its label ("EudraCT:", "NCT Number:"); a value only cited in running text (another trial's
    NCT number on page 26) is listed under "others" instead. Per kind the own value seen on the most
    pages wins (ties: first seen), with its verbatim source text (page, layer, the item or table-row
    text around it) for provenance. sponsor_protocol_id is doc_metadata's protocol_id, else a labelled
    "Protocol Number: X".
    With with_refs each source also names the item it was taken from: "ref" (a table row: the table's ref) and
    "row" (its index in table.data.grid, else None); the rest of the result is the same.
    """
    sources = _identifier_sources(doc)
    out: dict[str, Any] = {"sponsor_protocol_id": None, **{k: None for k in IDENTIFIER_PATTERNS},
                           "sources": {}, "others": {}}

    def own(kind: str, page: int, layer: str, text: str, start: int) -> bool:
        label = IDENTIFIER_LABELS.get(kind)
        return (layer == "furniture" or page <= IDENTIFIER_EARLY_PAGES
                or bool(label and label.search(text[max(0, start - 40):start])))

    def source(page: int, layer: str, text: str, a: int, b: int, ref: str, row: int | None) -> dict[str, Any]:
        src = {"page": page if page < 10 ** 6 else None, "layer": layer, "text": _snippet(text, a, b)}
        if with_refs:
            src.update(ref=ref, row=row)
        return src

    def pick(kind: str, finder) -> None:
        counts, first, where, seen = Counter(), [], {}, []
        for page, layer, text, ref, row in sources:
            for m in finder(text):
                value = m.group(1)
                if value not in seen:
                    seen.append(value)
                if not own(kind, page, layer, text, m.start(1)):
                    continue
                if (page, value) not in where:
                    counts[value] += 1
                    where[(page, value)] = (page, layer, text, m.start(1), m.end(1), ref, row)
                if value not in first:
                    first.append(value)
        best = _most_frequent(counts, first)
        if best is not None:
            out[kind] = best
            out["sources"][kind] = source(*min((v for (p, val), v in where.items() if val == best),
                                               key=lambda v: v[0]))
        if [v for v in seen if v != best]:
            out["others"][kind] = [v for v in seen if v != best]

    for kind, rx in IDENTIFIER_PATTERNS.items():
        pick(kind, rx.finditer)
    pid = (meta or {}).get("protocol_id")
    if pid:
        # The id as doc_metadata found it; its source is the first text holding it, a labelled one first.
        rx = re.compile(rf"(?<![A-Za-z0-9])({re.escape(pid)})(?![A-Za-z0-9])")
        hits = [(page, layer, text, ref, row, m) for page, layer, text, ref, row in sources
                for m in [rx.search(text)] if m]
        hits.sort(key=lambda h: (not re.search(r"(?i)\bprotocol\b", h[2]), h[0]))
        out["sponsor_protocol_id"] = pid
        if hits:
            page, layer, text, ref, row, m = hits[0]
            out["sources"]["sponsor_protocol_id"] = source(page, layer, text, m.start(1), m.end(1), ref, row)
    else:
        pick("sponsor_protocol_id", _SPONSOR_ID.finditer)
    if not out["others"]:
        del out["others"]
    return out


def doc_prefix(meta: dict[str, Any]) -> str:
    """Embedding-text prefix from the fields that were found ("None" is never written): "<ACRONYM> protocol <id>
    (<version label>, <date>)", or "Protocol <id> (...)" without an acronym. Never the file name: the same PDF gets
    the same embedding texts and chunk boundaries (the prefix counts against the token budget) whatever its Docling
    JSON is called (the service names every result protocol.json)."""
    prefix = f"{meta['acronym']} protocol" if meta["acronym"] else "Protocol"
    if meta["protocol_id"]:
        prefix += f" {meta['protocol_id']}"
    extra = [v for v in (meta["version_label"], meta["version_date"]) if v]
    return prefix + (f" ({', '.join(extra)})" if extra else "")


def chunk_id_base(meta: dict[str, Any]) -> str:
    """"C1234567-A4" when id and amendment are known (keeps existing ids and caches valid),
    else the document key from the source PDF's hash, so ids never collide across documents or read "None"."""
    if meta["protocol_id"] and meta["amendment"]:
        return f"{meta['protocol_id']}-A{meta['amendment']}"
    return meta["doc_key"]


FRONT_MATTER = "Front matter"  # section name of chunks above the first heading (retrieve.py shows the same)


def front_matter_refs(doc: DoclingDocument) -> set[str]:
    """Body items that come before the first title or section heading in reading order."""
    refs = set()
    for it, _ in doc.iterate_items(with_groups=False, included_content_layers={ContentLayer.BODY}):
        if isinstance(it, SectionHeaderItem | TitleItem):
            break
        refs.add(it.self_ref)
    return refs


def accepted_section_numbers(doc: DoclingDocument) -> dict[str, str]:
    """Heading self_ref -> section number, for numbers the heading-level step accepted.

    Uses protocol_postprocess.section_numbers when available; the fallback is
    the same rule as fix_heading_levels (a lower top-level number, e.g.
    "2. HBV Monitoring" inside Appendix 10.12.1, is a local list heading).
    """
    try:  # longest valid number chain, Roman / "Section N" labels, outline level offset (structure.py)
        from structure import section_numbers

        return section_numbers(doc)
    except ImportError:
        pass
    import protocol_postprocess

    if hasattr(protocol_postprocess, "section_numbers"):
        return protocol_postprocess.section_numbers(doc)
    out, top = {}, 0
    for item, _ in doc.iterate_items():
        if isinstance(item, SectionHeaderItem) and (m := _SECTION_NUM.match(item.text.strip())):
            if (n := int(m.group(1).split(".")[0])) >= top:
                top, out[item.self_ref] = n, m.group(1)
    return out


def heading_paths(doc: DoclingDocument, serializer: ChunkingDocSerializer) -> dict[str, list[DocItem]]:
    """Item self_ref -> the heading items in scope, tracked exactly as HierarchicalChunker does."""
    by_level: dict[int, DocItem] = {}
    paths = {}
    excluded = serializer.get_excluded_refs()
    for item, _ in doc.iterate_items(with_groups=True, traverse_pictures=serializer.params.traverse_pictures,
                                     included_content_layers=serializer.params.layers):
        if item.self_ref in excluded:
            continue
        if isinstance(item, TitleItem | SectionHeaderItem):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            for k in [k for k in by_level if k >= level]:
                del by_level[k]
            by_level[level] = item
        else:
            paths[item.self_ref] = [by_level[k] for k in sorted(by_level)]
    return paths


def section_of(headings: list[str] | None, path: list[DocItem] | None = None,
               accepted: dict[str, str] | None = None) -> str | None:
    """Section number of the deepest numbered heading in scope.

    With the heading items (path) and the accepted numbers, only accepted
    numbers count; otherwise any numbered heading text does (legacy).
    """
    if path is not None and accepted is not None and [h.text for h in path] == list(headings or []):
        return next((accepted[h.self_ref] for h in reversed(path) if h.self_ref in accepted), None)
    for h in reversed(headings or []):
        if m := _SECTION_NUM.match(h.strip()):
            return m.group(1)
    return None


def chunk_type(items: list[DocItem], glossary_refs: set[str]) -> str:
    kinds = set()
    for it in items:
        if isinstance(it, TableItem):
            kinds.add("glossary" if it.self_ref in glossary_refs else "table")
        elif isinstance(it, PictureItem):
            kinds.add("figure")
        elif it.label == DocItemLabel.LIST_ITEM:
            kinds.add("list")
        else:
            kinds.add("text")
    return kinds.pop() if len(kinds) == 1 else "mixed:" + "+".join(sorted(kinds))


# --------------------------------------------------------------------------- quality checks

def quality_checks(doc: DoclingDocument, records: list[dict],
                   serializer: ProtocolDocSerializer, max_tokens: int) -> dict[str, Any]:
    excluded = serializer.get_excluded_refs()
    owned = set()  # captions/footnotes/figure children are covered by their owner
    for it in [*doc.tables, *doc.pictures]:
        owned |= {r.cref for r in it.captions} | {r.cref for r in it.footnotes}
    for pic in doc.pictures:
        owned |= {r.cref for r in pic.children}
    seen = Counter(ref for r in records for ref in r["doc_item_refs"])
    heads_used = {h for r in records for h in (r["headings"] or [])}

    missing, dupes, orphan_heads = [], [], []
    for it, _ in doc.iterate_items(with_groups=False, included_content_layers={ContentLayer.BODY}):
        if it.self_ref in excluded or it.self_ref in owned:
            continue
        if isinstance(it, SectionHeaderItem | TitleItem):
            if it.text not in heads_used:
                orphan_heads.append(it.text[:80])
            continue
        if isinstance(it, PictureItem):
            if not seen[it.self_ref]:
                if not dropped_figure(it, doc, figure_parts(it, doc)):
                    missing.append(it.self_ref)
            continue
        if isinstance(it, TextItem) and not it.text.strip():
            continue
        n = seen[it.self_ref]
        if n == 0:
            missing.append(f"{it.self_ref} p{it.prov[0].page_no if it.prov else '?'}: {getattr(it, 'text', '')[:60]}")
        elif n > 1 and not isinstance(it, TableItem):
            dupes.append(it.self_ref)

    # Mid-row splits: a chunk line that is only a fragment of a full table row.
    # "Label: ..." / "Label (cont.): ..." fragments are deliberate sentence-level splits
    # of rows too long for one chunk (row label repeated); anything else is a defect.
    ts = serializer.table_serializer
    full_rows = {}
    for t in doc.tables:
        if t.self_ref not in excluded:
            _, rows = ts.split_prefix(serializer.serialize(item=t).text, t, doc, serializer)
            full_rows[t.self_ref] = [r.rstrip("\n") for r in rows]
    labeled, unlabeled = set(), set()
    for rec in records:
        for ref in rec["doc_item_refs"]:
            rows = full_rows.get(ref)
            if not rows:
                continue
            row_set = set(rows)
            for line in rec["text"].splitlines():
                if len(line) <= 15 or line in row_set:
                    continue
                label, _, rest = line.partition(": ")
                base = label.removesuffix(" (cont.)")
                if any(row_label_matches(base, r) and rest[:40] in r for r in rows):
                    labeled.add(rec["chunk_id"])
                elif any(line in r for r in rows):
                    unlabeled.add(f"{rec['chunk_id']} ({ref})")

    return {
        "empty_chunks": [r["chunk_id"] for r in records if not r["text"].strip()],
        "oversized_chunks": [(r["chunk_id"], r["n_tokens"]) for r in records if r["n_tokens"] > max_tokens],
        "chunks_without_headings": [r["chunk_id"] for r in records if not r["headings"]],
        "uncovered_items": missing,
        "duplicated_items": dupes,
        "headings_without_content": orphan_heads,
        "tables_split_mid_row": sorted(unlabeled),
        "long_rows_split_with_label": sorted(labeled),
    }


# --------------------------------------------------------------------------- main

def main() -> None:
    from project_paths import main_json

    parser = argparse.ArgumentParser()
    parser.add_argument("json", nargs="?", default=str(main_json()))
    parser.add_argument("--out", default="output/chunks")
    parser.add_argument("--tokenizer", default="Qwen/Qwen3-Embedding-8B",
                        help="HF tokenizer of the embedding model, or tiktoken:<encoding>")
    parser.add_argument("--max-tokens", type=int, default=512,
                        help="limit for prefix+headings+text (leave room for special tokens)")
    parser.add_argument("--table-format", choices=["rows", "markdown"], default="rows")
    parser.add_argument("--no-merge-peers", action="store_true")
    parser.add_argument("--prefix", help="document prefix for embedding text (default: derived)")
    parser.add_argument("--meta", action="append", default=[], metavar="FIELD=VALUE",
                        help=f"explicit document metadata, overrides detection; FIELD in {', '.join(META_FIELDS)}")
    parser.add_argument("--quiet-text", action="store_true",
                        help="log counts only: no titles, metadata values, headings or item text (confidential documents)")
    parser.add_argument("--parent-min", type=int, default=300, help="parents: merge siblings below this (tokens)")
    parser.add_argument("--parent-max", type=int, default=1500, help="parents: section/window limit (tokens)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("transformers").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("docling_core.types.doc.items.picture.picture").setLevel(logging.WARNING)  # "sequence longer than max" noise
    chunk_document(Path(args.json), Path(args.out), tokenizer=args.tokenizer, max_tokens=args.max_tokens,
                   table_format=args.table_format, merge_peers=not args.no_merge_peers, prefix=args.prefix,
                   explicit_meta=dict(kv.split("=", 1) for kv in args.meta), quiet_text=args.quiet_text,
                   parent_min=args.parent_min, parent_max=args.parent_max)


def _relative_image_path(uri: str | None, root: Path) -> str | None:
    """A figure's image as a posix path relative to the folder of the Docling JSON ("images/image_000.png"), so
    chunk records do not depend on where the document is stored (Docling writes \\ on Windows)."""
    if not uri or uri.startswith("data:") or "://" in uri:
        return None
    path = Path(uri.replace("\\", "/"))
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return path.name
    return path.as_posix()


def chunk_document(src: Path, out: Path, *, tokenizer: str = "Qwen/Qwen3-Embedding-8B", max_tokens: int = 512,
                   table_format: str = "rows", merge_peers: bool = True, prefix: str | None = None,
                   explicit_meta: dict[str, str] | None = None, quiet_text: bool = False,
                   parent_min: int = 300, parent_max: int = 1500) -> dict:
    """Chunk one converted document (Docling JSON `src`) into `out`: chunks.jsonl, parents.jsonl,
    sections.json, tables.jsonl, glossary.json, abbreviations.json and stats.json (returned).
    quiet_text logs counts only (confidential documents)."""
    out.mkdir(parents=True, exist_ok=True)

    doc = DoclingDocument.load_from_json(src)
    meta = doc_metadata(doc, src, explicit_meta or {})
    prefix = prefix if prefix is not None else doc_prefix(meta)
    id_base = chunk_id_base(meta)
    if quiet_text:
        log.info("Document fields found: %s", {k: bool(meta[k]) for k in META_FIELDS})
    else:
        log.info("Document: %s", meta)

    provider = ProtocolSerializerProvider(table_format)
    chunker = ProtocolChunker(
        tokenizer=build_tokenizer(tokenizer, max_tokens),
        serializer_provider=provider,
        merge_peers=merge_peers,
        doc_prefix=prefix,
    )
    chunks = list(chunker.chunk(dl_doc=doc))
    serializer = provider.get_serializer(doc)
    glossary = build_glossary(doc)
    glossary_refs = set(glossary.table_refs)
    paths = heading_paths(doc, serializer)
    accepted = accepted_section_numbers(doc)

    records = []
    table_parts: dict[str, list[int]] = defaultdict(list)
    img_root = src.parent
    front = front_matter_refs(doc)
    for i, ch in enumerate(chunks):
        if not ch.text.strip():
            continue
        if not ch.meta.headings and ch.meta.doc_items and all(it.self_ref in front for it in ch.meta.doc_items):
            ch.meta.headings = [FRONT_MATTER]  # text above the first heading (e.g. a sponsor line)
        # Resolve against the document: chunk metadata may hold plain DocItem copies.
        items = [it.get_ref().resolve(doc) for it in ch.meta.doc_items]
        pages = sorted({p.page_no for it in items for p in it.prov})
        embed = chunker.contextualize(ch)
        rec = {
            "chunk_id": f"{id_base}-{len(records):04d}",
            "content_hash": hashlib.sha1(embed.encode()).hexdigest()[:12],
            "protocol_id": meta["protocol_id"],
            "amendment": meta["amendment"],
            "version_date": meta["version_date"],
            "source_sha256": meta["source_sha256"],
            "doc_key": meta["doc_key"],
            "section_number": section_of(ch.meta.headings, paths.get(items[0].self_ref) if items else None,
                                         accepted),
            "section_title": ch.meta.headings[-1] if ch.meta.headings else None,
            "headings": ch.meta.headings,
            "page_start": pages[0] if pages else None,
            "page_end": pages[-1] if pages else None,
            "pages": pages,
            "chunk_type": chunk_type(items, glossary_refs),
            "element_types": sorted({it.label.value for it in items}),
            "doc_item_refs": [it.self_ref for it in items],
            "n_tokens": chunker.tokenizer.count_tokens(embed),
            "text": ch.text,
            "embed_text": embed,
        }
        tables = [it for it in items if isinstance(it, TableItem)]
        if tables:
            rec["tables"] = [{"table_id": t.self_ref, "caption": t.caption_text(doc) or None}
                             for t in tables]
            for t in tables:
                table_parts[t.self_ref].append(len(records))
        figures = [it for it in items if isinstance(it, PictureItem)]
        if figures:
            rec["figures"] = []
            for p in figures:
                f = figure_parts(p, doc)
                uri = str(p.image.uri) if p.image and p.image.uri else None
                rec["figures"].append({
                    "figure_id": p.self_ref, "class": f["class"], "caption": f["caption"] or None,
                    "image_path": _relative_image_path(uri, img_root),
                    "has_description": bool(f["description"]), "has_mermaid": bool(f["mermaid"]),
                })
        records.append(rec)
    for ref, idxs in table_parts.items():
        for part, idx in enumerate(idxs, start=1):
            for t in records[idx]["tables"]:
                if t["table_id"] == ref:
                    t.update(part=part, parts=len(idxs))

    # Section tree and parents (parents.py): section_id / role / parent_id are metadata only; chunk
    # text, embed_text, content_hash and ids are unchanged, so embeddings and caches stay valid.
    import parents as parents_mod

    index = parents_mod.section_index(doc)
    store = parents_mod.ParentStore(doc, index, records, chunker.tokenizer, parent_min, parent_max,
                                    serializer=serializer)
    store.annotate(records)
    parent_stats = store.stats()
    (out / "sections.json").write_text(json.dumps(index.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    with open(out / "parents.jsonl", "w", encoding="utf-8") as fh:
        for p in store.parents:
            fh.write(json.dumps(p.to_json(), ensure_ascii=False) + "\n")
    meta["identifiers"] = registry_identifiers(doc, meta)

    with open(out / "chunks.jsonl", "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Full tables for small-to-big display (a retrieved row-group -> whole table).
    heading_of: dict[str, list[str] | None] = {}
    for rec in records:
        for t in rec.get("tables", []):
            heading_of.setdefault(t["table_id"], rec["headings"])
    # Page-split fragments left unmerged because their column layout differs per page
    # (table_merge.continuation_chains): the retriever expands the whole chain.
    from table_merge import continuation_chains

    chain_of = {}
    for chain in continuation_chains(doc):
        chain = [r for r in chain if r in heading_of]
        if len(chain) > 1:
            chain_of.update({r: chain for r in chain})
    with open(out / "tables.jsonl", "w", encoding="utf-8") as fh:
        for t in doc.tables:
            if t.self_ref not in heading_of:
                continue
            fh.write(json.dumps({
                "table_id": t.self_ref, "caption": t.caption_text(doc) or None,
                "chain": chain_of.get(t.self_ref),
                "headings": heading_of[t.self_ref],
                "pages": sorted({p.page_no for p in t.prov}),
                "n_rows": t.data.num_rows, "n_cols": t.data.num_cols,
                "markdown": ProtocolTableSerializer("markdown").serialize(
                    item=t, doc_serializer=serializer, doc=doc).text,
            }, ensure_ascii=False) + "\n")

    (out / "abbreviations.json").write_text(json.dumps(glossary.definitions(), indent=1, ensure_ascii=False),
                                            encoding="utf-8")
    (out / "glossary.json").write_text(json.dumps(glossary.to_json(), indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    log.info("Glossary: %d abbreviations %s from tables %s; %d with alternative definitions",
             len(glossary.entries), dict(Counter(e.source for e in glossary.entries.values())),
             glossary.table_refs, sum(bool(e.alternatives) for e in glossary.entries.values()))

    # ---- stats + checks
    toks = sorted(r["n_tokens"] for r in records)
    pct = lambda q: toks[min(len(toks) - 1, int(q * len(toks)))]  # noqa: E731
    bins = Counter(min(t // 64, 8) for t in toks)
    checks = quality_checks(doc, records, serializer, max_tokens)
    stats = {
        "settings": {"tokenizer": tokenizer, "max_tokens": max_tokens,
                     "table_format": table_format, "merge_peers": merge_peers,
                     "prefix": prefix},
        "document": meta,
        "chunks": len(records),
        "tokens": {"min": toks[0], "p10": pct(0.1), "median": int(statistics.median(toks)),
                   "p90": pct(0.9), "max": toks[-1], "mean": round(statistics.mean(toks), 1),
                   "total": sum(toks)},
        "token_histogram": {f"{b * 64}-{b * 64 + 63}" if b < 8 else "512+": bins[b]
                            for b in sorted(bins)},
        "chunk_types": dict(Counter(r["chunk_type"] for r in records).most_common()),
        "chunks_containing_label": dict(Counter(l for r in records for l in r["element_types"]).most_common()),
        "largest": [(r["chunk_id"], r["n_tokens"], r["chunk_type"], r["section_title"])
                    for r in sorted(records, key=lambda r: -r["n_tokens"])[:10]],
        "checks": checks,
        "parents": parent_stats,
    }
    (out / "stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")

    log.info("Chunks: %d | tokens min/p10/median/p90/max: %d/%d/%d/%d/%d (mean %.0f)",
             len(records), toks[0], pct(0.1), stats["tokens"]["median"], pct(0.9), toks[-1],
             stats["tokens"]["mean"])
    log.info("Token histogram: %s", stats["token_histogram"])
    log.info("Chunk types: %s", stats["chunk_types"])
    log.info("Chunks containing label: %s", stats["chunks_containing_label"])
    for cid, n, kind, sec in stats["largest"]:
        if quiet_text:  # chunk ids may contain the protocol id
            log.info("  largest: %4d tokens  %s", n, kind)
        else:
            log.info("  largest: %s %4d tokens  %-8s %s", cid, n, kind, (sec or "")[:60])
    for name, val in checks.items():
        (log.warning if val and name not in INFO_CHECKS else log.info)(
            "Check %s: %d %s", name, len(val), "" if quiet_text or not val else val[:8])
    log.info("Parents (%s index): %d %s | tokens median/p90/max %d/%d/%d | under min %d, non-table over max %d",
             parent_stats["index"], parent_stats["parents"], parent_stats["kinds"], parent_stats["tokens"]["median"],
             parent_stats["tokens"]["p90"], parent_stats["tokens"]["max"], parent_stats["under_min"],
             parent_stats["over_max_non_table"])
    (log.warning if parent_stats["chunks_mapped"] != len(records) or parent_stats["chunks_in_several_parents"]
     else log.info)("Parents: chunks mapped %d/%d, in several parents %d, without a unit %d",
                    parent_stats["chunks_mapped"], len(records), parent_stats["chunks_in_several_parents"],
                    parent_stats["chunks_without_unit"])
    ids = meta["identifiers"]
    if quiet_text:
        log.info("Identifiers found: %s", {k: bool(ids.get(k)) for k in ("sponsor_protocol_id", *IDENTIFIER_PATTERNS)})
    else:
        log.info("Identifiers: %s", {k: ids.get(k) for k in ("sponsor_protocol_id", *IDENTIFIER_PATTERNS)})
    log.info("Outputs written to %s", out.resolve())
    return stats


if __name__ == "__main__":
    main()
