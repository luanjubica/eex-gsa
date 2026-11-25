/** @odoo-module **/
import { registry } from '@web/core/registry';
import { useService } from '@web/core/utils/hooks';
import { Component, onWillStart, onWillDestroy, useState } from '@odoo/owl';

const priceFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 });
const volumeFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export class EexDashboard extends Component {
    setup() {
        this.orm = useService('orm');
        this.action = useService('action');
        this.notification = useService('notification');
        this.state = useState({
            data: null, error: '', busy: false, changedCells: {},
            secondsUntilCheck: 60, checkProgress: 0,
        });
        this.selected = this.props.action.params?.watchlist_id || false;
        this.requestedWatchlist = this.selected;
        this.alive = true;
        this.reloadQueued = false;
        this.channels = new Set();
        this.pendingMarkets = new Set();
        this.deltaPending = false;
        this.bus = useService('bus_service');
        this.onPrices = ({ detail }) => {
            for (const { type, payload } of detail) {
                if (type === 'eex/latest' && this.channels.has(payload)) {
                    this.pendingMarkets.add(payload);
                }
            }
            this.flushLatest();
        };
        this.onReconnect = () => {
            if (this.channels.size) this.load();
        };
        this.onVisibility = () => {
            if (!document.hidden && this.channels.size) this.load();
        };
        this.bus.addEventListener('notification', this.onPrices);
        this.bus.addEventListener('connect', this.onReconnect);
        this.bus.addEventListener('reconnect', this.onReconnect);
        document.addEventListener('visibilitychange', this.onVisibility);
        onWillStart(async () => {
            await this.load();
            if (this.alive) this.timer = setInterval(() => this.tick(), 250);
        });
        onWillDestroy(() => {
            this.alive = false;
            clearInterval(this.timer);
            this.bus.removeEventListener('notification', this.onPrices);
            this.bus.removeEventListener('connect', this.onReconnect);
            this.bus.removeEventListener('reconnect', this.onReconnect);
            document.removeEventListener('visibilitychange', this.onVisibility);
            for (const market of this.channels) this.bus.deleteChannel(this.channel(market));
            this.channels.clear();
        });
    }
    channel(id) { return `eex_market_data.market.${id}`; }
    syncChannels(marketIds) {
        const next = new Set(marketIds);
        for (const market of this.channels) {
            if (!next.has(market)) this.bus.deleteChannel(this.channel(market));
        }
        const added = [...next].filter(market => !this.channels.has(market));
        this.channels = next;
        for (const market of added) {
            this.bus.addChannel(this.channel(market)).then(() => {
                if (!this.channels.has(market)) this.bus.deleteChannel(this.channel(market));
            }).catch(() => {
                // The periodic board check also works when WebSocket is unavailable.
            });
        }
    }
    scheduleNextCheck() {
        const seconds = this.state.data?.board_refresh_interval || 60;
        this.pollDuration = seconds * 1000;
        this.nextCheckAt = Date.now() + this.pollDuration;
        this.state.secondsUntilCheck = seconds;
        this.state.checkProgress = 0;
    }
    tick() {
        if (document.hidden || !this.nextCheckAt) return;
        const remaining = Math.max(0, this.nextCheckAt - Date.now());
        this.state.secondsUntilCheck = Math.ceil(remaining / 1000);
        this.state.checkProgress = Math.min(100, Math.round(100 * (this.pollDuration - remaining) / this.pollDuration));
        if (!remaining && !this.state.busy) this.load();
        const now = Date.now();
        for (const [key, until] of Object.entries(this.state.changedCells)) {
            if (until <= now) delete this.state.changedCells[key];
        }
    }
    async load() {
        if (!this.alive) return;
        if (this.state.busy || this.deltaPending) {
            this.reloadQueued = true;
            return;
        }
        this.reloadQueued = false;
        const watchlist = this.requestedWatchlist;
        this.state.busy = true;
        try {
            const data = await this.orm.call('eex.watchlist', 'dashboard_data', [watchlist]);
            if (this.alive && this.requestedWatchlist === watchlist) {
                this.trackChanges(data);
                this.state.data = data;
                this.selected = data.selected;
                this.requestedWatchlist = data.selected;
                this.state.error = '';
                this.syncChannels(data.market_ids || []);
            }
        } catch (error) {
            if (this.alive && this.requestedWatchlist === watchlist) {
                this.state.error = 'Could not load the watchlist. Check your connection and access permissions.';
            }
        } finally {
            if (this.alive) {
                this.state.busy = false;
                this.scheduleNextCheck();
                this.runPending();
            }
        }
    }
    runPending() {
        if (this.reloadQueued) this.load();
        else this.flushLatest();
    }
    async flushLatest() {
        if (!this.alive || document.hidden || this.state.busy || this.deltaPending ||
                !this.pendingMarkets.size || this.requestedWatchlist !== this.selected) return;
        const markets = new Set([...this.pendingMarkets].filter(id => this.channels.has(id)));
        this.pendingMarkets.clear();
        if (!markets.size || !this.state.data) return;
        const watchlist = this.selected;
        this.deltaPending = true;
        try {
            const data = await this.orm.call('eex.watchlist', 'dashboard_latest', [watchlist, [...markets]]);
            if (!this.alive || this.requestedWatchlist !== watchlist) return;
            const rows = this.state.data.rows.filter(row => markets.has(row.market_id));
            const feeds = new Map((data?.rows || []).map(row => [row.id, row.feed]));
            if (feeds.size !== rows.length || rows.some(row => !feeds.has(row.id))) {
                this.reloadQueued = true;
                return;
            }
            const expires = Date.now() + 30000;
            const columns = this.state.data.columns.filter(column => column.source === 'stat' && column.type === 'number');
            for (const row of rows) {
                const feed = feeds.get(row.id);
                for (const column of columns) {
                    if ((row.feeds.stat.values[column.field] ?? null) !== (feed.values[column.field] ?? null)) {
                        this.state.changedCells[this.cellKey(row, column.key)] = expires;
                    }
                }
                row.feeds.stat = feed;
            }
        } catch (error) {
            // The next full cache check reconciles permissions, connectivity and any missed events.
        } finally {
            this.deltaPending = false;
            if (this.alive) this.runPending();
        }
    }
    get lastStatsAt() {
        let latest = '';
        for (const row of this.state.data.rows) {
            const fetched = row.feeds.stat.fetched_at;
            if (fetched && fetched > latest) latest = fetched;
        }
        return latest ? latest + ' UTC' : '—';
    }
    cellKey(row, key) { return `${row.id}:${key}`; }
    feedForColumn(row, column) {
        return column.source === 'instrument' ? null : row.feeds[column.source];
    }
    rawValue(row, column) {
        const feed = this.feedForColumn(row, column);
        const value = column.source === 'instrument' ? row[column.field] :
            column.type === 'number' ? feed.values[column.field] : feed[column.field];
        return value === undefined ? null : value;
    }
    trackChanges(data) {
        const current = this.state.data;
        if (current?.selected !== data.selected) {
            this.state.changedCells = {};
            return;
        }
        const byId = new Map(current.rows.map(row => [row.id, row]));
        const existing = new Set(current.columns.filter(column => column.type === 'number').map(column => column.key));
        const columns = data.columns.filter(column => column.type === 'number' && existing.has(column.key));
        const expires = Date.now() + 30000;
        for (const row of data.rows) {
            const previous = byId.get(row.id);
            if (!previous) continue;
            for (const column of columns) {
                if (this.rawValue(previous, column) !== this.rawValue(row, column)) {
                    this.state.changedCells[this.cellKey(row, column.key)] = expires;
                }
            }
        }
    }
    isChanged(row, key) { return Boolean(this.state.changedCells[this.cellKey(row, key)]); }
    async select(event) { this.requestedWatchlist = Number(event.target.value); await this.load(); }
    async refresh() {
        try {
            await this.orm.call('eex.watchlist', 'action_refresh', [[this.selected]]);
            this.notification.add('Refresh queued. The background worker will collect data within the configured limits.', { type: 'info' });
            await this.load();
        } catch (error) {
            this.notification.add('Could not queue a refresh.', { type: 'danger' });
        }
    }
    edit() {
        return this.action.doAction({ type: 'ir.actions.act_window', res_model: 'eex.watchlist',
            res_id: this.selected, views: [[false, 'form']], target: 'current' });
    }
    create() {
        return this.action.doAction({ type: 'ir.actions.act_window', res_model: 'eex.watchlist',
            views: [[false, 'form']], target: 'current' });
    }
    importBook() {
        return this.action.doAction('eex_market_data.action_book_import');
    }
    async history() {
        const action = await this.orm.call('eex.watchlist', 'action_history', [[this.selected]]);
        return this.action.doAction(action);
    }
    async loadHistory() {
        const result = await this.orm.call('eex.watchlist', 'action_history_refresh', [[this.selected]]);
        this.notification.add(result.params.message, { type: result.params.type });
    }
    export() { window.location.assign(`/eex/watchlist/${this.selected}/export`); }
    exportBook() { window.location.assign(`/eex/watchlist/${this.selected}/book/export`); }
    exportHistory() { window.location.assign(`/eex/watchlist/${this.selected}/history/export`); }
    value(row, column) {
        const value = this.rawValue(row, column);
        if (value === null || value === '' || value === false && column.type !== 'availability') return '—';
        if (column.type === 'number') return (column.field === 'volume' ? volumeFormatter : priceFormatter).format(value);
        if (column.type === 'status') return this.label(value);
        if (column.type === 'availability') return value ? 'Available' : 'Outside latest catalogue';
        if (column.type === 'datetime') return value + ' UTC';
        return value;
    }
    label(status) {
        return { ok: 'Available', waiting: 'Awaiting data', empty: 'No data', stale: 'Stale cache',
            previous_day: 'Earlier trading day', error: 'Collection error', authentication: 'Authentication failed', disabled: 'Feed disabled', paused: 'Collection paused' }[status] || status;
    }
    shortTime(feed) { return feed.fetched_at ? feed.fetched_at.slice(11, 19) : '—'; }
    cellClass(row, column) {
        const feed = this.feedForColumn(row, column);
        return [column.type === 'number' ? 'text-end eex-number' : '',
            ['date', 'datetime'].includes(column.type) ? 'eex-time-cell' : '',
            column.type === 'text' ? 'eex-text-cell' : '',
            feed && ['stale', 'error', 'authentication', 'paused'].includes(feed.status) ? 'text-warning' : '',
        ].filter(Boolean).join(' ');
    }
    title(row, column) {
        const feed = this.feedForColumn(row, column);
        const value = this.rawValue(row, column);
        if (!feed) return value === null || value === '' ? '' : String(value);
        const detail = this.detail(feed);
        return column.key === 'last' && value === null && ['ok', 'empty'].includes(feed.status)
            ? 'No Last trade reported for this session.\n' + detail : detail;
    }
    detail(feed) {
        return `${this.label(feed.status)}\nTrading date: ${feed.trade_date || '—'}\nSource: ${feed.source_at ? feed.source_at + ' UTC' : 'Not supplied'}\nFetched: ${feed.fetched_at ? feed.fetched_at + ' UTC' : '—'}${feed.message ? '\n' + feed.message : ''}`;
    }
}
EexDashboard.template = 'eex_market_data.Dashboard';
registry.category('actions').add('eex_market_data.dashboard', EexDashboard);

export class EexHistoryDashboard extends Component {
    setup() {
        this.orm = useService('orm');
        this.action = useService('action');
        this.notification = useService('notification');
        this.watchlistId = this.props.action.params?.watchlist_id;
        this.state = useState({ data: null, error: '', busy: false, metric: 'settlement' });
        onWillStart(() => this.load());
    }
    async load(metric = this.state.metric) {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const data = await this.orm.call(
                'eex.watchlist', 'history_dashboard_data', [this.watchlistId, metric]
            );
            this.state.data = data;
            this.state.metric = data.metric;
            this.state.error = '';
        } catch (error) {
            this.state.error = 'Could not load daily history. Check your connection and access permissions.';
        } finally {
            this.state.busy = false;
        }
    }
    selectMetric(event) { return this.load(event.target.value); }
    back() {
        return this.action.doAction({
            type: 'ir.actions.client', tag: 'eex_market_data.dashboard',
            params: { watchlist_id: this.watchlistId },
        });
    }
    async details() {
        const action = await this.orm.call('eex.watchlist', 'action_history_details', [[this.watchlistId], this.state.metric]);
        return this.action.doAction(action);
    }
    async loadHistory() {
        const result = await this.orm.call('eex.watchlist', 'action_history_refresh', [[this.watchlistId]]);
        this.notification.add(result.params.message, { type: result.params.type });
    }
    exportHistory() {
        window.location.assign(`/eex/watchlist/${this.watchlistId}/history/export`);
    }
    value(value, metric) {
        if (value === null || value === undefined) return '—';
        return new Intl.NumberFormat(undefined, {
            maximumFractionDigits: metric === 'volume' ? 2 : 4,
        }).format(value);
    }
}
EexHistoryDashboard.template = 'eex_market_data.HistoryDashboard';
registry.category('actions').add('eex_market_data.history_dashboard', EexHistoryDashboard);
