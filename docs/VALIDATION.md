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

## Odoo 16 XML file-mode compatibility fix — 17 September 2026

The client's older Odoo 16 checkout reads view XML by concatenating the architecture field's text with its child elements. Compact XML with no whitespace before the first child produced `None + str`, causing Watchlists, Connections and other view screens to fail when reading architectures from files.

Added leading whitespace inside every affected architecture field. Reproduced the failure on 12 views using the exact `get_view_arch_from_file` function from the client's checkout, then verified that all 13 module views load after the fix. Both new regression tests and all eight connector tests pass. This check executes only the XML reader functions; it does not connect to or modify the client's database.

To repeat with a specific Odoo version, set `ODOO_SOURCE_ROOT` to that checkout and run `python -m unittest discover -s tests -v` with its Python environment (requires `lxml` and `requests`). Without this variable, the version-specific test is skipped and the standalone XML compatibility check still runs.
