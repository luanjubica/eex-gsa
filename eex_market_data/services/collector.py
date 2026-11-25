"""Run the shared EEX queue from a dedicated Odoo shell process."""
import logging
import time

import requests

from odoo import api, SUPERUSER_ID
from odoo.addons.eex_market_data.models.job import SERVICE_LOCK, WORKER_LOCK

from .client import EexClient

_logger = logging.getLogger(__name__)


class RequestPacer:
    """Space request starts across every job using this collector."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock = clock
        self.sleep = sleep
        self.last_start = None

    def wait(self, interval):
        if self.last_start is not None:
            remaining = interval - (self.clock() - self.last_start)
            if remaining > 0:
                self.sleep(remaining)
        self.last_start = self.clock()


def collect_forever(registry):
    with registry.cursor() as lease:
        lease.execute('SELECT pg_try_advisory_lock(%s)', [SERVICE_LOCK])
        if not lease.fetchone()[0]:
            raise RuntimeError('An EEX service collector is already running for this database.')
        lease.commit()
        try:
            _collect_while_enabled(registry)
        finally:
            lease.execute('SELECT pg_advisory_unlock(%s)', [SERVICE_LOCK])
            lease.commit()


def _collect_while_enabled(registry):
    pacer = RequestPacer()
    last_schedule = last_fast_schedule = None
    advanced = 0
    with requests.Session() as session:
        while True:
            ran = False
            with registry.cursor() as cr:
                cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [WORKER_LOCK])
                if cr.fetchone()[0]:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    if not env['eex.connection']._uses_service_collector():
                        _logger.info('EEX service collector disabled for %s.', registry.db_name)
                        return
                    jobs = env['eex.job']
                    now = time.monotonic()
                    if not jobs._collection_paused():
                        if last_schedule is None or now - last_schedule >= 5:
                            jobs._schedule(service=True)
                            last_schedule = now
                        if last_fast_schedule is None or now - last_fast_schedule >= 1:
                            jobs._schedule_fast_stats()
                            last_fast_schedule = now
                        due = jobs._due_jobs()
                        fast = ('connection_id.fast_last', '=', True)
                        if advanced >= 4:
                            job = jobs.search(due + ['|', ('kind', '!=', 'stat'),
                                                     ('connection_id.fast_last', '=', False)], limit=1)
                        else:
                            job = jobs.browse()
                        job = job or jobs.search(due + [('kind', '=', 'stat'), fast], limit=1)
                        job = job or jobs.search(due, limit=1)
                        if job:
                            client = EexClient(job.connection_id._token(), session=session, sleep=pacer.wait)
                            job._run(client)
                            advanced = advanced + 1 if job.kind == 'stat' and job.connection_id.fast_last else 0
                            ran = True
                    cr.commit()
            if not ran:
                time.sleep(0.25)
