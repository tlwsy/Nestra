#!/usr/bin/env python3
"""Standalone wrapper for the shared SSRF-safe onboarding probe."""

from __future__ import annotations

import sys

from nestra.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["probe", *sys.argv[1:]]))
