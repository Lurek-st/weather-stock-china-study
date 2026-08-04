# Source Policy

Every production value must trace to a registered structured source and immutable raw artifact. The registry records documentation, access, credential, cost, coverage, latency, licence, automation, role, status, and fallback.

News may explain an anomaly or trigger review but has rank zero for production values. Undocumented endpoints, browser-session reverse engineering, and scraping that lacks confirmed permission are disabled. A controlled manual official/vendor file is preferable to an unlicensed automated scrape.

Credentials live only in official user configuration or environment variables. Never commit `.cdsapirc`, `.env`, tokens, cookies, passwords, or private keys. Logs and manifests store parameter names and redacted paths, never secret values.
