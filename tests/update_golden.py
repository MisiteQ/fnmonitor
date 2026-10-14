#!/usr/bin/env python3
"""Regenerate tests/golden/shapes.json (JSON key/type contract of the main API endpoints).

Run from the repo root after an intentional API change, review the diff, commit it:

    python3 tests/update_golden.py
    git diff tests/golden/shapes.json

Values are never stored, only keys and types (see fnm_support.shape / OPAQUE_PATHS)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fnm_support as S  # noqa: E402


def main():
    srv = S.contract_server().start()
    try:
        shapes = S.capture_shapes(srv)
    finally:
        srv.stop()
    os.makedirs(os.path.dirname(S.GOLDEN), exist_ok=True)
    with open(S.GOLDEN, "w", encoding="utf-8") as f:
        json.dump(shapes, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print("wrote %s (%d endpoints)" % (os.path.relpath(S.GOLDEN), len(shapes)))


if __name__ == "__main__":
    main()
