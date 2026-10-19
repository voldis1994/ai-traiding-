"""Best-effort cross-asset classification of a broker instrument.

Never invent a currency mapping for an unknown symbol: the news guard then
reports UNMAPPED_INSTRUMENT instead of claiming a false all-clear.
"""
import re

CURRENCIES = ('USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','HKD','SGD','SEK','NOK','MXN','ZAR','TRY','PLN')
FX = ('USD','EUR','GBP','JPY','CHF','CAD','AUD','NZD','CNY','HKD','SGD','SEK','NOK','MXN','ZAR','TRY','PLN')


def instrument_profile(epic, instrument=None):
    instrument = instrument or {}
    code = str(epic or '').upper()
    name = str(instrument.get('name') or '').upper()
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
    if any(x in asset_type for x in ('SHARE','STOCK','ETF','EQUITY')):
        kind='EQUITIES';currencies=[declared] if declared in CURRENCIES else []
    elif pair:
        kind = 'FOREX'; currencies = list(pair)
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
    return {'epic':epic,'name':instrument.get('name') or epic,
            'kind':kind,'currencies':currencies,'mapped':bool(currencies)}
