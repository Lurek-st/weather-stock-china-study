# DNB stock-index inventory

The official Dutch catalogue identifies the dataset as **Aandelenbeursindices (Dag)**, with Dutch and international stock-market indices per day, both end-of-period (*ultimo*) and average values. It registers two CC-BY 4.0 JSON resources: 1990 through 2018/2019 and from 2018 onward.

| Evidence field | Dataset / resources |
| --- | --- |
| Dataset owner and publisher | De Nederlandsche Bank (Rijk) |
| Dataset licence | CC-BY 4.0: `https://creativecommons.org/licenses/by/4.0/` |
| Catalogue access | Public |
| Account / API Key | No requirement stated on the official catalogue |
| Update frequency | Daily |
| Historical resource | `9f3c874d-6407-44f6-881c-7f9de1dcf31e`; catalogue description: 1990 through 2018/2019; listed modified 2019-01-10 |
| Newer resource | `64131a7e-3eaa-45f1-a915-b47dbf05b517`; catalogue description: from 2018; listed modified 2020-04-01 |
| Resource format / licence | JSON / CC-BY 4.0 for each resource listing |
| Attribution / modification | CC-BY requires appropriate attribution and an indication of modifications; no Share-Alike requirement |
| Third-party exception | No exception is disclosed in the catalogue listing, but source/provenance fields in the actual files remain unreadable |

On 2026-08-04 this probe directly requested both prescribed `statistiek.api.dnb.nl/api/dataset/resourcefile` URLs twice. In this environment the hostname could not be resolved, so no actual JSON document was returned. No resource content, series code, sequence name, count, range, unit, source field, rights field, or value definition is asserted here.

| Required index | Resource result | Inventory result |
| --- | --- | --- |
| AEX | `request_failed` | `not_found_in_unreadable_resource` |
| S&P 500 | `request_failed` | `not_found_in_unreadable_resource` |
| FTSE 100 | `request_failed` | `not_found_in_unreadable_resource` |
| DAX | `request_failed` | `not_found_in_unreadable_resource` |
| Nikkei 225 | `request_failed` | `not_found_in_unreadable_resource` |
| TOPIX | `request_failed` | `not_found_in_unreadable_resource` |

`not_found_in_unreadable_resource` means no conclusion about absence: the file was not readable. It is intentionally not an inventory claim.

| International-index rights assessment | Resource licence applies | Third-party rights | Public redistribution | Evidence |
| --- | --- | --- | --- | --- |
| S&P 500 | `unclear` | `third_party_rights_unresolved` | `unclear` | JSON source/provenance fields unreadable |
| FTSE 100 | `unclear` | `third_party_rights_unresolved` | `unclear` | JSON source/provenance fields unreadable |
| DAX | `unclear` | `third_party_rights_unresolved` | `unclear` | JSON source/provenance fields unreadable |
| Nikkei 225 | `unclear` | `third_party_rights_unresolved` | `unclear` | JSON source/provenance fields unreadable |
| TOPIX | `unclear` | `third_party_rights_unresolved` | `unclear` | JSON source/provenance fields unreadable |

If both resources later become readable, deterministic stitching must prefer the newer resource on equal overlapping values, flag any overlapping value conflict as a revision investigation, and retain resource ID plus hash. This is an unexecuted rule, not a claim that the overlap is clean.
