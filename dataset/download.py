"""Downloads the raw SNAP soc-Pokec files. Run once; the ~1.6M-node / 30.6M-edge
raw graph is far too big to load into any of the five DBs' free tiers, which is
exactly why dataset/sample.py exists — this script only fetches the source data
sample.py samples from.

Usage: python -m dataset.download
Writes to dataset/raw/ (gitignored — multi-hundred-MB files don't belong in git;
the *sampled* dataset that sample.py produces does get committed, since that's
the actual frozen benchmark input and is small).
"""
import gzip
import shutil
import sys
import urllib.request
from pathlib import Path

RAW_DIR = Path(__file__).parent / "raw"

FILES = {
    "soc-pokec-relationships.txt.gz": "https://snap.stanford.edu/data/soc-pokec-relationships.txt.gz",
    "soc-pokec-profiles.txt.gz": "https://snap.stanford.edu/data/soc-pokec-profiles.txt.gz",
}


def download_and_extract():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for fname, url in FILES.items():
        gz_path = RAW_DIR / fname
        txt_path = RAW_DIR / fname.replace(".gz", "")
        if txt_path.exists():
            print(f"[skip] {txt_path.name} already extracted")
            continue
        if not gz_path.exists():
            print(f"[download] {url}")
            urllib.request.urlretrieve(url, gz_path)
        print(f"[extract] {gz_path.name} -> {txt_path.name}")
        with gzip.open(gz_path, "rb") as f_in, open(txt_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


if __name__ == "__main__":
    try:
        download_and_extract()
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        print(
            "If snap.stanford.edu is unreachable from this network, download "
            "soc-pokec-relationships.txt.gz and soc-pokec-profiles.txt.gz manually "
            "and place them under dataset/raw/.",
            file=sys.stderr,
        )
        sys.exit(1)
