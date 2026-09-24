# EEX Market Data for Odoo 16

An installable Odoo 16 Community/Enterprise add-on for self-hosted installations. Each user selects futures from an EEX reference catalogue, creates private or shared watchlists, chooses columns and a collection interval, and views cached prices in a market board.

## Included

- EEX DataSource REST **v2**, Bearer authentication, fixed official API origin.
- Discovery of commodities, areas, trading dates and outright futures by ISIN.
- Last/open/high/low prices and volume (`stat`), bid/ask (`tob`), settlement (`spr`).
- User-owned watchlists; optional company sharing grants read/refresh/export access, not editing rights. EEX administrators can manage all watchlists in their allowed companies.
- One shared market request per feed, regardless of how many users select that market. Cache stores selected instruments only.
- Background collection, configurable intervals, manual refresh requests, retries/backoff and job diagnostics.
- Per-feed status, trading date, source timestamp where supplied, and fetch timestamp. Missing prices remain missing; zero and negative prices remain valid.
- EEX API daily history with native Odoo tables, graphs and pivots, plus authenticated XLSX exports shaped like the former add-in output.
- Optional intraday sampling and configurable retention for both sampled and EEX API history.
- Token read from a server environment variable; never stored in Odoo fields or returned to the browser.

## Installation on the client's server

1. Copy `eex_market_data/` into an existing custom add-ons directory in Odoo's `addons_path`.
2. Ensure the Odoo Python environment contains `requests` and `xlsxwriter` (included in the official Odoo 16 Docker image).
3. Set `EEX_API_TOKEN` in the environment of **all Odoo workers**, including cron workers. For systemd, use a restricted environment file managed by the server administrator; for containers, use your deployment's secret/environment mechanism. Do not put the token in a workbook, repository or browser code.
4. Restart Odoo, update the Apps list in developer mode, remove the Apps filter if needed, and install **EEX Market Data**. CLI alternative: `odoo -d YOUR_DATABASE -i eex_market_data --stop-after-init` using your existing configuration.
5. Give administrators the **EEX Market Data / Administrator** role and other users **EEX Market Data / User** in Settings → Users.
6. Open **EEX Market Data → Configuration → Connections**. Create a connection for the company. Check **Token configured**, choose subscribed feeds and subscription timing, save, and enable collection.
7. Click **Discover markets / Test connection**. This queues a real authenticated reference-data request. Follow its result in **Collection Jobs**.
8. Open **Configuration → Markets**, enable the desired markets, and click **Sync catalogue**. The scheduled worker also picks newly enabled markets up automatically. Wait for the catalogue job to finish.
9. Create a watchlist, add instruments from the catalogue, choose columns and interval, save, then open the market board.

The Odoo scheduled action **EEX: schedule and collect market data** must remain active. A working cron worker and outbound HTTPS to `api.eex-group.com` are required. Allow at least 90 seconds for cron execution in your deployment; normal batches target 40 seconds, with one final request able to exceed that target by its timeout.

## Refresh and retention

The collector runs once per minute. Watchlists default to 300 seconds; `0` means manual collection. Administrators enforce a minimum of at least 60 seconds. Intervals are targets, not latency guarantees. Manual refresh queues work and obeys the same minimum. Open boards reread the **database cache** every 15 seconds while visible; they do not call EEX. A visible countdown and progress bar show the next cache check, while a second progress indicator shows feed collection. Board timestamps are always UTC. Changed values remain highlighted for 30 seconds after a board check; initial loads and newly added instruments establish a baseline without highlighting.

The worker processes at most 20 jobs per run, with a 40-second soft budget and 1.1-second spacing before every request. A database advisory lock serializes workers and enqueue actions. HTTP 429 pauses all pending jobs; transient failures retry up to five attempts. A job that failed because the server token was absent resumes automatically once the token is restored and the watchlist is due. Invalid or expired credentials, missing entitlements, malformed responses and truncation still require administrator attention and an explicit retry. Successful feeds remain independent of failed feeds.

Use one collector per EEX account. The advisory lock cannot coordinate separate Odoo databases, servers or other software using the same EEX account; these must share an external limiter or separate authorized account allocation.

EEX API history defaults to the latest 10 available trading days. The module queries the range-based historical `/stats` and `/sprs` derivatives endpoints for enabled daily feeds, stores values, and presents one date-by-instrument table per market with a selector for close, settlement, OHLC, or volume. Detailed Odoo list, graph, and pivot views remain available. Bid/ask history continues to come from the sampled live cache because EEX top-of-book history is tick-level and contract-specific. Use **Load history** after adding instruments, then **Export history** for one date-by-contract close sheet per market plus a normalized detail sheet. The lookback and retention are configurable; Odoo autovacuum removes records beyond retention.

Intraday sampling remains available and defaults to one observation per hour. It records what Odoo observed during live collection and is distinct from authoritative EEX daily history. Settlement corrections update the daily API-history row when history is loaded again.

## Data semantics and current scope

- Only `Future` / `Simple Instrument` contracts are imported. Options, spreads, rolling symbols, gas spot, indices and transparency feeds are outside this release.
- ISIN is the stable identity within each company/market catalogue. `Maturity` is not assumed to be the delivery month; actual `Start`/`End` delivery dates are used.
- Trading dates come from EEX reference data and are compared with Europe/Berlin's calendar. Weekends/holidays use the latest available trading date and are labelled as an earlier trading day. Dates are rechecked on a new Berlin day and at least every six hours.
- Prices are requested for that trading date. A missing same-day settlement is blank; the system does not silently substitute the previous day's settlement.
- `LastPx` and `TotTrdVol` are taken from EEX statistics, which include both exchange and trade registration activity. `stat` does not supply an event timestamp; no source time is invented.
- Settlement's `Tm` is its last correction timestamp, and top-of-book's `Tm` is its last update timestamp. Odoo stores UTC internally; the custom board/export explicitly label UTC.
- Volume uses EEX's reference UOM. Japanese POWER prices are displayed per kWh despite the reference volume UOM being MWh, as specified by EEX's guide. No currency conversion or contract-size conversion is applied.
- Staleness means no successful fetch within the larger of the administrator threshold or twice the watchlist interval. Trading date/source age are shown independently. A quiet market is not automatically an API outage.
- An empty successful quote response clears that feed's current values. Failed requests preserve the prior values with error/stale status. An empty reference catalogue preserves the existing catalogue and retries.
- Reaching EEX's 60,000-record cap fails visibly instead of silently accepting incomplete data. Product partitioning would need to be added for any market that reaches this cap.
- Subscription timing is an administrator-declared label, not a measurement. Confirm account entitlements, multi-user use and permitted retention with EEX before production rollout.

## Local evaluation and tests

The included Compose file is for **local evaluation only**, binds port 18069 to localhost and uses disposable development credentials. It never connects to a client's Odoo database. Initial local Odoo credentials are `admin` / `admin`; change them before any wider access.

```sh
docker compose -p eex16 up -d
```

Open <http://localhost:18069>. Collection starts disabled; no EEX token is required to install the module. Set `EEX_API_TOKEN` in your local environment before starting Compose only when you intend to test live access.

Run the Odoo regression tests in a separate test database:

```sh
docker compose -p eex16 up -d db
docker compose -p eex16 run --rm odoo odoo -d eex_test \
  -i eex_market_data --without-demo=all --test-enable \
  --test-tags /eex_market_data --stop-after-init --max-cron-threads=0
```

Use `-u eex_market_data` instead of `-i` for subsequent runs on the same test database. Standalone tests: `python3 -m unittest discover -s tests -v` in an environment with `requests` installed. Stop local services using `docker compose -p eex16 down`; named development volumes remain for the next run.

## API sources

Implementation checked against the public [EEX derivatives OpenAPI schema](https://eds.eex-group.com/api-spec/derivatives.yaml), API version 1.0.1, retrieved 16 September 2026, and [EEX REST v2 User Guide, revision 007](https://www.eex.com/fileadmin/EEX/Downloads/Market_Data/EEX_Group_DataSource/API/EEX_Group_DataSource_REST_API__v2__User_Guide_v007.pdf).

See [architecture and acceptance checklist](docs/ARCHITECTURE.md) for rollout validation. Live acceptance with the client's account remains a deployment step; mocked tests do not establish account entitlements or real-world latency.
