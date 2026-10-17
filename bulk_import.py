#!/usr/bin/env python3
"""Entry point: python bulk_import.py <command> [options]."""

import sys

from webnovel_importer.cli import main

if __name__ == "__main__":
    sys.exit(main())
