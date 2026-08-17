# ChatGPT Task Migration

Do not assume any existing scheduled task was changed automatically.

After this PR is reviewed and merged:

1. Disable the V1.0.2 Tuesday–Saturday formal numeric collection tasks.
2. Keep the old prompt and V1 code as historical/deprecated compatibility material.
3. Create one Monday task using `prompts/global-research-orchestrator-v2.0.md`.
4. Make the task determine a mature candidate week, inspect official calendar/source incidents, and output only a Codex command envelope.
5. Confirm it emits no weather/market values, Daily Transport Export, GitHub writes, OpenAI API calls, or secrets.
6. Run the first result through `--dry-run`; then perform a fixture acceptance and, after credentials/licences are ready, one live mature week.
