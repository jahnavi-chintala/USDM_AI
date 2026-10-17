"""Register an already-converted output folder in the service, without reprocessing it.

  python -m service.import_existing LZZT_CDISC_Pilot                 (a folder name under output_public/)
  python -m service.import_existing <folder> [--pdf <source.pdf>] [--assume-current] [--replace]

The source PDF defaults to public_protocols/<folder name>/<document.json pdf_name or protocol.pdf>, or
<folder>/source.pdf. doc_id is the SHA-256 of its bytes (checked against document.json when present). The
outputs are copied into documents/<doc_id>/ in the service layout: source.pdf, protocol.json/.md/.html/
.decisions.json (renamed from <stem>.*), quality.json, the image folder(s) protocol.json refers to, tables/,
chunks/ and convert.log (as logs/convert.log); then the folder is published atomically and the document
recorded as done. Its pipeline_version is "imported" (so the result shows as stale and can be reprocessed)
unless --assume-current records the running PIPELINE_VERSION and pipeline_fingerprint().

The main protocol's output (output/), anything under output_tests/ or test_protocols/ and the main PDF are
refused without --allow-private; the check is on the path alone, before anything there is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from service.config import ROOT, ServiceConfig
from service.contracts import PipelineResult
from service.pipeline import document_identifiers
from service.registry import (IMPORTED_VERSION, DocumentBusy, Registry, inspect_pdf_in_process)
from service.storage import LocalStorage, SOURCE_NAME, Storage, check_doc_id, new_job_id, utcnow

PRIVATE_DIRS = ("output", "output_tests", "test_protocols")
MAIN_SUFFIXES = (".json", ".md", ".html", ".decisions.json")


class ImportRefused(Exception):
    pass


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_private(path: Path | str) -> bool:
    """Lexical check (nothing is opened): inside output/, output_tests/, test_protocols/ or the main PDF."""
    p = _norm(path)
    for name in PRIVATE_DIRS:
        base = _norm(ROOT / name)
        if p == base or p.startswith(base + os.sep):
            return True
    try:
        import project_paths

        return p == _norm(project_paths.main_pdf())
    except Exception:  # noqa: BLE001 - no main protocol configured
        return False


def _check_allowed(path: Path, allow_private: bool) -> None:
    if allow_private:
        return
    if is_private(path):
        raise ImportRefused(f"{path} is private (the main protocol or a test protocol): pass --allow-private")
    if path.exists() and is_private(path.resolve()):  # a link into a private folder
        raise ImportRefused(f"{path} leads into a private folder: pass --allow-private")


def resolve_folder(arg: str | Path) -> Path:
    """A folder name under output_public/, or a path."""
    path = Path(arg)
    if not path.is_absolute() and len(path.parts) == 1:
        candidate = ROOT / "output_public" / path
        if not is_private(candidate) and candidate.is_dir():
            return candidate
    return path if path.is_absolute() else Path.cwd() / path


def _document_json(folder: Path) -> dict:
    try:
        return json.loads((folder / "document.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_pdf(folder: Path, pdf: Path | None, allow_private: bool) -> Path:
    if pdf is None:
        name = _document_json(folder).get("pdf_name") or "protocol.pdf"
        candidates = [ROOT / "public_protocols" / folder.name / PurePosixPath(name).name, folder / SOURCE_NAME]
        pdf = next((c for c in candidates if not is_private(c) and c.is_file()), None)
        if pdf is None:
            raise ImportRefused(f"no source PDF found for {folder.name}: pass --pdf")
    _check_allowed(pdf, allow_private)
    if not pdf.is_file():
        raise ImportRefused(f"{pdf} is not a file")
    return pdf


def detect_stem(folder: Path, pdf: Path) -> str:
    for stem in dict.fromkeys((pdf.stem, "protocol")):
        if (folder / f"{stem}.json").is_file():
            return stem
    raise ImportRefused(f"no converted <stem>.json in {folder.name}")


def referenced_image_dirs(json_path: Path) -> list[str]:
    """Relative folders of the image URIs in a Docling JSON (written with \\ on Windows, / elsewhere)."""
    stack, dirs = [json.loads(json_path.read_text(encoding="utf-8"))], set()
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            uri = node.get("uri")
            if isinstance(uri, str) and uri and not uri.startswith("data:") and "://" not in uri:
                parts = PureWindowsPath(uri).parts if "\\" in uri else PurePosixPath(uri).parts
                if len(parts) == 2 and parts[0] not in ("", ".", "..") and ":" not in parts[0]:
                    dirs.add(parts[0])
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return sorted(dirs)


def _copy_outputs(folder: Path, stem: str, work: Path) -> list[str]:
    copied = []
    for suffix in MAIN_SUFFIXES:
        src = folder / f"{stem}{suffix}"
        if src.is_file():
            shutil.copy2(src, work / f"protocol{suffix}")
            copied.append(f"protocol{suffix}")
    if (folder / "quality.json").is_file():
        shutil.copy2(folder / "quality.json", work / "quality.json")
        copied.append("quality.json")
    image_dirs = referenced_image_dirs(folder / f"{stem}.json") or [f"{stem}_images"]
    for name in [*image_dirs, "tables", "chunks"]:
        src = folder / name
        if src.is_dir() and src.resolve().is_relative_to(folder.resolve()):
            shutil.copytree(src, work / name, symlinks=False)
            copied.append(f"{name}/")
    if (folder / "convert.log").is_file():
        (work / "logs").mkdir(exist_ok=True)
        shutil.copy2(folder / "convert.log", work / "logs" / "convert.log")
        copied.append("logs/convert.log")
    return copied


def import_folder(config: ServiceConfig, folder: Path | str, *, pdf: Path | str | None = None,
                  assume_current: bool = False, allow_private: bool = False, replace: bool = False,
                  version: tuple[str, str] | None = None, registry: Registry | None = None,
                  storage: Storage | None = None) -> dict:
    """Import one converted folder; returns {doc_id, job_id, status, pipeline_version, files, warnings}.
    version=(pipeline_version, fingerprint) overrides --assume-current / "imported" (tests)."""
    folder = Path(folder)
    _check_allowed(folder, allow_private)
    if not folder.is_dir():
        raise ImportRefused(f"{folder} is not a folder")
    pdf_path = find_pdf(folder, Path(pdf) if pdf else None, allow_private)
    doc_id = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    expected = _document_json(folder).get("pdf_sha256")
    if expected and expected != doc_id:
        raise ImportRefused("document.json names another PDF (pdf_sha256 differs)")
    check_doc_id(doc_id)
    stem = detect_stem(folder, pdf_path)

    if version is None:
        if assume_current:
            import pipeline_version as pv

            version = (pv.PIPELINE_VERSION, pv.pipeline_fingerprint(None, config.models()))  # as the pipeline
        else:
            version = (IMPORTED_VERSION, "")

    own_registry = registry is None
    storage = storage or LocalStorage(config)
    registry = registry or Registry(config)
    job_id = new_job_id()
    try:
        existing = registry.get_document(doc_id)
        if existing and existing["has_result"] and not replace:
            raise ImportRefused(f"{doc_id} already has a result: pass --replace to overwrite it")
        info = inspect_pdf_in_process(pdf_path)
        work = storage.create_workdir(job_id)
        try:
            shutil.copy2(pdf_path, work / SOURCE_NAME)
            files = [SOURCE_NAME, *_copy_outputs(folder, stem, work)]
            warnings = registry.register_import(doc_id, job_id, filename=pdf_path.name, size=pdf_path.stat().st_size,
                                                pages=info.get("pages"), text_fp=info.get("text_fingerprint"))
        except BaseException:
            storage.remove_workdir(job_id)
            raise
        try:
            quality = (json.loads((work / "quality.json").read_text(encoding="utf-8")) or {}).get("summary") or {}
        except (OSError, ValueError, AttributeError):
            quality = {}
        result = PipelineResult(status="done", pipeline_version=version[0], fingerprint=version[1], sha256=doc_id,
                                pages=info.get("pages") or 0, identifiers=document_identifiers(work),
                                quality=quality, warnings=[])
        try:
            storage.write_publish_record(job_id, {"job_id": job_id, "doc_id": doc_id, "published_at": utcnow(),
                                                  "result": result.to_json(), "imported_from": folder.name})
            storage.publish(job_id, doc_id)
        except BaseException:
            registry.record_failure(job_id, "ImportFailed", None)
            raise
        job = registry.record_success(job_id, result)
        return {"doc_id": doc_id, "job_id": job_id, "status": job["state"], "pipeline_version": version[0],
                "files": files, "warnings": warnings}
    finally:
        if own_registry:
            registry.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="folder name under output_public/, or a path")
    parser.add_argument("--pdf", type=Path, help="source PDF (default: public_protocols/<name>/protocol.pdf)")
    parser.add_argument("--assume-current", action="store_true",
                        help="record the running pipeline version + fingerprint instead of 'imported'")
    parser.add_argument("--allow-private", action="store_true",
                        help="allow output/, output_tests/, test_protocols/ and the main PDF")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing result of the same PDF")
    parser.add_argument("--data-dir", type=Path, help="DATA_DIR (default: from the environment)")
    args = parser.parse_args(argv)

    config = ServiceConfig.from_env()
    if args.data_dir:
        config = ServiceConfig(**{**config.__dict__, "data_dir": args.data_dir.resolve()})
    try:
        _check_allowed(Path(args.folder), args.allow_private)
        out = import_folder(config, resolve_folder(args.folder), pdf=args.pdf, assume_current=args.assume_current,
                            allow_private=args.allow_private, replace=args.replace)
    except (ImportRefused, DocumentBusy) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
