import time
import requests
from datetime import datetime

# =============================================
# SNIPER BOT v14.0 — SIMPLE Y EFECTIVO
# 5 señales/hora | Min 70% | ORO + BTC
# =============================================

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

TWELVE_API_KEY = '4faa588607814607a01ff11d31e86830'

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10,  'pip': 0.1},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200, 'pip': 1.0},
}

MAX_PER_HOUR  = 5
MIN_PROB      = 70
COOLDOWN      = 300
MAX_SIG_AGE   = 7200

active_signals   = []
hour_signals     = {}   # {hora: count}
last_signal      = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0,    'btc': 0}
last_hour_report = -1
stats = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
last_day_report  = None

def now_str():
    return datetime.now().strftime('%H:%M')

def log(msg):
    print(f'[{now_str()}] {msg}')

def tg_send(chat_id, msg):
    try:
        r = requests.post(TG_API, json={
            'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'
        }, timeout=10)
        return r.json().get('ok', False)
    except:
        return False

def send_alert(msg):
    tg_send(TG_CHAT_ID, msg)
    tg_send(TG_GROUP_ID, msg)

# ── PRECIOS ───────────────────────────────────

def get_prices(symbol, outputsize=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': '5min',
            'outputsize': outputsize, 'apikey': TWELVE_API_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            log(f'Twelve error: {data.get("message")}')
            return None
        values = data.get('values', [])
        return [float(v['close']) for v in reversed(values)] if values else None
    except Exception as e:
        log(f'Twelve error: {e}')
        return None

def get_spot(symbol):
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': symbol, 'apikey': TWELVE_API_KEY}, timeout=8)
        p = float(r.json().get('price', 0))
        return p if p > 0 else None
    except:
        return None

def binance_prices(sym, min_price):
    try:
        r = requests.get(f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=60', timeout=10)
        prices = [float(k[4]) for k in r.json()]
        t = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}', timeout=5).json()
        if 'bidPrice' in t:
            prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
        return prices if prices[-1] > min_price else None
    except:
        return None

def get_gold_prices():
    p = get_prices('XAU/USD')
    if p and p[-1] > 2000:
        s = get_spot('XAU/USD')
        if s and s > 2000: p[-1] = s
        return p
    return binance_prices('XAUUSDT', 2000)

def get_btc_prices():
    p = get_prices('BTC/USD')
    if p and p[-1] > 10000:
        s = get_spot('BTC/USD')
        if s and s > 10000: p[-1] = s
        return p
    return binance_prices('BTCUSDT', 10000)

def get_spot_price(asset_key):
    sym_td = 'XAU/USD' if asset_key == 'gold' else 'BTC/USD'
    p = get_spot(sym_td)
    if p: return p
    try:
        sym_b = 'XAUUSDT' if asset_key == 'gold' else 'BTCUSDT'
        t = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym_b}', timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except: pass
    return None

# ── INDICADORES ───────────────────────────────

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
    return sum(prices[-n:]) / n if len(prices) >= n else sum(prices) / len(prices)

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow+sig+2: return 0, 0
    def sma(d, n): return sum(d[-n:])/n if len(d) >= n else 0
    ao = [sma(prices[:i],fast)-sma(prices[:i],slow) for i in range(slow, len(prices)+1)]
    if len(ao) < sig+1: return 0, 0
    return round(ao[-1]-sma(ao,sig),4), round(ao[-2]-sma(ao[:-1],sig),4)

def calc_prob(prices, direction):
    prob = 40
    rsi = calc_rsi(prices)
    ac_now, ac_prev = calc_ac(prices)
    px = prices[-1]
    ma20 = calc_ma(prices, 20)
    ma5  = calc_ma(prices, 5)

    # RSI
    if direction == 'buy':
        if rsi < 30: prob += 20
        elif rsi < 45: prob += 12
        elif rsi < 55: prob += 6
        elif rsi > 65: prob -= 8
    else:
        if rsi > 70: prob += 20
        elif rsi > 55: prob += 12
        elif rsi > 45: prob += 6
        elif rsi < 35: prob -= 8

    # AC SMO
    if direction == 'buy':
        if ac_now > 0 and ac_now > ac_prev: prob += 12
        elif ac_now > 0: prob += 6
    else:
        if ac_now < 0 and ac_now < ac_prev: prob += 12
        elif ac_now < 0: prob += 6

    # Tendencia MA
    if direction == 'buy' and ma5 > ma20: prob += 10
    elif direction == 'sell' and ma5 < ma20: prob += 10

    # Momentum (3 velas)
    if len(prices) >= 4:
        last3 = prices[-3:]
        up3 = all(last3[i] > last3[i-1] for i in range(1,3))
        dn3 = all(last3[i] < last3[i-1] for i in range(1,3))
        if direction == 'buy'  and up3: prob += 10
        if direction == 'sell' and dn3: prob += 10

    return min(max(round(prob), 35), 97)

def detect_signal(prices):
    rsi = calc_rsi(prices)
    ac_now, _ = calc_ac(prices)
    ma5  = calc_ma(prices, 5)
    ma20 = calc_ma(prices, 20)
    px   = prices[-1]

    candidates = []

    # RSI extremo
    if rsi < 35: candidates.append('buy')
    if rsi > 65: candidates.append('sell')

    # Momentum 3 velas
    if len(prices) >= 4:
        last3 = prices[-3:]
        if all(last3[i] > last3[i-1] for i in range(1,3)):
            candidates.append('buy')
        if all(last3[i] < last3[i-1] for i in range(1,3)):
            candidates.append('sell')

    # MA cruce
    if ma5 > ma20 and rsi < 60: candidates.append('buy')
    if ma5 < ma20 and rsi > 40: candidates.append('sell')

    # Rebote local
    if len(prices) >= 6:
        local_min = min(prices[-6:-1])
        local_max = max(prices[-6:-1])
        if px > local_min and prices[-2] <= local_min * 1.0002:
            candidates.append('buy')
        if px < local_max and prices[-2] >= local_max * 0.9998:
            candidates.append('sell')

    if not candidates: return None

    buys  = candidates.count('buy')
    sells = candidates.count('sell')
    if buys > sells:   return 'buy'
    if sells > buys:   return 'sell'
    return None

# ── SEGUIMIENTO TP/SL ─────────────────────────

def check_active_signals():
    global active_signals
    to_remove = []
    now = time.time()

    for sig in active_signals:
        cfg = ASSETS[sig['asset']]
        is_btc = sig['asset'] == 'btc'
        fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'
        age_min = round((now - sig['time']) / 60)

        if now - sig['time'] > MAX_SIG_AGE:
            to_remove.append(sig)
            continue

        price = get_spot_price(sig['asset'])
        if not price: continue

        hit_tp = (sig['dir'] == 'buy'  and price >= sig['tp']) or \
                 (sig['dir'] == 'sell' and price <= sig['tp'])
        hit_sl = (sig['dir'] == 'buy'  and price <= sig['sl']) or \
                 (sig['dir'] == 'sell' and price >= sig['sl'])

        if hit_tp:
            stats[sig['asset']]['tp'] += 1
            send_alert(
                f'<b>✅ TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                f'{fmt(sig["entry"])} → {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

        elif hit_sl:
            stats[sig['asset']]['sl'] += 1
            send_alert(
                f'<b>❌ SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                f'{fmt(sig["entry"])} → {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

    for s in to_remove:
        if s in active_signals: active_signals.remove(s)

# ── RESUMEN HORARIO ───────────────────────────

def check_hourly_report():
    global last_hour_report
    now = datetime.now()
    if now.minute != 0 or now.hour == last_hour_report:
        return
    last_hour_report = now.hour

    tp = stats['gold']['tp'] + stats['btc']['tp']
    sl = stats['gold']['sl'] + stats['btc']['sl']
    total  = tp + sl
    pct    = round(tp/total*100) if total > 0 else 0
    stars  = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
    hora   = f'{(now.hour-1)%24:02d}:00'

    abiertas = len(active_signals)
    send_alert(
        f'<b>RESUMEN {hora} - {now.hour:02d}:00</b>\n'
        f'✅TP: {tp}  ❌SL: {sl}  🔄Curso: {abiertas}\n'
        f'Efectividad: <b>{pct}%</b> {stars}'
    )

# ── RESUMEN DIARIO ────────────────────────────

def check_daily_report():
    global last_day_report
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour == 23 and now.minute == 0 and last_day_report != today:
        last_day_report = today
        tp_g = stats['gold']['tp']; sl_g = stats['gold']['sl']
        tp_b = stats['btc']['tp'];  sl_b = stats['btc']['sl']
        total_tp = tp_g + tp_b
        total_sl = sl_g + sl_b
        total    = total_tp + total_sl
        pct = round(total_tp/total*100) if total > 0 else 0
        stars = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
        send_alert(
            f'<b>RESUMEN DIA {now.strftime("%d/%m")}</b>\n'
            f'✅TP: {total_tp}  ❌SL: {total_sl}\n'
            f'Efectividad: <b>{pct}%</b> {stars}\n'
            f'🥇 ORO  {tp_g}✅ {sl_g}❌\n'
            f'₿  BTC  {tp_b}✅ {sl_b}❌'
        )
        for k in stats:
            stats[k]['tp'] = 0
            stats[k]['sl'] = 0

# ── ANALIZAR Y ENVIAR ─────────────────────────

def analyze_and_alert(asset_key, prices):
    cfg  = ASSETS[asset_key]
    px   = prices[-1]
    is_btc = asset_key == 'btc'
    fmt  = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'
    hora = datetime.now().hour

    # Control 5 señales por hora
    count_hora = hour_signals.get(hora, 0)
    if count_hora >= MAX_PER_HOUR:
        log(f'  {cfg["icon"]} Limite {MAX_PER_HOUR}/hora alcanzado')
        return

    # Detectar señal
    sig = detect_signal(prices)
    if not sig:
        log(f'{cfg["icon"]} {fmt(px)} → sin señal')
        return

    # Probabilidad
    prob = calc_prob(prices, sig)
    log(f'{cfg["icon"]} {fmt(px)} → {sig.upper()} {prob}%')

    if prob < MIN_PROB:
        log(f'  {prob}% < {MIN_PROB}%, skip')
        return

    # Cooldown
    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if last_signal[asset_key] == sig_key and time.time() - last_signal_time[asset_key] < COOLDOWN:
        log('  Cooldown activo')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()
    hour_signals[hora] = count_hora + 1

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    # Lotaje recomendado
    if prob >= 88:   lote_rec, lote_ico = '0.10 - 0.15', '💪'
    elif prob >= 78: lote_rec, lote_ico = '0.05 - 0.10', '👍'
    else:            lote_rec, lote_ico = '0.01 - 0.05', '👌'

    # Calcular ganancias por lotaje
    # XAU: 0.01 lot = $1 por cada $1 de movimiento en precio
    # TP=20 → ganancia = lot * 100 * tp * pip_value
    lots_txt = '\n'.join([
        f'  {lot:.2f} → +{round(cfg["tp"] * lot * 100 * cfg["pip"]):.0f}€'
        for lot in LOTAJES
    ])

    tipo = 'BUY' if isBuy else 'SELL'
    num  = hour_signals[hora]

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {prob}%</b>\n'
        f'Entrada: {fmt(px)}\n'
        f'🎯 TP: {fmt(tp)} (+{cfg["tp"]})\n'
        f'🛑 SL: {fmt(sl)} (-{cfg["sl"]})\n'
        f'━━━━━━━━━━━━\n'
        f'{lote_ico} Lote rec: {lote_rec}\n'
        f'{lots_txt}\n'
        f'━━━━━━━━━━━━\n'
        f'Señal {num}/{MAX_PER_HOUR} | {now_str()}'
    )
    log(f'✅ {tipo} {fmt(px)} {prob}% — señal {num}/{MAX_PER_HOUR}')

    active_signals.append({
        'asset': asset_key, 'dir': sig,
        'entry': px, 'tp': tp, 'sl': sl,
        'time': time.time(),
    })

# ── MAIN ──────────────────────────────────────

def main():
    log('SNIPER BOT v14.0 INICIANDO')
    send_alert(
        '<b>🤖 SNIPER BOT v14.0 ACTIVO</b>\n'
        '🥇 ORO  🎯+20 🛑-10\n'
        '₿  BTC  🎯+500 🛑-200\n'
        '✅ Min 70% | 5 señales/hora\n'
        '🔔 TP/SL | Resumen horario | 23:00'
    )

    while True:
        try:
            check_hourly_report()
            check_daily_report()
            check_active_signals()

            gold_p = get_gold_prices()
            if gold_p: analyze_and_alert('gold', gold_p)
            else: log('Sin datos ORO')

            time.sleep(3)

            btc_p = get_btc_prices()
            if btc_p: analyze_and_alert('btc', btc_p)
            else: log('Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('🛑 SNIPER BOT Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
