import os
import re

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from ..services.client import INVALID_TOKEN_MESSAGE, MISSING_TOKEN_MESSAGE


class EexConnection(models.Model):
    _name = 'eex.connection'
    _description = 'EEX Connection'

    name = fields.Char(required=True, default='EEX DataSource')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    token_env = fields.Char(required=True, default='EEX_API_TOKEN', string='Token environment variable')
    token_configured = fields.Boolean(compute='_compute_token_configured')
    token_expiry = fields.Date(string='Token expiry (for reference)')
    enabled = fields.Boolean(string='Enable collection', default=False)
    data_timing = fields.Selection([('unknown', 'Not verified'), ('realtime', 'Real-time subscription'),
                                    ('delayed', 'Delayed subscription')], default='unknown', required=True)
    minimum_refresh = fields.Integer(default=60, string='Minimum refresh (seconds)')
    board_refresh_interval = fields.Integer(default=60, string='Board cache refresh (seconds)',
                                            help='How often an open market board rereads Odoo cached values. This does not call EEX.')
    stale_after = fields.Integer(default=900, string='Stale after (seconds without a successful fetch)')
    fast_last = fields.Boolean(string='Fast Last price', help='Use the dedicated collector for faster Last updates.')
    fast_last_interval = fields.Integer(default=10, string='Last collection target (seconds)')
    collect_stat = fields.Boolean(default=True, string='Last price and statistics')
    collect_tob = fields.Boolean(default=False, string='Bid / ask')
    collect_spr = fields.Boolean(default=True, string='Settlement prices')
    retain_history = fields.Boolean(default=True, string='Store sampled history')
    history_interval = fields.Integer(default=3600, string='History sample interval (seconds)')
    history_backfill_days = fields.Integer(default=10, string='API history trading days')
    retention_days = fields.Integer(default=90, string='History retention (days)')
    market_ids = fields.One2many('eex.market', 'connection_id')

    _sql_constraints = [('company_unique', 'unique(company_id)', 'Use one EEX connection per company.')]

    @api.depends('token_env')
    def _compute_token_configured(self):
        for record in self:
            record.token_configured = bool(os.environ.get(record.token_env or ''))

    @api.constrains('token_env', 'minimum_refresh', 'board_refresh_interval', 'stale_after', 'history_interval',
                    'history_backfill_days', 'retention_days', 'fast_last_interval')
    def _validate_settings(self):
        for record in self:
            if not re.fullmatch(r'EEX_[A-Z0-9_]+', record.token_env or ''):
                raise ValidationError(_('Use an environment variable starting with EEX_.'))
            if record.minimum_refresh < 60 or record.stale_after < record.minimum_refresh:
                raise ValidationError(_('Minimum refresh must be at least 60 seconds; stale threshold must be no shorter.'))
            if not 5 <= record.board_refresh_interval <= 3600:
                raise ValidationError(_('Board cache refresh must be between 5 and 3,600 seconds.'))
            if not 2 <= record.fast_last_interval <= 60:
                raise ValidationError(_('The Last collection interval must be between 2 and 60 seconds.'))
            if record.history_interval < 60 or record.retention_days < 1:
                raise ValidationError(_('History interval must be at least 60 seconds and retention at least one day.'))
            if not 1 <= record.history_backfill_days <= 366:
                raise ValidationError(_('API history must cover between 1 and 366 trading days.'))

    def _token(self):
        self.ensure_one()
        return os.environ.get(self.token_env, '').strip()

    def _feeds(self):
        self.ensure_one()
        return [kind for kind in ('stat', 'tob', 'spr') if self['collect_' + kind]]

    def action_discover(self):
        self.ensure_one()
        if not self.env.user.has_group('eex_market_data.group_eex_manager'):
            raise AccessError(_('Only EEX administrators can discover markets.'))
        self.check_access_rights('write')
        self.check_access_rule('write')
        if not self.enabled or not self._token():
            raise ValidationError(_('Set the token on the server and enable collection first.'))
        self.env['eex.job'].sudo()._enqueue(self, 'commodities')
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Discovery queued'), 'message': _('The background worker will load available markets.'),
            'type': 'success', 'sticky': False}}

    @api.model
    def _uses_service_collector(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'eex_market_data.service_collector') == '1'

    def action_retry_authentication(self):
        self.ensure_one()
        if not self.env.user.has_group('eex_market_data.group_eex_manager'):
            raise AccessError(_('Only EEX administrators can retry authentication.'))
        self.check_access_rights('write')
        self.check_access_rule('write')
        if not self._token():
            raise ValidationError(_('Set the token on the server before retrying.'))
        jobs = self.env['eex.job'].sudo()
        jobs._lock_queue()
        failed = jobs.search([
            ('connection_id', '=', self.id), ('state', '=', 'failed'),
            ('message', 'in', [MISSING_TOKEN_MESSAGE, INVALID_TOKEN_MESSAGE]),
        ])
        failed.write({'state': 'pending', 'attempts': 0, 'next_run': fields.Datetime.now()})
        if self.enabled and self.active:
            jobs._enqueue(self, 'commodities')
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Authentication check queued'),
            'message': _('Retrying authentication and %s previously failed jobs.') % len(failed),
            'type': 'info',
        }}
