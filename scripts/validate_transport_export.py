#!/usr/bin/env python3
"""Validate daily transport exports embedded in a ChatGPT response or weekly package."""
from __future__ import annotations
import argparse, json
from pathlib import Path
try:
    from .weekly_package import (
        WEEKLY_BEGIN,
        extract_daily_blocks,
        manifest_consistency_errors,
        parse_daily_exports_tolerant,
        parse_weekly_envelope,
        validate_transport,
    )
except ImportError:
    from weekly_package import (
        WEEKLY_BEGIN,
        extract_daily_blocks,
        manifest_consistency_errors,
        parse_daily_exports_tolerant,
        parse_weekly_envelope,
        validate_transport,
    )

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument('file'); args=parser.parse_args()
    text=Path(args.file).read_text(encoding='utf-8')
    exports, malformed=parse_daily_exports_tolerant(text)
    failed=0
    for item in malformed:
        failed+=1
        print(f"FAIL block-{item['block_index']}")
        print("  - invalid JSON: "+item["error"])
    if not extract_daily_blocks(text):
        failed+=1; print("FAIL package\n  - no daily export blocks found")
    if WEEKLY_BEGIN in text:
        try:
            envelope=parse_weekly_envelope(text)
            for error in manifest_consistency_errors(
                envelope, exports, len(extract_daily_blocks(text)), len(malformed)
            ):
                failed+=1; print("FAIL manifest\n  - "+error)
        except Exception as exc:
            failed+=1; print("FAIL weekly-envelope\n  - "+str(exc))
    for e in exports:
        errors=validate_transport(e)
        label=e.get('run_id','unknown')
        if errors:
            failed+=1; print(f'FAIL {label}'); [print('  - '+x) for x in errors]
        else: print(f'PASS {label}')
    return 1 if failed else 0
if __name__=='__main__': raise SystemExit(main())
