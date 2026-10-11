"""Re-run MAIN, then post-processing (from cached batches) and chunking for P1-P3 and the public protocols,
then the SoA comparison against the public USDM keys. Numbers-only output for P1-P3 (test_protocols.py).
Log: reports/eval/phase2_rerun.log."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOG = ROOT / "reports" / "eval" / "full_rerun.log"
PY = sys.executable


def run(args: list[str], fh) -> int:
    fh.write(f"\n$ {' '.join(args[1:])}\n")
    fh.flush()
    return subprocess.run(args, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode


def main() -> None:
    with open(LOG, "w", encoding="utf-8") as fh:
        if run([PY, "convert_protocol.py"], fh) == 0:  # MAIN into output/
            run([PY, "chunk_protocol.py", "--quiet-text"], fh)
        run([PY, "test_protocols.py", "convert"], fh)
        run([PY, "test_protocols.py", "report"], fh)
        run([PY, "test_protocols.py", "tables"], fh)
        for pdf in sorted((ROOT / "public_protocols").glob("*/protocol.pdf")):
            out = ROOT / "output_public" / pdf.parent.name
            rc = run([PY, "convert_protocol.py", str(pdf), "--out", str(out), "--figure-model", ""], fh)
            if rc == 0:
                run([PY, "chunk_protocol.py", str(out / "protocol.json"), "--out", str(out / "chunks"),
                     "--quiet-text"], fh)
            if (pdf.parent / "usdm_v4.json").exists() or (pdf.parent / "usdm_v4_partial.json").exists():
                run([PY, "usdm_gold.py", "soa-compare", pdf.parent.name], fh)
        fh.write("\nDONE\n")


if __name__ == "__main__":
    main()
