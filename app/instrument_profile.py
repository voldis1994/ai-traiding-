"""Best-effort cross-asset classification of a broker instrument.

Never invent a currency mapping for an unknown symbol: the news guard then
reports UNMAPPED_INSTRUMENT instead of claiming a false all-clear.

Capital.com CFD epics (e.g. CS.D.USCGC.TODAY.IP) often omit the word GOLD —
map those broker codes explicitly so news/peers are not stuck UNMAPPED/WAITING.
"""
import re

CURRENCIES = ('USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','HKD','SGD','SEK','NOK','MXN','ZAR','TRY','PLN')
FX = ('USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','HKD','SGD','SEK','NOK','MXN','ZAR','TRY','PLN')

# Capital / IG style epic fragments → (kind, currencies)
_BROKER_EPIC_HINTS = (
    (('USCGC', 'XAUUSD', 'XAUX', 'GOLD'), 'METALS', ['USD']),
    (('USSILV', 'XAGUSD', 'XAG', 'SILVER'), 'METALS', ['USD']),
    (('XPT', 'PLATINUM', 'XPD', 'PALLADIUM'), 'METALS', ['USD']),
    (('OIL_CRUDE', 'OIL_BRENT', 'USOIL', 'UKOIL', 'WTI', 'BRENT', 'NATGAS'),
     'COMMODITIES', ['USD']),
    (('US100', 'US500', 'US30', 'SPX', 'NDX'), 'INDICES', ['USD']),
    (('BTC', 'ETH', 'XRP', 'SOLUSD', 'LTC'), 'CRYPTO', ['USD']),
)


def _broker_hint(code):
    raw = str(code or '').upper().replace(' ', '')
    if not raw:
        return None
    for keys, kind, currencies in _BROKER_EPIC_HINTS:
        if any(k in raw for k in keys):
            return kind, list(currencies)
    return None


def instrument_profile(epic, instrument=None):
    instrument = instrument or {}
    code = str(epic or '').upper()
    name = str(
        instrument.get('name')
        or instrument.get('instrumentName')
        or instrument.get('epic')
        or '').upper()
    full = code + ' ' + name
    declared = str(instrument.get('currency') or instrument.get('currencyCode') or '').upper()
    pair = None
    # E.g. EURUSD, EUR/USD, CS.D.EURUSD.CFD.IP, USD/JPY.
    for match in re.finditer(r'(?<![A-Z])([A-Z]{3})\s*[/_-]?\s*([A-Z]{3})(?![A-Z])', full):
        a,b = match.groups()
        if a in FX and b in FX and a != b:
            pair = (a,b);break
    kind = 'OTHER'; currencies = []
    asset_type=str(instrument.get('type') or instrument.get('instrumentType') or '').upper()
    hint = _broker_hint(code) or _broker_hint(name)
    if any(x in asset_type for x in ('SHARE','STOCK','ETF','EQUITY')):
        kind='EQUITIES';currencies=[declared] if declared in CURRENCIES else []
    elif pair:
        kind = 'FOREX'; currencies = list(pair)
    elif hint:
        kind, currencies = hint
    elif any(re.search(r'(?<![A-Z])'+x+r'(?![A-Z])',full) for x in ('GOLD','XAU','SILVER','XAG','PLATINUM','XPT','PALLADIUM','XPD')):
        kind = 'METALS';currencies = ['USD']
    elif any(x in full for x in ('CRUDE','BRENT','WTI','NATURAL GAS','NATGAS','NATURALGAS','OIL','COPPER','COCOA','WHEAT','CORN','COFFEE')):
        kind = 'COMMODITIES';currencies = ['USD']
    elif any(x in full for x in ('BITCOIN','BTC','ETHEREUM','ETHUSD','ETH/','SOLANA','SOLUSD','XRP','RIPPLE','LITECOIN','LTC')):
        kind = 'CRYPTO';currencies = ['USD']
    elif any(x in full for x in ('NASDAQ','US100','US500','S&P 500','S&P500','DOW','US30','RUSSELL','WALL STREET')):
        kind = 'INDICES';currencies = ['USD']
    elif any(x in full for x in ('DAX','GER40','GERMANY 40','EURO STOXX','EUROSTOXX','FRANCE 40','CAC40')):
        kind = 'INDICES';currencies = ['EUR']
    elif any(x in full for x in ('FTSE','UK100','UK 100')):
        kind = 'INDICES';currencies = ['GBP']
    elif any(x in full for x in ('NIKKEI','JAPAN 225','JP225')):
        kind = 'INDICES';currencies = ['JPY']
    elif any(x in full for x in ('HANG SENG','HONG KONG 50','HK50')):
        kind = 'INDICES';currencies = ['HKD','CNY']
    elif declared in CURRENCIES:
        kind = 'OTHER';currencies = [declared]
    return {'epic':epic,'name':instrument.get('name') or instrument.get('instrumentName') or epic,
            'kind':kind,'currencies':currencies,'mapped':bool(currencies)}
