"""Moved to the project root as keyterm_match.py (shared with eval_v1.py); kept so the gold scripts' imports work."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from keyterm_match import alt_in, alt_regex, compact, key_terms_match, missing_groups, normalize  # noqa: E402,F401
