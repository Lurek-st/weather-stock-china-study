# DNB resource access diagnostic

**Scope and stop point.** This bounded diagnostic examined access to the two
resource files registered for the DNB daily stock-market-indices dataset. It
does not parse market data, search for replacement sources, or change the
production pipeline. All named paths were checked once on 2026-08-04; no
network settings, DNS settings, hosts entries, or proxy configuration changed.

## Classification

`public_dns_nxdomain`

The production hostname `statistiek.api.dnb.nl` returned an explicit
non-existent-domain response through the Windows default resolver and through
both independently specified public resolvers (Cloudflare 1.1.1.1 and Google
8.8.8.8). Python's standard resolver likewise raised a name-resolution error.

## Resource results

| Resource | Registered purpose | Direct result | Final access result |
| --- | --- | --- | --- |
| `64131a7e-3eaa-45f1-a915-b47dbf05b517` | newer data (from 2018) | Direct no-proxy request could not resolve the host. | `request_failed` |
| `9f3c874d-6407-44f6-881c-7f9de1dcf31e` | historical data (1990--2019) | It remains listed in the official catalogue; direct no-proxy request could not resolve the same host. | `request_failed` |

The Windows default curl route was also blocked by an unreachable
process-scoped proxy before it could test the remote host. This does not alter
the DNS classification because the no-proxy request and public-DNS queries
failed independently. In WSL, `getent` produced no address; `nslookup` was not
installed and was not installed for this task. A proxy-routed WSL curl request
failed during TLS negotiation after its proxy tunnel, which is a separate
proxy-path observation rather than proof of resource availability.

## Official-path checks

The official Dutch government catalogue remains publicly accessible and lists
both exact production `resourcefile` URLs, with the dataset and each listed
JSON resource marked CC BY 4.0. It provides no evidence that the legacy
resources were removed or replaced.

The current DNB API-services page says API datasets are made available in
phases and users must register through My DNB and create a subscription to the
**Public** product to view technical documentation and use available datasets.
It does not identify this daily stock-index dataset as migrated or publish a
replacement endpoint. A My DNB subscription is therefore a possible next path,
not evidence that these listed files are retired.

## Consequence for AEX

Keep `aex_dnb` at `technical_access_blocked`. No AEX JSON, series definition,
window result, or third-party-rights conclusion can be claimed until an
official resource is resolvable or DNB confirms an accessible replacement.

## Evidence links

- [Official dataset catalogue](https://data.overheid.nl/dataset/3_1_day_aandelenbeursindicesdag)
- [DNB API services](https://www.dnb.nl/en/statistics/access-to-statistics-through-api-services/)
- [Newer registered resource](https://statistiek.api.dnb.nl/api/dataset/resourcefile?id=64131a7e-3eaa-45f1-a915-b47dbf05b517)
- [Historical registered resource](https://statistiek.api.dnb.nl/api/dataset/resourcefile?id=9f3c874d-6407-44f6-881c-7f9de1dcf31e)
