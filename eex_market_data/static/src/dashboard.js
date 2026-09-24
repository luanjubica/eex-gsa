/** @odoo-module **/
import { registry } from '@web/core/registry';
import { useService } from '@web/core/utils/hooks';
import { Component, onWillStart, onWillUnmount, useState } from '@odoo/owl';

export class EexDashboard extends Component {
    setup() {
        this.orm = useService('orm');
        this.action = useService('action');
        this.notification = useService('notification');
        this.state = useState({ data: null, error: '', busy: false, checkedAt: '', changedCells: {} });
        this.selected = this.props.action.params?.watchlist_id || false;
        this.alive = true;
        this.sequence = 0;
        this.previousValues = null;
        this.previousWatchlist = false;
        onWillStart(async () => {
            await this.load();
            this.timer = setInterval(() => {
                if (!document.hidden && !this.state.busy) this.load();
            }, 15000);
        });
        onWillUnmount(() => { this.alive = false; clearInterval(this.timer); });
    }
    async load() {
        const sequence = ++this.sequence;
        this.state.busy = true;
        try {
            const data = await this.orm.call('eex.watchlist', 'dashboard_data', [this.selected]);
            if (this.alive && sequence === this.sequence) {
                this.trackChanges(data);
                this.state.data = data;
                this.state.checkedAt = new Date().toLocaleTimeString();
                this.selected = data.selected;
                this.state.error = '';
            }
        } catch (error) {
            if (this.alive && sequence === this.sequence) {
                this.state.error = 'Could not load the watchlist. Check your connection and access permissions.';
            }
        } finally {
            if (this.alive && sequence === this.sequence) this.state.busy = false;
        }
    }
    cellKey(row, key) { return `${row.id}:${key}`; }
    rawValue(row, key) {
        const value = this.feed(row, key).values[key];
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
                const value = this.value(row, column.key);
                snapshot[key] = value;
                if (sameWatchlist && this.previousValues &&
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
    feed(row, key) { return row.feeds[key === 'settlement' ? 'spr' : ['bid', 'ask'].includes(key) ? 'tob' : 'stat']; }
    value(row, key) {
        const value = this.rawValue(row, key);
        return value === null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: key === 'volume' ? 2 : 4 }).format(value);
    }
    label(status) {
        return { ok: 'Available', waiting: 'Awaiting data', empty: 'No data', stale: 'Stale cache',
            previous_day: 'Earlier trading day', error: 'Collection error', disabled: 'Feed disabled', paused: 'Collection paused' }[status] || status;
    }
    shortTime(feed) { return feed.fetched_at ? feed.fetched_at.slice(11, 19) : '—'; }
    detail(feed) {
        return `${this.label(feed.status)}\nTrading date: ${feed.trade_date || '—'}\nSource: ${feed.source_at ? feed.source_at + ' UTC' : 'Not supplied'}\nFetched: ${feed.fetched_at ? feed.fetched_at + ' UTC' : '—'}${feed.message ? '\n' + feed.message : ''}`;
    }
}
EexDashboard.template = 'eex_market_data.Dashboard';
registry.category('actions').add('eex_market_data.dashboard', EexDashboard);
