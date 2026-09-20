"""Bound this tool's own disk growth. Stdlib only; weekly timer.

`data/raw/*.jsonl` is an append-only audit/reprocess copy of every poll — the parsed
data already lives in usage.db, so old days can be gzipped (~10x) and the very old ones
dropped. The current day is never touched (it's still being appended to). The systemd
journal is capped separately (see README — needs sudo).
"""
import gzip
import shutil
import time
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
KEEP_UNCOMPRESSED_DAYS = 7     # last week stays plain for easy inspection
KEEP_DAYS = 90                 # older than this is dropped (DB still has it)


def _age_days(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / 86400


def run(verbose: bool = True) -> dict:
    gzipped = deleted = 0
    if RAW.exists():
        for p in sorted(RAW.iterdir()):
            if p.name.endswith(".jsonl"):
                age = _age_days(p)
                if age > KEEP_DAYS:
                    p.unlink(); deleted += 1
                elif age > KEEP_UNCOMPRESSED_DAYS:
                    with open(p, "rb") as f, gzip.open(f"{p}.gz", "wb") as g:
                        shutil.copyfileobj(f, g)
                    p.unlink(); gzipped += 1
            elif p.name.endswith(".jsonl.gz") and _age_days(p) > KEEP_DAYS:
                p.unlink(); deleted += 1
    result = {"gzipped": gzipped, "deleted": deleted}
    if verbose:
        print(f"maintenance: {result}")
    return result


if __name__ == "__main__":
    run()
