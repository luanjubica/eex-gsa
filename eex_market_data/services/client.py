"""Small, GET-only client. Never include credentials or response bodies in errors."""
import time
from urllib.parse import quote

import requests

BASE_URL = 'https://api.eex-group.com/v2'
MAX_RECORDS = 60000
MISSING_TOKEN_MESSAGE = 'Configure a valid EEX access token on the server.'


class EexError(Exception):
    def __init__(self, message, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class EexClient:
    def __init__(self, token, session=None, sleep=time.sleep):
        self.token = token
        self.session = session or requests.Session()
        self.sleep = sleep

    def get(self, *segments, listing=False, params=None):
        if not self.token or '\n' in self.token or '\r' in self.token:
            raise EexError(MISSING_TOKEN_MESSAGE)
        if not segments or segments[0] not in ('rd', 'stat', 'tob', 'spr', 'stats', 'tobs', 'sprs'):
            raise EexError('Unsupported EEX endpoint.')
        path = '/'.join(quote(str(part), safe='') for part in segments)
        params = params or {}
        allowed_params = {'from', 'to', 'instrumentType', 'timeFrom', 'timeTo', 'limit', 'fields'}
        if not isinstance(params, dict) or set(params) - allowed_params:
            raise EexError('Unsupported EEX request parameters.')
        if listing:
            path += '/'
        # All callers run under the database-wide worker lock. Waiting before
        # every request also spaces calls across jobs and connection changes.
        self.sleep(1.1)
        try:
            response = self.session.get(
                BASE_URL + '/' + path,
                headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'},
                timeout=(5, 20), allow_redirects=False,
                params=params,
            )
        except requests.RequestException:
            raise EexError('EEX request failed or timed out.', retryable=True) from None
        status = response.status_code
        if status == 404:
            return []
        if status == 429:
            try:
                delay = min(3600, max(60, int(response.headers.get('Retry-After', 60))))
            except (TypeError, ValueError):
                delay = 60
            raise EexError('EEX rate limit reached. Retrying later.', True, delay)
        errors = {
            401: 'EEX token is missing, expired or invalid.',
            403: 'The EEX subscription does not permit this request.',
            400: 'EEX rejected the request parameters.',
        }
        if status != 200:
            raise EexError(errors.get(status, 'EEX returned HTTP %s.' % status), status >= 500)
        try:
            result = response.json()
        except ValueError:
            raise EexError('EEX returned invalid JSON.', True) from None
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            raise EexError('Unexpected EEX response structure.')
        if len(result) >= MAX_RECORDS:
            raise EexError('EEX response reached 60,000 records. Narrow the market/product scope before retrying.')
        return result
