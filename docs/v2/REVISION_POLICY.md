# Revision Policy

Raw artifacts are append-only. Identical content hashes are idempotently reused. Changed content creates a new revision and manifest; old bytes and metadata remain.

Market values use `pending`, `provisional`, `final`, `corrected`, `conflicting`, or `unavailable`. T+1 may remain pending. Retry at T+3/T+5 and again at mature-week execution. The first disagreement is `conflicting`, not unrecoverable. Resolution order is official final, official provisional, registered primary final, registered backup, then news for explanation only.

Canonical and panel outputs are fully rebuildable from registered raw revisions. A correction records old/new hashes, revision, reason, and timestamp. Frozen panels are immutable research releases in meaning; if a correction is required, publish a higher frozen revision and do not rewrite the cited revision.
