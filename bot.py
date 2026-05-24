import time
import requests
from datetime import datetime

# =============================================
# SNIPER BOT v11 FINAL
# - Precio desde Twelve Data (= MT4/MT5)
# - Fallback a Binance
# - Alertas noticias económicas
# - Mensaje Telegram corto y directo
# =============================================

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

# ─────────────────────────────────────────────
# ⚠️  PASO OBLIGATORIO:
#   1. Regístrate GRATIS en https://twelvedata.com
#   2. Copia tu API Key y pégala aquí abajo
#   3. El plan free da 800 llamadas/día — más que suficiente
# ─────────────────────────────────────────────
TWELVE_API_KEY = '4faa588607814607a01ff11d31e86830'

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20, 'sl': 10, 'val_pto': 1.0},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500,'sl': 200,'val_pto': 0.01},
}

last_signal      = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0,    'btc': 0}
last_news_alert  = 0
COOLDOWN         = 300   # 5 min entre señales iguales
NEWS_COOLDOWN    = 1800  # 30 min entre alertas de noticias

def now_str():
    return datetime.now().strftime('%H:%M')

def log(msg):
    print(f'[{now_str()}] {msg}')

# ---- TELEGRAM ----
def tg_send(chat_id, msg):
    try:
        r = requests.post(TG_API, json={
            'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'
        }, timeout=10)
        return r.json().get('ok', False)
    except:
        return False

def send_alert(msg):
    ok1 = tg_send(TG_CHAT_ID, msg)
    ok2 = tg_send(TG_GROUP_ID, msg)
    log(f'TG Jesús:{ok1} Grupo:{ok2}')
    return ok1 or ok2

# ============================================
# PRECIOS — Twelve Data (= MT4/MT5)
# ============================================

def get_twelve_prices(symbol, outputsize=60):
    """
    Twelve Data: misma fuente de precios que la mayoría de brokers MT4/MT5.
    Gratis hasta 800 llamadas/día. Registro en twelvedata.com
    """
    try:
        r = requests.get(
            'https://api.twelvedata.com/time_series',
            params={
                'symbol': symbol,
                'interval': '5min',
                'outputsize': outputsize,
                'apikey': TWELVE_API_KEY,
            },
            timeout=12
        )
        data = r.json()

        if data.get('status') == 'error':
            log(f'Twelve Data error [{symbol}]: {data.get("message")}')
            return None

        values = data.get('values', [])
        if not values:
            return None

        # values viene orden desc (reciente primero) → invertir
        closes = [float(v['close']) for v in reversed(values)]
        log(f'Twelve Data {symbol}: {closes[-1]:.2f}')
        return closes

    except Exception as e:
        log(f'Twelve Data excepción [{symbol}]: {e}')
        return None

def get_twelve_spot(symbol):
    """Precio spot actual desde Twelve Data."""
    try:
        r = requests.get(
            'https://api.twelvedata.com/price',
            params={'symbol': symbol, 'apikey': TWELVE_API_KEY},
            timeout=8
        )
        price = float(r.json().get('price', 0))
        return price if price > 0 else None
    except Exception as e:
        log(f'Twelve spot error [{symbol}]: {e}')
        return None

# ---- ORO ----
def get_gold_prices():
    """XAU/USD — precio idéntico a MT4/MT5"""

    # 1. Twelve Data (fuente principal)
    prices = get_twelve_prices('XAU/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 2000:
        spot = get_twelve_spot('XAU/USD')
        if spot and spot > 2000:
            prices[-1] = spot
        return prices

    # 2. Fallback: Binance XAUUSDT
    try:
        r = requests.get(
            'https://api.binance.com/api/v3/klines?symbol=XAUUSDT&interval=5m&limit=60',
            timeout=10
        )
        if r.status_code == 200:
            prices = [float(k[4]) for k in r.json()]
            t = requests.get(
                'https://api.binance.com/api/v3/ticker/bookTicker?symbol=XAUUSDT',
                timeout=5
            ).json()
            if 'bidPrice' in t:
                prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
            if prices and prices[-1] > 2000:
                log(f'ORO Binance (fallback): {prices[-1]:.2f}')
                return prices
    except Exception as e:
        log(f'ORO Binance error: {e}')

    log('⚠️ Sin datos ORO')
    return None

# ---- BTC ----
def get_btc_prices():
    """BTC/USD — precio idéntico a MT4/MT5"""

    # 1. Twelve Data (fuente principal)
    prices = get_twelve_prices('BTC/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 10000:
        spot = get_twelve_spot('BTC/USD')
        if spot and spot > 10000:
            prices[-1] = spot
        return prices

    # 2. Fallback: Binance BTCUSDT
    try:
        r = requests.get(
            'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=60',
            timeout=10
        )
        prices = [float(k[4]) for k in r.json()]
        t = requests.get(
            'https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT',
            timeout=5
        ).json()
        if 'bidPrice' in t:
            prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
        if prices and prices[-1] > 10000:
            log(f'BTC Binance (fallback): {prices[-1]:,.0f}')
            return prices
    except Exception as e:
        log(f'BTC error: {e}')

    log('⚠️ Sin datos BTC')
    return None

# ---- NOTICIAS ECONÓMICAS ----
def check_news():
    """Verificar noticias de alto impacto que afectan al mercado"""
    global last_news_alert
    try:
        r = requests.get(
            'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
            timeout=10
        )
        events = r.json()
        high_impact = []

        for event in events:
            try:
                if event.get('impact', '') not in ['High', 'high', '3']:
                    continue
                if event.get('country', '').upper() not in ['USD', 'EUR', 'GBP']:
                    continue
                title = event.get('title', '')
                keywords = ['fed', 'fomc', 'rate', 'inflation', 'cpi', 'nfp',
                           'gdp', 'employment', 'powell', 'interest', 'jobs',
                           'pce', 'ppi', 'retail']
                if any(k in title.lower() for k in keywords):
                    high_impact.append({
                        'title': title,
                        'time': event.get('time', ''),
                        'currency': event.get('country', '').upper()
                    })
            except:
                continue

        if high_impact and time.time() - last_news_alert > NEWS_COOLDOWN:
            last_news_alert = time.time()
            events_txt = '\n'.join([
                f"• {e['currency']} {e['time']} — {e['title']}"
                for e in high_impact[:3]
            ])
            msg = (
                f'⚠️ <b>NOTICIAS DE ALTO IMPACTO</b>\n'
                f'━━━━━━━━━━━━━━\n'
                f'🚨 HAY NOTICIAS QUE PUEDEN MOVER EL MERCADO\n'
                f'NO ABRAS OPERACIONES AHORA\n\n'
                f'{events_txt}\n'
                f'━━━━━━━━━━━━━━\n'
                f'⏳ Espera 30 min tras la noticia'
            )
            send_alert(msg)
            log(f'⚠️ Alerta noticias: {len(high_impact)} eventos')
            return True

    except Exception as e:
        log(f'News error: {e}')
    return False

# ---- INDICADORES ----
def calc_rsi(prices, n=14):
    if len(prices) < n + 1: return 50
    g = l = 0
    for i in range(len(prices) - n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/n, l/n
    if al == 0: return 100
    return round(100 - (100 / (1 + ag/al)), 1)

def calc_ma(prices, n):
    s = prices[-min(n, len(prices)):]
    return sum(s) / len(s)

def calc_mom(prices, n=5):
    if len(prices) < n+1: return 0
    return prices[-1] - prices[-1-n]

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow + sig + 2: return 0, 0
    def sma(d, n): return sum(d[-n:])/n if len(d) >= n else 0
    ao = [sma(prices[:i], fast) - sma(prices[:i], slow)
          for i in range(slow, len(prices)+1)]
    if len(ao) < sig + 1: return 0, 0
    return round(ao[-1] - sma(ao, sig), 4), round(ao[-2] - sma(ao[:-1], sig), 4)

def detect_range(prices):
    if len(prices) < 20:
        return {'is_range': False, 'high': 0, 'low': 0, 'size': 0}
    recent = prices[-20:]
    high, low = max(recent), min(recent)
    size = high - low
    is_range = abs(calc_ma(prices,5) - calc_ma(prices,20)) < size * 0.3 and size > 0
    return {'is_range': is_range, 'high': high, 'low': low, 'size': size}

def calc_prob(prices, direction, range_info):
    prob = 40
    px = prices[-1]
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

    return min(max(round(prob), 35), 97)

# ---- ANALIZAR Y ALERTAR ----
def analyze_and_alert(asset_key, prices):
    cfg = ASSETS[asset_key]
    px = prices[-1]
    rsi = calc_rsi(prices)
    range_info = detect_range(prices)
    ac_now, ac_prev = calc_ac(prices)
    fmt = lambda v: f'{round(v):,}' if asset_key == 'btc' else f'{v:.2f}'

    sig = None
    if range_info['is_range'] and range_info['size'] > 0:
        sz = range_info['size']
        if (px - range_info['low']) / sz < 0.25:
            sig = 'buy'
        elif (range_info['high'] - px) / sz < 0.25:
            sig = 'sell'
    if not sig:
        if rsi < 30: sig = 'buy'
        elif rsi > 70: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} AC:{ac_now:.3f} → {sig or "sin señal"}')
    if not sig: return

    prob = calc_prob(prices, sig, range_info)
    if prob < 75:
        log(f'  Prob {prob}% < 75%, skip')
        return

    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if last_signal[asset_key] == sig_key and time.time() - last_signal_time[asset_key] < COOLDOWN:
        log(f'  Cooldown activo')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = '🟢'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = '🔴'
    else:                                  ac_txt = '🟡'

    lots_txt = '\n'.join([
        f'{lot:.2f} → +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01, 2):.2f}€'
        for lot in LOTAJES
    ])

    emoji = '💚' if isBuy else '🩷'
    tipo  = 'BUY' if isBuy else 'SELL'

    msg = (
        f'{emoji} <b>{tipo} {cfg["icon"]} {cfg["name"]}</b>\n'
        f'📊 <b>{prob}%</b> | RSI {rsi} | AC {ac_txt}\n'
        f'━━━━━━━━━━━━\n'
        f'📍 <b>{fmt(px)}</b>\n'
        f'🎯 {fmt(tp)}  (+{cfg["tp"]})\n'
        f'🛑 {fmt(sl)}  (-{cfg["sl"]})\n'
        f'━━━━━━━━━━━━\n'
        f'{lots_txt}\n'
        f'🕐 {now_str()}'
    )

    if send_alert(msg):
        log(f'✅ {tipo} {fmt(px)} {prob}%')

# ---- MAIN ----
def main():
    log('SNIPER BOT v11 — INICIANDO')

    send_alert(
        '🤖 <b>SNIPER BOT v11 — ACTIVO 24/7</b>\n'
        '━━━━━━━━━━━━\n'
        '🥇 ORO  TP+20 / SL-10\n'
        '₿  BTC  TP+500 / SL-200\n'
        '✅ Solo +75% | ⚡ AC SMO\n'
        '📰 Alerta noticias ON\n'
        '💹 Precio = Twelve Data (MT4/MT5)\n'
        '━━━━━━━━━━━━\n'
        '🚀 ¡Listo!'
    )

    scan = 0
    while True:
        try:
            scan += 1
            log(f'--- Escaneo #{scan} ---')

            if scan % 10 == 0:
                check_news()

            gold_p = get_gold_prices()
            if gold_p:
                analyze_and_alert('gold', gold_p)
            else:
                log('⚠️ Sin datos ORO')

            time.sleep(3)

            btc_p = get_btc_prices()
            if btc_p:
                analyze_and_alert('btc', btc_p)
            else:
                log('⚠️ Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('⛔ <b>SNIPER BOT</b> — Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
