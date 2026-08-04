# Open market source probe

This directory documents a bounded, non-production assessment of possible open market sources. The probe is deliberately separate from `scripts/v2/run_mature_week.py`: no candidate is a V2 production source, no historical backfill is initiated, and no ChatGPT task is changed.

Use `python scripts/v2/probes/run_all_probes.py --dry-run` to regenerate the machine-readable, non-raw summaries. A future adapter may write transport responses only beneath `.local/source-probes/`, which is ignored. It must retain request parameters, retrieval times, HTTP headers, byte and semantic hashes, but those raw responses are never committed by this probe.

The three fixed assessment windows are the most recent 30 valid trading records selected from 60 calendar days, 2023-06-01 through 2023-06-30, and 2020-10-01 through 2020-10-31. A missing fixed window is reported as `historical_window_not_covered`; it is never substituted.
