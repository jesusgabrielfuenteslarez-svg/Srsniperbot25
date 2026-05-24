import time
import requests
from datetime import datetime

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

TWELVE_API_KEY = 'TU_API_KEY_AQUI'

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]
ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10,  'val_pto': 1.0},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200, 'val_pto': 0.01},
}

active_signals   = []
last_signal      = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0,    'btc': 0}
last_news_alert  = 0
COOLDOWN       = 300
NEWS_COOLDOWN  = 1800
MAX_SIGNAL_AGE = 7200

# ── Estadísticas del día ──────────────────────
stats = {
    'gold': {'tp': 0, 'sl': 0},
    'btc':  {'tp': 0, 'sl': 0},
}
last_summary_date = None   # controla que el resumen salga 1 vez/día
SUMMARY_HOUR = 23          # hora a la que se envía el resumen (23:00)
SUMMARY_MIN  = 0

def reset_stats():
    for k in stats:
        stats[k]['tp'] = 0
        stats[k]['sl'] = 0

def send_daily_summary():
    tp_gold = stats['gold']['tp']
    sl_gold = stats['gold']['sl']
    tp_btc  = stats['btc']['tp']
    sl_btc  = stats['btc']['sl']
    total_tp = tp_gold + tp_btc
    total_sl = sl_gold + sl_btc
    total    = total_tp + total_sl
    pct = round((total_tp / total) * 100) if total > 0 else 0

    if   pct == 100: stars = '🌟🌟🌟'
    elif pct >= 80:  stars = '🌟🌟'
    elif pct >= 60:  stars = '🌟'
    else:            stars = ''

    msg = (
        f'📊 <b>RESUMEN {datetime.now().strftime("%d/%m")}</b>\n'
        f'━━━━━━━━━━━━\n'
        f'✅ TP: {total_tp}   ❌ SL: {total_sl}\n'
        f'📈 Efectividad: <b>{pct}%</b> {stars}\n'
        f'🔢 Total: {total} señales\n'
        f'━━━━━━━━━━━━\n'
        f'🥇 ORO  {tp_gold}✅ {sl_gold}❌\n'
        f'₿  BTC  {tp_btc}✅ {sl_btc}❌'
    )
    send_alert(msg)
    log(f'Resumen diario enviado: {pct}% ({total_tp}/{total})')

# ─────────────────────────────────────────────

def now_str():
    return datetime.now().strftime('%H:%M')

def log(msg):
    print(f'[{now_str()}] {msg}')

def tg_send(chat_id, msg):
    try:
        r = requests.post(TG_API, json={'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        return r.json().get('ok', False)
    except:
        return False

def send_alert(msg):
    tg_send(TG_CHAT_ID, msg)
    tg_send(TG_GROUP_ID, msg)

def get_twelve_prices(symbol, outputsize=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': '5min', 'outputsize': outputsize, 'apikey': TWELVE_API_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            log(f'Twelve error [{symbol}]: {data.get("message")}')
            return None
        values = data.get('values', [])
        return [float(v['close']) for v in reversed(values)] if values else None
    except Exception as e:
        log(f'Twelve excepcion [{symbol}]: {e}')
        return None

def get_twelve_spot(symbol):
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': symbol, 'apikey': TWELVE_API_KEY}, timeout=8)
        price = float(r.json().get('price', 0))
        return price if price > 0 else None
    except:
        return None

def binance_klines(sym):
    r = requests.get(f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=60', timeout=10)
    prices = [float(k[4]) for k in r.json()]
    t = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}', timeout=5).json()
    if 'bidPrice' in t:
        prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
    return prices

def get_gold_prices():
    prices = get_twelve_prices('XAU/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 2000:
        spot = get_twelve_spot('XAU/USD')
        if spot and spot > 2000: prices[-1] = spot
        return prices
    try:
        p = binance_klines('XAUUSDT')
        if p and p[-1] > 2000: return p
    except: pass
    return None

def get_btc_prices():
    prices = get_twelve_prices('BTC/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 10000:
        spot = get_twelve_spot('BTC/USD')
        if spot and spot > 10000: prices[-1] = spot
        return prices
    try:
        p = binance_klines('BTCUSDT')
        if p and p[-1] > 10000: return p
    except: pass
    return None

def get_spot_price(asset_key):
    symbol = 'XAU/USD' if asset_key == 'gold' else 'BTC/USD'
    price = get_twelve_spot(symbol)
    if price: return price
    try:
        sym = 'XAUUSDT' if asset_key == 'gold' else 'BTCUSDT'
        t = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}', timeout=5).json()
        if 'bidPrice' in t: return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except: pass
    return None

def check_news():
    global last_news_alert
    try:
        r = requests.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=10)
        events, high_impact = r.json(), []
        for ev in events:
            try:
                if ev.get('impact','') not in ['High','high','3']: continue
                if ev.get('country','').upper() not in ['USD','EUR','GBP']: continue
                title = ev.get('title','')
                if any(k in title.lower() for k in ['fed','fomc','rate','inflation','cpi','nfp','gdp','powell','pce','ppi']):
                    high_impact.append(f"{ev.get('country','').upper()} {ev.get('time','')} - {title}")
            except: continue
        if high_impact and time.time() - last_news_alert > NEWS_COOLDOWN:
            last_news_alert = time.time()
            txt = '\n'.join([f'- {e}' for e in high_impact[:3]])
            send_alert(f'<b>NOTICIAS ALTO IMPACTO</b>\nNO OPERAR AHORA\n\n{txt}\n\nEspera 30 min')
    except Exception as e:
        log(f'News error: {e}')

def check_daily_summary():
    global last_summary_date
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour == SUMMARY_HOUR and now.minute == SUMMARY_MIN:
        if last_summary_date != today:
            last_summary_date = today
            send_daily_summary()
            reset_stats()

def calc_rsi(prices, n=14):
    if len(prices) < n+1: return 50
    g = l = 0
    for i in range(len(prices)-n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/n, l/n
    if al == 0: return 100
    return round(100 - (100/(1+ag/al)), 1)

def calc_ma(prices, n):
    s = prices[-min(n,len(prices)):]
    return sum(s)/len(s)

def calc_mom(prices, n=5):
    return prices[-1] - prices[-1-n] if len(prices) >= n+1 else 0

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow+sig+2: return 0, 0
    def sma(d, n): return sum(d[-n:])/n if len(d) >= n else 0
    ao = [sma(prices[:i],fast)-sma(prices[:i],slow) for i in range(slow,len(prices)+1)]
    if len(ao) < sig+1: return 0, 0
    return round(ao[-1]-sma(ao,sig),4), round(ao[-2]-sma(ao[:-1],sig),4)

def detect_range(prices):
    if len(prices) < 20: return {'is_range':False,'high':0,'low':0,'size':0}
    recent = prices[-20:]
    high, low = max(recent), min(recent)
    size = high - low
    return {'is_range': abs(calc_ma(prices,5)-calc_ma(prices,20)) < size*0.3 and size>0,
            'high': high, 'low': low, 'size': size}

def calc_prob(prices, direction, range_info):
    prob, px = 40, prices[-1]
    rsi = calc_rsi(prices)
    ac_now, ac_prev = calc_ac(prices)
    size = range_info['size'] or 1
    if range_info['is_range']: prob += 12
    if direction == 'buy':
        dist = (px - range_info['low']) / size
        if dist < 0.20: prob += 20
        elif dist < 0.35: prob += 10
        if rsi < 30: prob += 18
        elif rsi < 40: prob += 10
        elif rsi < 50: prob += 5
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0: prob += 5
        if calc_mom(prices) > 0: prob += 5
    else:
        dist = (range_info['high'] - px) / size
        if dist < 0.20: prob += 20
        elif dist < 0.35: prob += 10
        if rsi > 70: prob += 18
        elif rsi > 60: prob += 10
        elif rsi > 50: prob += 5
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0: prob += 5
        if calc_mom(prices) < 0: prob += 5
    return min(max(round(prob),35),97)

def check_active_signals():
    global active_signals
    if not active_signals: return
    to_remove = []
    now = time.time()
    for sig in active_signals:
        cfg = ASSETS[sig['asset']]
        is_btc = sig['asset'] == 'btc'
        fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'
        age_min = round((now - sig['time']) / 60)
        if now - sig['time'] > MAX_SIGNAL_AGE:
            send_alert(f'EXPIRADA {cfg["icon"]} {cfg["name"]}\n{fmt(sig["entry"])} sin resultado (2h)\n{now_str()}')
            to_remove.append(sig)
            continue
        price = get_spot_price(sig['asset'])
        if not price: continue
        hit_tp = (sig['direction']=='buy'  and price >= sig['tp']) or \
                 (sig['direction']=='sell' and price <= sig['tp'])
        hit_sl = (sig['direction']=='buy'  and price <= sig['sl']) or \
                 (sig['direction']=='sell' and price >= sig['sl'])
        if hit_tp:
            stats[sig['asset']]['tp'] += 1
            send_alert(
                f'<b>TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)
            log(f'TP {cfg["name"]} {fmt(price)}')
        elif hit_sl:
            stats[sig['asset']]['sl'] += 1
            send_alert(
                f'<b>SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)
            log(f'SL {cfg["name"]} {fmt(price)}')
    for s in to_remove:
        if s in active_signals: active_signals.remove(s)
    if active_signals:
        log(f'Activas: {len(active_signals)}')

def analyze_and_alert(asset_key, prices):
    cfg = ASSETS[asset_key]
    px = prices[-1]
    rsi = calc_rsi(prices)
    range_info = detect_range(prices)
    ac_now, ac_prev = calc_ac(prices)
    is_btc = asset_key == 'btc'
    fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'

    sig = None
    if range_info['is_range'] and range_info['size'] > 0:
        sz = range_info['size']
        if (px - range_info['low']) / sz < 0.25: sig = 'buy'
        elif (range_info['high'] - px) / sz < 0.25: sig = 'sell'
    if not sig:
        if rsi < 30: sig = 'buy'
        elif rsi > 70: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} AC:{ac_now:.3f} -> {sig or "sin senal"}')
    if not sig: return

    prob = calc_prob(prices, sig, range_info)
    if prob < 75:
        log(f'  {prob}% < 75%, skip')
        return

    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if last_signal[asset_key] == sig_key and time.time() - last_signal_time[asset_key] < COOLDOWN:
        log('  Cooldown')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC+'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC-'
    else:                                  ac_txt = 'AC~'

    lots_txt = '\n'.join([
        f'{lot:.2f} +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01, 2):.2f}EUR'
        for lot in LOTAJES
    ])

    tipo = 'BUY' if isBuy else 'SELL'

    msg = (
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {prob}%</b>\n'
        f'RSI {rsi} | {ac_txt}\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} (+{cfg["tp"]})\n'
        f'SL: {fmt(sl)} (-{cfg["sl"]})\n'
        f'---\n'
        f'{lots_txt}\n'
        f'{now_str()}'
    )

    send_alert(msg)
    log(f'{tipo} {fmt(px)} {prob}%')
    active_signals.append({
        'asset': asset_key, 'direction': sig,
        'entry': px, 'tp': tp, 'sl': sl, 'time': time.time(),
    })
    log(f'Activas: {len(active_signals)}')

def main():
    log('SNIPER BOT v11.3 INICIANDO')
    send_alert(
        '<b>SNIPER BOT v11.3 ACTIVO</b>\n'
        'ORO TP+20 SL-10 | BTC TP+500 SL-200\n'
        '+75% | AC SMO | TP/SL ON | Resumen diario 23:00'
    )

    scan = 0
    while True:
        try:
            scan += 1
            if scan % 10 == 0: check_news()
            check_active_signals()
            check_daily_summary()

            gold_p = get_gold_prices()
            if gold_p: analyze_and_alert('gold', gold_p)
            else: log('Sin datos ORO')

            time.sleep(3)

            btc_p = get_btc_prices()
            if btc_p: analyze_and_alert('btc', btc_p)
            else: log('Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('SNIPER BOT Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
