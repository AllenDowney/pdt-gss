#!/usr/bin/env python3
"""Fetch or check the files this repo is built from, as sources.yaml lists them.

    python scripts/download.py --check            # what is present; no network
    python scripts/download.py --accept-terms     # fetch what can be fetched

NORC distributes the GSS under its terms of use (https://gss.norc.org/terms-and-conditions.html).
The Stata files and the documentation PDFs are downloaded by hand: NORC's site
offers only the current release, and --check prints the steps for each. gssrdoc
is fetched by `make gssrdoc`.

Uses the PublicDataToolkit library (pdt.download); `make download` sets PYTHONPATH.
"""

import sys
from pathlib import Path

try:
    from pdt.download import main
except ImportError:
    sys.exit("Needs the PublicDataToolkit library: pip install -e ../PublicDataToolkit, "
             "or run through `make download`.")

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    sys.exit(main([str(ROOT), *sys.argv[1:]]))
