#!/usr/bin/env python3
"""Generate a conservative ChatGPT backfill request from an audited date list."""
from __future__ import annotations
import argparse, json
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('audit_json'); p.add_argument('--output'); a=p.parse_args()
    report=json.loads(Path(a.audit_json).read_text(encoding='utf-8'))
    lines=['# Weather–Market Backfill Request','',f"Candidate range: {report.get('start')} to {report.get('end')}",'',
           'Before collecting, verify each candidate against the official exchange calendar. Do not create a record solely because it is a weekday.',
           'Use historical observed/reported data, preserve nulls, include source URLs, and output V1.0.2 daily transport blocks.','']
    for region, value in report.get('regions',{}).items():
        lines += [f'## {region}', *[f'- {d}' for d in value.get('missing_candidates',[])], '']
    text='\n'.join(lines)
    if a.output: Path(a.output).write_text(text,encoding='utf-8')
    else: print(text)
    return 0
if __name__=='__main__': raise SystemExit(main())
