#!/usr/bin/env python3
"""Entry point shim; the implementation lives in the switchboard package."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from switchboard.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
