# DNB Public API setup boundary

This note is only for a user who independently chooses to explore DNB's newer
API after the legacy `resourcefile` host becomes unavailable. It does not
establish that the daily stock-market-indices dataset is available through that
API.

1. Go to [DNB API services](https://www.dnb.nl/en/statistics/access-to-statistics-through-api-services/).
2. Register or sign in through My DNB (the official page also names
   eHerkenning as a sign-in route).
3. In My DNB, open **DNB API Services**, then create a subscription for the
   **Public** product.
4. Inspect the DNB Statistics API documentation and its API-tab catalogue to
   confirm whether the named daily stock-market-indices dataset is available
   and to obtain its documented endpoint and authentication requirements.

Do not place passwords, subscription material, cookies, or credentials in this
repository, a pull request, or a chat. The official page consulted here requires
a subscription but does not specify a credential header or key name; this
repository therefore does not prescribe an environment-variable name or
configuration format. Follow the authenticated official documentation only
after access is granted.
