# KOSPI Service Key setup

The official FSC Index Price Information OpenAPI requires a Public Data Portal
Service Key. This repository neither stores nor requests that key.

1. On a desktop browser, sign in to the [official dataset page](https://www.data.go.kr/en/data/15094807/openapi.do).
2. Select the official use-application flow and obtain approval. The portal
   describes the development and operating applications as automatic approval.
3. Set the key only for the current PowerShell session:

   ```powershell
   $env:KOREA_DATA_GO_KR_SERVICE_KEY = "your locally issued Service Key"
   ```

4. Run `python scripts/v2/probes/probe_kospi.py --live` from the repository.

Do not put the key in a repository file, `.env.example`, fixture, pull request,
issue, command transcript, or chat. A user-level persistent environment
variable is outside this repository and should only be created when the user
explicitly chooses that configuration.
