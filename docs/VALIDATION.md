# Validation status

Validated on 16–17 September 2026:

- Fresh installation and subsequent module upgrade on Odoo 16 Community: passed.
- Ten Odoo regression tests: passed, with zero failures or errors. Coverage includes private/shared watchlists, company isolation, collection deduplication, refresh constraints, instrument filtering, stale-data status, sampling, missing/zero/negative prices, and preserving cached values on failed requests.
- Eight standalone connector/normalization tests: passed. Coverage includes exact endpoint paths, authentication headers, redirects, HTTP errors, throttling, truncation, malformed responses and UTC timestamps.
- Python compilation and XML parsing: passed.
- Basic credential-pattern scan of source files: no matches. No client API token is included.

The Odoo tests ran against official Odoo 16 source with an isolated local PostgreSQL database. Docker-based testing was interrupted by an unresponsive local Docker engine.

Browser rendering and end-to-end XLSX download validation remain pending. Automatic approval review blocked the synthetic test-data setup, reporting an approval-service usage limit. The module has not been tested with the client's EEX token or deployed to the client's server.

Before production use, complete the staging acceptance checks in [ARCHITECTURE.md](ARCHITECTURE.md), including actual market/feed entitlements, response parity, update frequency, browser interaction and Excel export.
