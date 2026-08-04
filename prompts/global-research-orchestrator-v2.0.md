# Global Research Orchestrator 2.0.0

Run weekly on Monday. Determine the latest complete week whose registered weather, market, calendar, and conflict evidence can satisfy the requested maturity. “Two weeks ago” is only the first candidate. Research official notices for holidays, early closes, special sessions, temporary closures, trading-rule changes, and registered-source incidents.

Do not output formal weather or market numbers. Do not create Daily Transport Export. Do not call the OpenAI API or any paid model API. Do not write GitHub. Never request secrets in chat.

Return exactly:

```text
BEGIN_CODEX_MATURE_WEEK_RUN
PROTOCOL_VERSION: 2.0.0
TARGET_WEEK: YYYY-Www
MATURITY_TARGET: provisional_ready|research_ready
SOURCE_REGISTRY_VERSION: 2.0.0
LOCATION_REGISTRY_VERSION: 2.0.0
KNOWN_CALENDAR_EXCEPTIONS:
- market_id: evidence URL and concise status, or none found
KNOWN_SOURCE_INCIDENTS:
- source_id: evidence URL and concise status, or none found
CODEX_COMMAND:
python scripts/v2/run_mature_week.py --week YYYY-Www --allow-provisional
REQUIRED_VALIDATIONS:
- registry and schema validation
- raw artifact hash validation
- calendar evidence present for every trading day
- no pending/conflicting/unavailable core market value
- weather-hour completeness and DST checks
- CSV/Parquet row-count and manifest-hash agreement
END_CODEX_MATURE_WEEK_RUN
```

If evidence is insufficient, set the maturity target conservatively and state the blocking source. Do not invent `none found` unless official sources were actually checked.
