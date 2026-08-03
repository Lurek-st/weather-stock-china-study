#!/usr/bin/env python3
"""Audit region files by date range. Official holiday checks remain a human/Codex review step."""
from __future__ import annotations
import argparse, json
from datetime import date, timedelta
from pathlib import Path

REGIONS=('asia','europe','us')
def daterange(a,b):
    cur=a
    while cur<=b:
        yield cur; cur+=timedelta(days=1)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--start',required=True); p.add_argument('--end',required=True); p.add_argument('--repo-root',default='.'); p.add_argument('--weekdays-only',action='store_true'); a=p.parse_args()
    root=Path(a.repo_root); start=date.fromisoformat(a.start); end=date.fromisoformat(a.end)
    report={'start':a.start,'end':a.end,'regions':{}}
    for region in REGIONS:
        existing={x.stem for x in (root/'data'/'raw'/region).glob('*.json')} if (root/'data'/'raw'/region).exists() else set()
        expected=[d.isoformat() for d in daterange(start,end) if not a.weekdays_only or d.weekday()<5]
        report['regions'][region]={'existing':sorted(set(expected)&existing),'missing_candidates':sorted(set(expected)-existing),
          'note':'Candidate list only. Codex must verify official exchange calendars before requesting backfill.'}
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
