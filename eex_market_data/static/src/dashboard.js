/** @odoo-module **/
import { registry } from '@web/core/registry';
import { useService } from '@web/core/utils/hooks';
import { Component, onWillStart, onWillUnmount, useState } from '@odoo/owl';

export class EexDashboard extends Component {
    setup() {
        this.orm = useService('orm');
        this.action = useService('action');
        this.notification = useService('notification');
        this.state = useState({
            data: null, error: '', busy: false, checkedAt: '', changedCells: {},
            secondsUntilCheck: 60, checkProgress: 0,
        });
        this.selected = this.props.action.params?.watchlist_id || false;
        this.alive = true;
        this.sequence = 0;
        this.previousValues = null;
        this.previousWatchlist = false;
        onWillStart(async () => {
            await this.load();
            this.timer = setInterval(() => this.tick(), 250);
        });
        onWillUnmount(() => { this.alive = false; clearInterval(this.timer); });
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
    }
    async load() {
        if (this.state.busy) return;
        const sequence = ++this.sequence;
        this.state.busy = true;
        try {
            const data = await this.orm.call('eex.watchlist', 'dashboard_data', [this.selected]);
            if (this.alive && sequence === this.sequence) {
                this.trackChanges(data);
                this.state.data = data;
                this.state.checkedAt = new Date().toISOString().slice(11, 19) + ' UTC';
                this.selected = data.selected;
                this.state.error = '';
            }
        } catch (error) {
            if (this.alive && sequence === this.sequence) {
                this.state.error = 'Could not load the watchlist. Check your connection and access permissions.';
            }
        } finally {
            if (this.alive && sequence === this.sequence) {
                this.state.busy = false;
                this.scheduleNextCheck();
            }
        }
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
        const now = Date.now();
        const snapshot = {};
        const sameWatchlist = this.previousWatchlist === data.selected;
        const changed = {};
        if (sameWatchlist) {
            for (const [key, until] of Object.entries(this.state.changedCells)) {
                if (until > now) changed[key] = until;
            }
        }
        for (const row of data.rows) {
            for (const column of data.columns) {
                const key = this.cellKey(row, column.key);
                const value = this.value(row, column);
                snapshot[key] = value;
                if (column.type === 'number' && sameWatchlist && this.previousValues &&
                        Object.prototype.hasOwnProperty.call(this.previousValues, key) &&
                        this.previousValues[key] !== value) {
                    changed[key] = now + 30000;
                }
            }
        }
        this.previousValues = snapshot;
        this.previousWatchlist = data.selected;
        this.state.changedCells = changed;
    }
    isChanged(row, key) { return Boolean(this.state.changedCells[this.cellKey(row, key)]); }
    async select(event) { this.selected = Number(event.target.value); await this.load(); }
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
    async history() {
        const action = await this.orm.call('eex.watchlist', 'action_history', [[this.selected]]);
        return this.action.doAction(action);
    }
    async loadHistory() {
        const result = await this.orm.call('eex.watchlist', 'action_history_refresh', [[this.selected]]);
        this.notification.add(result.params.message, { type: result.params.type });
    }
    export() { window.location.assign(`/eex/watchlist/${this.selected}/export`); }
    exportHistory() { window.location.assign(`/eex/watchlist/${this.selected}/history/export`); }
    value(row, column) {
        const value = this.rawValue(row, column);
        if (value === null || value === '' || value === false && column.type !== 'availability') return '—';
        if (column.type === 'number') return new Intl.NumberFormat(undefined, {
            maximumFractionDigits: column.field === 'volume' ? 2 : 4,
        }).format(value);
        if (column.type === 'status') return this.label(value);
        if (column.type === 'availability') return value ? 'Available' : 'Outside latest catalogue';
        if (column.type === 'datetime') return value + ' UTC';
        return value;
    }
    label(status) {
        return { ok: 'Available', waiting: 'Awaiting data', empty: 'No data', stale: 'Stale cache',
            previous_day: 'Earlier trading day', error: 'Collection error', disabled: 'Feed disabled', paused: 'Collection paused' }[status] || status;
    }
    shortTime(feed) { return feed.fetched_at ? feed.fetched_at.slice(11, 19) : '—'; }
    cellClass(row, column) {
        const feed = this.feedForColumn(row, column);
        return [column.type === 'number' ? 'text-end eex-number' : '',
            ['date', 'datetime'].includes(column.type) ? 'eex-time-cell' : '',
            column.type === 'text' ? 'eex-text-cell' : '',
            feed && ['stale', 'error', 'paused'].includes(feed.status) ? 'text-warning' : '',
        ].filter(Boolean).join(' ');
    }
    title(row, column) {
        const feed = this.feedForColumn(row, column);
        const value = this.rawValue(row, column);
        return feed ? this.detail(feed) : value === null || value === '' ? '' : String(value);
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
        this.state = useState({ data: null, error: '', busy: false, metric: 'last' });
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
        const action = await this.orm.call('eex.watchlist', 'action_history_details', [[this.watchlistId]]);
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
