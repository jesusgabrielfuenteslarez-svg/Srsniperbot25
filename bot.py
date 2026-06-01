import os, time, json, requests
from datetime import datetime, timezone
from collections import deque

# =============================================
# SNIPER BOT v13.0
# Filtro de alta probabilidad — ORO y BTC
# Telegram: solo Entrada, TP, SL y resultado
# =============================================

TG_TOKEN    = os.environ.get('TG_TOKEN',    '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ')
TG_CHAT_ID  = os.environ.get('TG_CHAT_ID',  '6467338067')
TG_GROUP_ID = os.environ.get('TG_GROUP_ID', '-5123266724')
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'

ASSETS = {
    'gold': {'icon': '🥇', 'label': 'ORO',     'tp': 10,  'sl': 5},
    'btc':  {'icon': '₿',  'label': 'BITCOIN',  'tp': 250, 'sl': 125},
}

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]
GAINS = {
    'gold': {l: int(10  * l * 100) for l in LOTAJES},
    'btc':  {l: int(250 * l * 0.1 * 10) for l in LOTAJES},
}

# Umbrales internos — el usuario no los ve
SCORE_MIN = 55   # umbral fijo — siempre igual

LOG_FILE       = '/tmp/sniper_log.json'

# ── Estado interno ──────────────────────────
locked        = {'gold': False, 'btc': False}
active_sigs   = []
stats         = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
trade_history = deque(maxlen=40)
trade_log     = []
daily_pnl     = 0.0

last_d_report = None

# =============================================
# UTILIDADES
# =============================================

def now_str():
    return datetime.now().strftime('%H:%M')

def log(m):
    print(f'[{now_str()}] {m}', flush=True)

def send(msg):
    for chat in [TG_CHAT_ID, TG_GROUP_ID]:
        try:
            requests.post(TG_API, json={
                'chat_id': chat, 'text': msg
            }, timeout=10)
        except:
            pass

def fmt_gold(v):  return f'{v:.2f}'
def fmt_btc(v):   return f'{round(v):,}'

def save_log():
    try:
        with open(LOG_FILE, 'w') as f:
            json.dump(trade_log[-300:], f)
    except:
        pass

# =============================================
# PRECIOS
# =============================================

def candles_twelve(symbol, interval='5min', size=50):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': interval,
            'outputsize': size, 'apikey': TWELVE_KEY,
        }, timeout=12)
        d = r.json()
        if d.get('status') == 'error': return None
        vals = d.get('values', [])
        if not vals: return None
        return {
            'c': [float(v['close'])  for v in reversed(vals)],
            'h': [float(v['high'])   for v in reversed(vals)],
            'l': [float(v['low'])    for v in reversed(vals)],
            'v': [float(v.get('volume', 0)) for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Twelve {symbol}: {e}')
        return None

def candles_binance(sym, min_px):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=50',
            timeout=8)
        k = r.json()
        return {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        } if float(k[-1][4]) > min_px else None
    except:
        return None

def gold_data(tf='5min'):
    d = candles_twelve('XAU/USD', tf)
    if d and d['c'][-1] > 2000: return d
    return candles_binance('XAUUSDT', 2000) if tf == '5min' else None

def btc_data(tf='5min'):
    d = candles_twelve('BTC/USD', tf)
    if d and d['c'][-1] > 10000: return d
    return candles_binance('BTCUSDT', 10000) if tf == '5min' else None

def get_spot(asset):
    sym = 'XAU/USD' if asset == 'gold' else 'BTC/USD'
    try:
        p = float(requests.get('https://api.twelvedata.com/price',
            params={'symbol': sym, 'apikey': TWELVE_KEY}, timeout=6
        ).json().get('price', 0))
        if p > 0: return p
    except: pass
    try:
        s = 'XAUUSDT' if asset == 'gold' else 'BTCUSDT'
        t = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={s}',
            timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except: pass
    return None

# =============================================
# INDICADORES
# =============================================

def rsi(c, n=14):
    if len(c) < n+1: return 50
    g = l = 0
    for i in range(len(c)-n, len(c)):
        d = c[i]-c[i-1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g/n, l/n
    return round(100 - 100/(1+ag/al), 1) if al else 100

def ema(c, n):
    if len(c) < n: return c[-1]
    k = 2/(n+1); e = c[0]
    for p in c[1:]: e = p*k + e*(1-k)
    return e

def atr(h, l, c, n=14):
    if len(c) < n+1: return 1
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# ANÁLISIS INTERNO — todo aquí, nada al usuario
# =============================================

def analyze_signal(data, data_h1=None):
    """
    Analiza acción del precio, liquidez, estructura,
    momentum, volumen, RSI, EMA, volatilidad.
    Devuelve (dirección, score) o (None, 0).
    Score máx 100. Mínimo requerido: externo.
    """
    c, h, l, v = data['c'], data['h'], data['l'], data['v']
    px   = c[-1]
    r    = rsi(c)
    e21  = ema(c, 21)
    e50  = ema(c, min(50, len(c)))
    at   = atr(h, l, c)

    buy = sell = 0

    # ── 1. TENDENCIA EMA21 (peso 20) ──────────
    if px > e21: buy  += 20
    else:         sell += 20
    if e21 > e50: buy  += 5
    else:          sell += 5

    # ── 2. RSI — zonas extremas (peso 25) ─────
    if   r < 28: buy  += 25
    elif r < 38: buy  += 15
    elif r < 45: buy  += 7
    elif r > 72: sell += 25
    elif r > 62: sell += 15
    elif r > 55: sell += 7

    # penalización RSI contrario
    if r > 65 and buy  > sell: buy  -= 10
    if r < 35 and sell > buy:  sell -= 10

    # ── 3. MOMENTUM 3 velas (peso 20) ─────────
    if len(c) >= 4:
        up3 = all(c[i] > c[i-1] for i in range(-3, 0))
        dn3 = all(c[i] < c[i-1] for i in range(-3, 0))
        if up3: buy  += 20
        if dn3: sell += 20
        # penalización momento contrario
        if dn3 and buy  > sell: buy  -= 12
        if up3 and sell > buy:  sell -= 12

    # ── 4. IMPULSO institucional (peso 15) ────
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5, 0)) / 5
        if avg > 0 and move > avg * 2.5:
            if c[-1] > c[-5]: buy  += 15
            else:              sell += 15

    # ── 5. SWEEP DE LIQUIDEZ (peso 20) ────────
    # Barrido institucional: rompe mínimo/máximo y cierra dentro
    if len(c) >= 6:
        prev_h = max(h[-6:-1])
        prev_l = min(l[-6:-1])
        rng    = h[-1] - l[-1]
        if rng > 0:
            if l[-1] < prev_l and c[-1] > prev_l:
                wick = min(c[-2], c[-1]) - l[-1]
                if wick / rng >= 0.35: buy  += 20
            if h[-1] > prev_h and c[-1] < prev_h:
                wick = h[-1] - max(c[-2], c[-1])
                if wick / rng >= 0.35: sell += 20

    # ── 6. SOPORTE / RESISTENCIA rango 20v (peso 10) ──
    if len(c) >= 20:
        mn  = min(l[-20:]); mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if pos < 0.20: buy  += 10
            if pos > 0.80: sell += 10
            if pos > 0.75 and buy  > sell: buy  -= 8
            if pos < 0.25 and sell > buy:  sell -= 8

    # ── 7. VOLUMEN confirma (peso 10) ─────────
    if v and len(v) >= 5:
        avg_v = sum(v[-6:-1]) / 5
        if avg_v > 0 and v[-1] > avg_v * 1.3:
            # volumen alto confirma la dirección dominante
            if buy > sell: buy  += 10
            else:          sell += 10

    # ── 8. VOLATILIDAD — evitar velas caóticas ─
    if at > 0:
        last_move = abs(c[-1] - c[-2])
        if last_move > at * 4:  # vela explosiva sin contexto — reducir confianza
            buy  = int(buy  * 0.75)
            sell = int(sell * 0.75)

    # ── 9. CHoCH en H1 si disponible ──────────
    if data_h1 and len(data_h1['c']) >= 10:
        c1 = data_h1['c']
        e21_h1 = ema(c1, 21)
        if c1[-1] > e21_h1: buy  += 8
        else:                sell += 8

    # Necesita ventaja clara de al menos 15 puntos sobre el contrario
    if buy >= 50 and buy > sell + 15:
        return 'buy',  min(buy, 100)
    if sell >= 50 and sell > buy + 15:
        return 'sell', min(sell, 100)

    return None, 0

# =============================================
# SEGUIMIENTO TP / SL
# =============================================

def check_active_signals():
    global active_sigs, daily_pnl, last_sl, stats
    to_rm = []
    now   = time.time()

    for s in active_sigs:
        cfg = ASSETS[s['asset']]
        fmt = fmt_btc if s['asset'] == 'btc' else fmt_gold

        # Timeout 90 min
        if now - s['t'] > 5400:
            locked[s['asset']] = False
            to_rm.append(s)
            log(f'{cfg["icon"]} Timeout')
            continue

        p = get_spot(s['asset'])
        if not p: continue

        hit_tp = (s['d']=='buy'  and p >= s['tp']) or (s['d']=='sell' and p <= s['tp'])
        hit_sl = (s['d']=='buy'  and p <= s['sl']) or (s['d']=='sell' and p >= s['sl'])

        if not hit_tp and not hit_sl: continue

        locked[s['asset']] = False
        key = 'tp' if hit_tp else 'sl'
        stats[s['asset']][key] += 1
        trade_history.append(key)
        last_sl[s['asset']] = (key == 'sl')

        gain = GAINS[s['asset']].get(0.05, 0)
        daily_pnl += gain if hit_tp else -gain * 0.5

        trade_log.append({
            'asset':   s['asset'],
            'dir':     s['d'],
            'hour':    datetime.now().hour,
            'score':   s.get('score', 0),
            'result':  key,
            'dur_min': round((now - s['t']) / 60),
        })
        save_log()

        if hit_tp:
            send(f'✅ TP\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
        else:
            send(f'❌ SL\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')

        to_rm.append(s)

    for s in to_rm:
        if s in active_sigs:
            active_sigs.remove(s)

# =============================================
# RESUMEN DIARIO — 23:00
# =============================================

def daily_report():
    global last_d_report, daily_pnl, stats
    n = datetime.now()
    hoy = n.strftime('%Y-%m-%d')
    if n.hour != 23 or n.minute != 0 or last_d_report == hoy: return
    last_d_report = hoy

    tg = stats['gold']['tp']; sg = stats['gold']['sl']
    tb = stats['btc']['tp'];  sb = stats['btc']['sl']
    tot = tg+sg+tb+sb; tp = tg+tb
    pct = round(tp/tot*100) if tot else 0

    send(
        f'📊 RESUMEN\n\n'
        f'Señales: {tot}\n\n'
        f'✅ TP: {tp}\n'
        f'❌ SL: {tot-tp}\n\n'
        f'Efectividad: {pct}%'
    )

    for k in stats: stats[k] = {'tp': 0, 'sl': 0}
    daily_pnl = 0.0

# =============================================
# GENERAR SEÑAL
# =============================================

def try_signal(asset, data, data_h1=None):
    cfg = ASSETS[asset]
    fmt = fmt_btc if asset == 'btc' else fmt_gold
    px  = data['c'][-1]

    if locked[asset]:
        log(f'{cfg["icon"]} {fmt(px)} — operación abierta')
        return

    sig, score = analyze_signal(data, data_h1)
    log(f'{cfg["icon"]} {fmt(px)} | {sig} score:{score}')

    # Umbral más exigente si el último trade fue SL
    threshold = SCORE_MIN

    if not sig or score < threshold:
        return

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo = 'BUY' if isBuy else 'SELL'

    gains = '\n'.join([
        f'0.01 → +{GAINS[asset][0.01]}€',
        f'0.02 → +{GAINS[asset][0.02]}€',
        f'0.05 → +{GAINS[asset][0.05]}€',
        f'0.08 → +{GAINS[asset][0.08]}€',
        f'0.10 → +{GAINS[asset][0.10]}€',
        f'0.15 → +{GAINS[asset][0.15]}€',
    ])

    send(
        f'{cfg["icon"]} {cfg["label"]}\n\n'
        f'{tipo}\n\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)}\n'
        f'SL: {fmt(sl)}\n\n'
        f'{gains}\n\n'
        f'Hora: {now_str()}'
    )
    log(f'SEÑAL {tipo} {cfg["label"]} {fmt(px)} score:{score} threshold:{threshold}')

    locked[asset] = True
    active_sigs.append({
        'asset': asset, 'd': sig,
        'e': px, 'tp': tp, 'sl': sl,
        't': time.time(), 'score': score,
    })

# =============================================
# MAIN LOOP
# =============================================

def main():
    log('SNIPER BOT v13.0')

    while True:
        try:
            daily_report()
            check_active_signals()

            is_weekend = datetime.now().weekday() >= 5

            # ORO — solo entre semana
            if not is_weekend:
                gd    = gold_data('5min')
                gd_h1 = gold_data('1h')
                if gd: try_signal('gold', gd, gd_h1)
                else:  log('Sin datos ORO')

            time.sleep(3)

            # BTC — siempre, 24/7
            bd    = btc_data('5min')
            bd_h1 = btc_data('1h')
            if bd: try_signal('btc', bd, bd_h1)
            else:  log('Sin datos BTC')

            time.sleep(15)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
