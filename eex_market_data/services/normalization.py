"""Pure normalization functions; missing prices stay null, including in exports."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

FEED_FIELDS = {
    'stat': {'last': 'LastPx', 'open': 'OpenPx', 'high': 'HighPx', 'low': 'LowPx', 'volume': 'TotTrdVol'},
    'tob': {'bid': 'BidPx', 'ask': 'AskPx'},
    'spr': {'settlement': 'Px'},
}


def number(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid numeric market data') from None
    if not parsed.is_finite():
        raise ValueError('Non-finite market data')
    return float(parsed)


def utc_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('EEX timestamp has no timezone')
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def is_outright(row):
    return (row.get('InstrumentType') == 'Simple Instrument'
            and not row.get('Legs') and not row.get('OptionType')
            and row.get('ProductType', 'Future') == 'Future')


def reference_values(row):
    if not isinstance(row, dict) or not all(row.get(key) for key in ('InstrumentType', 'Cmdty', 'Area')):
        raise ValueError('Unexpected reference data schema')
    if not is_outright(row):
        return None
    if not all(row.get(key) for key in ('InstrumentISIN', 'ShortCode', 'Maturity', 'Cmdty', 'Area')):
        raise ValueError('Incomplete instrument identity')
    return {
        'name': row.get('DisplayName') or '%s %s' % (row['ShortCode'], row['Maturity']),
        'isin': row['InstrumentISIN'], 'short_code': row['ShortCode'],
        'maturity': str(row['Maturity']), 'long_name': row.get('LongName'),
        'currency': row.get('Currency'), 'volume_unit': row.get('UOM'),
        # Japanese power prices use JPY/kWh although reference UOM is MWh.
        'price_unit': 'kWh' if row['Cmdty'] == 'POWER' and row['Area'] == 'JP' else row.get('UOM'),
        'delivery_start': row.get('Start'), 'delivery_end': row.get('End'),
        'expiry_date': row.get('ExpiryDate'), 'maturity_type': row.get('MaturityType'),
    }


def quote_values(row, kind):
    if not isinstance(row, dict) or not all(row.get(key) for key in ('InstrumentType', 'Cmdty', 'Area', 'TrdDate')):
        raise ValueError('Unexpected quote data schema')
    if not is_outright(row):
        return None
    return {name: number(row.get(source)) for name, source in FEED_FIELDS[kind].items()}
