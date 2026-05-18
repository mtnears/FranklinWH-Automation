#!/usr/bin/env python3
"""
version.py — Single source of truth for the FranklinWH-Automation version.

Reads from the VERSION file at the repo root. Inside the container the file
is at /app/VERSION; for out-of-container testing it's resolved relative to
this script. Result is cached after first successful read.

Usage:
    from version import get_version
    v = get_version()  # e.g. "4.4.1"
"""

import os

_CACHED_VERSION = None


def get_version() -> str:
    """Return the project version string. Returns 'unknown' if no VERSION
    file is readable from any known location."""
    global _CACHED_VERSION
    if _CACHED_VERSION is not None:
        return _CACHED_VERSION

    candidates = [
        '/app/VERSION',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'VERSION'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION'),
    ]

    for path in candidates:
        try:
            with open(path, 'r') as f:
                v = f.read().strip()
                if v:
                    _CACHED_VERSION = v
                    return _CACHED_VERSION
        except (OSError, IOError):
            continue

    _CACHED_VERSION = 'unknown'
    return _CACHED_VERSION


if __name__ == '__main__':
    print(get_version())
