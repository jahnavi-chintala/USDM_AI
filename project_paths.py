"""Where the main protocol lives on this machine.

The main protocol's file name is local configuration, not code: set the environment variable
MAIN_PROTOCOL_PDF, or write {"main_pdf": "<file name or path>"} to local_config.json in the project
root (untracked, like every document and output). Relative paths are taken from the project root.
Without either, the placeholder "main_protocol.pdf" is used, so the scripts fail with a missing file
rather than at import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL_CONFIG = ROOT / "local_config.json"
PLACEHOLDER = "main_protocol.pdf"


def _local() -> dict:
    try:
        return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main_pdf() -> Path:
    """The main protocol PDF."""
    value = os.environ.get("MAIN_PROTOCOL_PDF") or _local().get("main_pdf") or PLACEHOLDER
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main_json() -> Path:
    """Its converted Docling JSON (convert_protocol.py writes output/<pdf stem>.json)."""
    return ROOT / "output" / f"{main_pdf().stem}.json"
