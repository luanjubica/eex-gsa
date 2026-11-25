from odoo import models

CHANNEL_PREFIX = 'eex_market_data.market.'


class EexWebsocket(models.AbstractModel):
    _inherit = 'ir.websocket'

    def _build_bus_channel_list(self, channels):
        other = []
        market_ids = set()
        for channel in channels:
            if channel.startswith(CHANNEL_PREFIX):
                market_id = channel[len(CHANNEL_PREFIX):]
                if market_id.isascii() and market_id.isdigit() and len(market_id) <= 10:
                    market_ids.add(int(market_id))
            else:
                other.append(channel)
        if market_ids and self.env.user.has_group('eex_market_data.group_eex_user'):
            markets = self.env['eex.market'].search([('id', 'in', list(market_ids))])
            other.extend(market._price_channel() for market in markets)
        return super()._build_bus_channel_list(other)
