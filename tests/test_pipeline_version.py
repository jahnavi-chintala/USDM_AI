"""Self-test for pipeline_version.py: `python tests/test_pipeline_version.py` (no pytest, offline)."""

import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline_version as pv  # noqa: E402
from protocol_settings import ProtocolSettings  # noqa: E402


def test_semver_and_inputs() -> None:
    parts = pv.PIPELINE_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), pv.PIPELINE_VERSION
    inputs = pv.fingerprint_inputs()
    json.dumps(inputs)  # serialisable as is
    assert inputs["pipeline_version"] == pv.PIPELINE_VERSION
    assert inputs["packages"]["docling"], inputs["packages"]
    assert set(inputs["models"]) == {"figure", "identity", "embedding", "reranker", "tokenizer"}
    assert inputs["settings"] == json.loads(json.dumps(inputs["settings"]))


def test_identity_model_in_the_fingerprint() -> None:
    """IDENTITY_MODEL: unset -> the default model, stripped, "" -> off; each model is another fingerprint."""
    import os

    old = os.environ.pop("IDENTITY_MODEL", None)
    try:
        assert pv.identity_model() == pv.DEFAULT_IDENTITY_MODEL == "openrouter:anthropic/claude-opus-5.5"
        base = pv.pipeline_fingerprint()
        os.environ["IDENTITY_MODEL"] = "  openrouter:x/y "
        assert pv.identity_model() == "openrouter:x/y"
        assert pv.fingerprint_inputs()["models"]["identity"] == "openrouter:x/y"
        assert pv.pipeline_fingerprint() != base
        os.environ["IDENTITY_MODEL"] = ""
        assert pv.identity_model() == "" and pv.pipeline_fingerprint() != base
        os.environ["IDENTITY_MODEL"] = pv.DEFAULT_IDENTITY_MODEL
        assert pv.pipeline_fingerprint() == base
    finally:
        os.environ.pop("IDENTITY_MODEL", None)
        if old is not None:
            os.environ["IDENTITY_MODEL"] = old


def test_fingerprint_stable_and_sensitive() -> None:
    a = pv.pipeline_fingerprint()
    assert a == pv.pipeline_fingerprint() == pv.pipeline_fingerprint(ProtocolSettings())
    assert len(a) == 16 and int(a, 16) >= 0
    changed = pv.pipeline_fingerprint(replace(ProtocolSettings(), reading_order_repair=False))
    assert changed != a
    old = pv.PIPELINE_VERSION
    try:
        pv.PIPELINE_VERSION = "999.0.0"
        assert pv.pipeline_fingerprint() != a
    finally:
        pv.PIPELINE_VERSION = old
    assert pv.pipeline_fingerprint() == a


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} tests passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
