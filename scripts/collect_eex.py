# Run inside an Odoo shell scoped to a single database.
from odoo.addons.eex_market_data.services.collector import collect_forever

registry = env.registry
env.cr.rollback()
collect_forever(registry)
