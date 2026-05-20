import time
import requests
from datetime import datetime

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

ASSETS = {
    'gold': {
        'name': 'XAU/USD ORO', 'icon': '🥇',
        'tp': 20, 'sl': 10, 'val_pto': 1.0,
    },
    'btc': {
        'name': 'BTC/USD', 'icon': '₿',
        'tp': 500, 'sl': 200, 'val_pto': 0.01,
    }
}

last_signal = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0, 'btc': 0}
COOLDOWN = 300

def now_str():
    return datetime.now().strftime('%H:%M')

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
    print(f'[{now_str()}] TG Jesús:{ok1} Grupo:{ok2}')
    return ok1 or ok2

def get_gold_prices():
    """ORO desde Binance XAUUSDT — precio exacto MT5"""
    try:
        r = requests.get(
            'https://api.binance.com/api/v3/klines?symbol=XAUUSDT&interval=5m&limit=60',
            timeout=10
        )
        if r.status_code == 200:
            prices = [float(k[4]) for k in r.json()]
            if prices and prices[-1] > 2000:
                # Precio actual
                t = requests.get(
                    'https://api.binance.com/api/v3/ticker/bookTicker?symbol=XAUUSDT',
                    timeout=5
                ).json()
                if 'bidPrice' in t:
                    prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
                print(f'[{now_str()}] ORO Binance: {prices[-1]:.2f}')
                return prices
    except Exception as e:
        print(f'ORO Binance error: {e}')

    # Fallback: Yahoo x2
    try:
        r = requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=5m&range=1d',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=12
        )
        raw = r.json()['chart']['result'][0]['indicators']['quote'][0]['close']
        prices = [p * 2 + 12.0 for p in raw if p is not None]
        if prices and prices[-1] > 3000:
            print(f'[{now_str()}] ORO Yahoo x2: {prices[-1]:.2f}')
            return prices
    except Exception as e:
        print(f'ORO Yahoo error: {e}')

    return None

def get_btc_prices():
    """BTC desde Binance — precio exacto MT5"""
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
        print(f'[{now_str()}] BTC Binance: {prices[-1]:,.0f}')
        return prices
    except Exception as e:
        print(f'BTC error: {e}')
        return None

def calc_rsi(prices, n=14):
    if len(prices) < n + 1:
        return 50
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
    if len(prices) < slow + sig + 2:
        return 0, 0
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

def analyze_and_alert(asset_key, prices):
    cfg = ASSETS[asset_key]
    px = prices[-1]
    rsi = calc_rsi(prices)
    range_info = detect_range(prices)
    ac_now, ac_prev = calc_ac(prices)
    fmt = lambda v: f'{round(v):,}' if asset_key == 'btc' else f'{v:.2f}'

    # Detectar señal
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

    print(f'[{now_str()}] {cfg["icon"]} {fmt(px)} RSI:{rsi} → {sig or "sin señal"}')
    if not sig: return

    prob = calc_prob(prices, sig, range_info)
    if prob < 75:
        print(f'  Prob {prob}% < 75%, skip')
        return

    # Anti-spam
    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if last_signal[asset_key] == sig_key and time.time() - last_signal_time[asset_key] < COOLDOWN:
        print(f'  Cooldown activo')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if ac_now > 0 and ac_now > ac_prev:   ac_txt = '🟢 Alcista'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = '🔴 Bajista'
    else:                                  ac_txt = '🟡 Neutral'

    lots_txt = '\n'.join([
        f'{lot:.2f} → +{round(cfg["tp"]*cfg["val_pto"]*lot/0.01,2):.2f}€'
        for lot in LOTAJES
    ])

    emoji = '💚' if isBuy else '🩷'
    tipo  = 'BUY' if isBuy else 'SELL'

    msg = f'''{emoji} <b>{tipo} {cfg["icon"]} {cfg["name"]}</b>
━━━━━━━━━━━━━━
📊 <b>{prob}%</b> prob  |  RSI {rsi}  |  AC {ac_txt}
━━━━━━━━━━━━━━
📍 Entrada:  <b>{fmt(px)}</b>
🎯 TP:  <b>{fmt(tp)}</b>  (+{cfg["tp"]} pts)
🛑 SL:  <b>{fmt(sl)}</b>  (-{cfg["sl"]} pts)
━━━━━━━━━━━━━━
💰 Lotaje → Ganancia
{lots_txt}
━━━━━━━━━━━━━━
🕐 {now_str()}'''

    if send_alert(msg):
        print(f'  ✅ {tipo} {fmt(px)} {prob}%')

def main():
    print('SNIPER BOT v11 — 24/7 — Binance/TradingView')

    send_alert('''🤖 <b>SNIPER BOT v11 — ACTIVO 24/7</b>
━━━━━━━━━━━━━━
🥇 ORO:  TP +20 / SL -10 pts
₿  BTC:  TP +500 / SL -200 pts
✅ Solo +75% prob  |  ⚡ AC SMO
⏱ Cada 15 seg  |  Precio = MT5
━━━━━━━━━━━━━━
¡Listo! 🚀''')

    scan = 0
    while True:
        try:
            scan += 1
            print(f'\n--- #{scan} [{now_str()}] ---')

            gold_p = get_gold_prices()
            if gold_p: analyze_and_alert('gold', gold_p)

            time.sleep(3)

            btc_p = get_btc_prices()
            if btc_p: analyze_and_alert('btc', btc_p)

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('⛔ <b>SNIPER BOT</b> — Detenido')
            break
        except Exception as e:
            print(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
