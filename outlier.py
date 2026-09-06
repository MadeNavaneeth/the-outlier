#!/usr/bin/env python3
"""Convenience entry point: python3 outlier.py <command>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from outlier.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
