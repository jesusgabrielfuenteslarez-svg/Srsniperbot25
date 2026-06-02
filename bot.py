import os, time, requests
from datetime import datetime

# =============================================
# SNIPER SCANNER v2.0
# Escáner de oportunidades XAUUSD y BTCUSD
# Filosofía: equilibrio — ni ultra restrictivo
# ni spam. Como un analista profesional.
# =============================================

TG_TOKEN    = os.environ.get('TG_TOKEN',    '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ')
TG_CHAT_ID  = os.environ.get('TG_CHAT_ID',  '6467338067')
TG_GROUP_ID = os.environ.get('TG_GROUP_ID', '-5123266724')
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'

ASSETS = {
    'gold': {
        'icon': '🥇', 'label': 'ORO',
        'tp': 10, 'sl': 5,
        'symbol_twelve': 'XAU/USD',
        'symbol_binance': 'XAUUSDT',
        'min_price': 2000,
        'is_btc': False,
    },
    'btc': {
        'icon': '₿', 'label': 'BITCOIN',
        'tp': 250, 'sl': 125,
        'symbol_twelve': 'BTC/USD',
        'symbol_binance': 'BTCUSDT',
        'min_price': 10000,
        'is_btc': True,
    },
}

# Lotajes para mostrar en el mensaje
LOTS = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

# Umbral de score — 55 es equilibrado
# Sube → menos señales / Baja → más señales
SCORE_MIN = 55

# Cooldown mínimo entre alertas del mismo activo
COOLDOWN_SEC = 15 * 60   # 15 minutos

# ── Estado ─────────────────────────────────
last_alert  = {'gold': 0, 'btc': 0}
last_key    = {'gold': None, 'btc': None}
op_count    = {'gold': 0, 'btc': 0}
active_ops  = []   # para seguimiento TP/SL

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
            requests.post(TG_API,
                json={'chat_id': chat, 'text': msg},
                timeout=10)
        except:
            pass

def fmt(v, is_btc):
    return f'{round(v):,}' if is_btc else f'{v:.2f}'

def gain(tp_pts, lot, is_btc):
    # Oro: 1 punto = $1 por 0.01 lot → ~1€
    # BTC: 1 punto = $1 por 0.01 lot → ~1€
    return int(tp_pts * lot * 100)

# =============================================
# DATOS
# =============================================

def get_candles_twelve(symbol, interval='5min', size=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': interval,
            'outputsize': size, 'apikey': TWELVE_KEY,
        }, timeout=12)
        d = r.json()
        if d.get('status') == 'error': return None
        vals = d.get('values', [])
        if len(vals) < 20: return None
        return {
            'c': [float(v['close'])  for v in reversed(vals)],
            'h': [float(v['high'])   for v in reversed(vals)],
            'l': [float(v['low'])    for v in reversed(vals)],
            'v': [float(v.get('volume', 0)) for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Twelve {symbol}: {e}')
        return None

def get_candles_binance(symbol, interval='5m', size=60):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines'
            f'?symbol={symbol}&interval={interval}&limit={size}',
            timeout=8)
        k = r.json()
        if not k or len(k) < 20: return None
        return {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        }
    except Exception as e:
        log(f'Binance {symbol}: {e}')
        return None

def get_spot_binance(symbol):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}',
            timeout=5).json()
        if 'bidPrice' in r:
            return (float(r['bidPrice']) + float(r['askPrice'])) / 2
    except:
        pass
    return None

def fetch(asset, tf='5min'):
    cfg = ASSETS[asset]
    d = get_candles_twelve(cfg['symbol_twelve'], tf)
    if d and d['c'][-1] > cfg['min_price']:
        return d
    interval_b = '5m' if tf == '5min' else ('1h' if tf == '1h' else '15m')
    return get_candles_binance(cfg['symbol_binance'], interval_b)

def fetch_spot(asset):
    cfg = ASSETS[asset]
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': cfg['symbol_twelve'], 'apikey': TWELVE_KEY},
            timeout=6)
        p = float(r.json().get('price', 0))
        if p > cfg['min_price']: return p
    except:
        pass
    return get_spot_binance(cfg['symbol_binance'])

# =============================================
# INDICADORES
# =============================================

def calc_rsi(c, n=14):
    if len(c) < n + 1: return 50
    g = l = 0
    for i in range(len(c) - n, len(c)):
        d = c[i] - c[i-1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g/n, l/n
    return round(100 - 100/(1 + ag/al), 1) if al else 100

def calc_ema(c, n):
    if len(c) < n: return c[-1]
    k = 2/(n + 1); e = c[0]
    for p in c[1:]: e = p*k + e*(1-k)
    return e

def calc_atr(h, l, c, n=14):
    if len(c) < n + 1: return 1
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# DETECCIÓN DE ESTRUCTURA
# =============================================

def detect_choch(c, lookback=10):
    """
    CHoCH: el precio rompe el último swing high/low
    en dirección contraria al movimiento previo.
    Devuelve 'buy', 'sell' o None.
    """
    if len(c) < lookback + 3: return None
    seg = c[-(lookback+3):]
    highs = [i for i in range(1, len(seg)-1) if seg[i] > seg[i-1] and seg[i] > seg[i+1]]
    lows  = [i for i in range(1, len(seg)-1) if seg[i] < seg[i-1] and seg[i] < seg[i+1]]
    if not highs or not lows: return None
    last_high = seg[max(highs)]
    last_low  = seg[min(lows)]
    px = c[-1]
    if px > last_high: return 'buy'   # rompió estructura bajista
    if px < last_low:  return 'sell'  # rompió estructura alcista
    return None

def detect_bos(c, h, l, lookback=15):
    """
    Break of Structure: cierre por encima/debajo
    del swing más reciente con vela de confirmación.
    """
    if len(c) < lookback: return None
    prev_h = max(h[-lookback:-2])
    prev_l = min(l[-lookback:-2])
    if c[-1] > prev_h and c[-2] > prev_h: return 'buy'
    if c[-1] < prev_l and c[-2] < prev_l: return 'sell'
    return None

def detect_sweep(c, h, l):
    """
    Liquidity Sweep: rompe un nivel y cierra dentro.
    Señal institucional fuerte.
    """
    if len(c) < 8: return None
    prev_h = max(h[-8:-1])
    prev_l = min(l[-8:-1])
    rng = h[-1] - l[-1]
    if rng == 0: return None
    # Sweep de lows → rebote alcista
    if l[-1] < prev_l and c[-1] > prev_l:
        wick = min(c[-2], c[-1]) - l[-1]
        if wick / rng >= 0.30: return 'buy'
    # Sweep de highs → rebote bajista
    if h[-1] > prev_h and c[-1] < prev_h:
        wick = h[-1] - max(c[-2], c[-1])
        if wick / rng >= 0.30: return 'sell'
    return None

# =============================================
# SCORE — equilibrado
# Máximo teórico: ~105
# Umbral: 55
# =============================================

def score_oportunidad(d5, d15=None, d1h=None):
    c = d5['c']; h = d5['h']; l = d5['l']; v = d5['v']
    px   = c[-1]
    r    = calc_rsi(c)
    e9   = calc_ema(c, 9)
    e21  = calc_ema(c, 21)
    e50  = calc_ema(c, min(50, len(c)))
    at   = calc_atr(h, l, c)

    buy = sell = 0
    reasons_buy  = []
    reasons_sell = []

    # ── 1. TENDENCIA EMA (peso 15) ────────────
    if px > e21:
        buy += 10
        if e21 > e50: buy += 5; reasons_buy.append('EMA_trend')
    else:
        sell += 10
        if e21 < e50: sell += 5; reasons_sell.append('EMA_trend')

    # EMA9 como filtro de momentum inmediato
    if px > e9: buy  += 3
    else:        sell += 3

    # ── 2. RSI (peso 20) ──────────────────────
    # Zonas favorables sin ser extremas — equilibrado
    if   r < 30: buy  += 20; reasons_buy.append('RSI_OS')
    elif r < 40: buy  += 13; reasons_buy.append('RSI_low')
    elif r < 48: buy  +=  6
    elif r > 70: sell += 20; reasons_sell.append('RSI_OB')
    elif r > 60: sell += 13; reasons_sell.append('RSI_high')
    elif r > 52: sell +=  6

    # ── 3. MOMENTUM — velas consecutivas (peso 18) ──
    if len(c) >= 4:
        up3 = all(c[i] > c[i-1] for i in range(-3, 0))
        dn3 = all(c[i] < c[i-1] for i in range(-3, 0))
        if up3: buy  += 18; reasons_buy.append('MOM_up')
        if dn3: sell += 18; reasons_sell.append('MOM_dn')

    # ── 4. IMPULSO en 5 velas (peso 12) ──────
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5, 0)) / 5
        if avg > 0 and move > avg * 2.0:   # 2.0x más asequible que 2.5x
            if c[-1] > c[-5]: buy  += 12; reasons_buy.append('IMPULSE')
            else:              sell += 12; reasons_sell.append('IMPULSE')

    # ── 5. LIQUIDITY SWEEP (peso 20) ─────────
    sweep = detect_sweep(c, h, l)
    if sweep == 'buy':  buy  += 20; reasons_buy.append('SWEEP')
    if sweep == 'sell': sell += 20; reasons_sell.append('SWEEP')

    # ── 6. CHoCH (peso 15) ───────────────────
    choch = detect_choch(c)
    if choch == 'buy':  buy  += 15; reasons_buy.append('CHoCH')
    if choch == 'sell': sell += 15; reasons_sell.append('CHoCH')

    # ── 7. BOS (peso 12) ─────────────────────
    bos = detect_bos(c, h, l)
    if bos == 'buy':  buy  += 12; reasons_buy.append('BOS')
    if bos == 'sell': sell += 12; reasons_sell.append('BOS')

    # ── 8. SOPORTE / RESISTENCIA rango (peso 10) ──
    if len(c) >= 20:
        mn = min(l[-20:]); mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if pos < 0.22: buy  += 10; reasons_buy.append('SUP')
            if pos > 0.78: sell += 10; reasons_sell.append('RES')

    # ── 9. VOLUMEN confirma (peso 8) ─────────
    if v and len(v) >= 6:
        avg_v = sum(v[-7:-1]) / 6
        if avg_v > 0 and v[-1] > avg_v * 1.3:
            if buy > sell: buy  += 8
            else:          sell += 8

    # ── 10. CONFIRMACIÓN H1 si disponible (peso 10) ──
    if d1h and len(d1h['c']) >= 21:
        c1  = d1h['c']
        e21h = calc_ema(c1, 21)
        r1h  = calc_rsi(c1)
        if c1[-1] > e21h: buy  += 6
        else:              sell += 6
        if r1h < 45:       buy  += 4
        if r1h > 55:       sell += 4

    # ── 11. ATR — filtro de volatilidad ──────
    # Evitar velas de caos pero no bloquear mercado activo
    if at > 0:
        last_body = abs(c[-1] - c[-2])
        if last_body > at * 5:   # solo penalizar explosiones extremas
            buy  = int(buy  * 0.75)
            sell = int(sell * 0.75)

    # ── 12. CONFIRMACIÓN DE VELA ──────────────
    # Vela de cierre a favor suma puntos
    vela_body = c[-1] - c[-2]
    if vela_body > 0 and buy  > sell: buy  += 5
    if vela_body < 0 and sell > buy:  sell += 5

    buy  = min(buy,  100)
    sell = min(sell, 100)

    log(f'  buy:{buy} sell:{sell} r:{r:.1f} sweep:{sweep} choch:{choch} bos:{bos}')

    # Necesita superar umbral y tener ventaja sobre el contrario
    if buy  >= SCORE_MIN and buy  > sell + 10:
        return 'buy',  buy
    if sell >= SCORE_MIN and sell > buy  + 10:
        return 'sell', sell

    return None, 0

# =============================================
# SEGUIMIENTO TP / SL
# =============================================

def check_ops():
    global active_ops
    to_rm = []
    now   = time.time()

    for op in active_ops:
        cfg = ASSETS[op['asset']]

        # Timeout 2 horas
        if now - op['t'] > 7200:
            to_rm.append(op)
            log(f'{cfg["icon"]} Op timeout')
            continue

        p = fetch_spot(op['asset'])
        if not p: continue

        hit_tp = (op['dir']=='buy'  and p >= op['tp']) or \
                 (op['dir']=='sell' and p <= op['tp'])
        hit_sl = (op['dir']=='buy'  and p <= op['sl']) or \
                 (op['dir']=='sell' and p >= op['sl'])

        if hit_tp:
            n = op['n']
            send(f'✅ TP #{n}\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'TP #{n} {cfg["label"]}')
            to_rm.append(op)
        elif hit_sl:
            n = op['n']
            send(f'❌ SL #{n}\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'SL #{n} {cfg["label"]}')
            to_rm.append(op)

    for op in to_rm:
        if op in active_ops:
            active_ops.remove(op)

# =============================================
# ENVIAR OPORTUNIDAD
# =============================================

def send_oportunidad(asset, direction, px):
    cfg   = ASSETS[asset]
    isBuy = direction == 'buy'
    tp    = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl    = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo  = 'BUY' if isBuy else 'SELL'
    is_btc = cfg['is_btc']

    op_count[asset] += 1
    n = sum(op_count.values())

    gains = '\n'.join([
        f'{l:.2f} → +{gain(cfg["tp"], l, is_btc)}€'
        for l in LOTS
    ])

    msg = (
        f'{cfg["icon"]} {cfg["label"]}\n\n'
        f'{tipo}\n\n'
        f'Entrada: {fmt(px, is_btc)}\n'
        f'TP: {fmt(tp, is_btc)}\n'
        f'SL: {fmt(sl, is_btc)}\n\n'
        f'Ganancia estimada:\n'
        f'{gains}\n\n'
        f'Hora: {now_str()}'
    )
    send(msg)
    log(f'OPORTUNIDAD #{n} {tipo} {cfg["label"]} {fmt(px, is_btc)}')

    last_alert[asset] = time.time()
    last_key[asset]   = f'{direction}-{round(px / (cfg["tp"] * 3))}'

    # Registrar para seguimiento
    active_ops.append({
        'asset': asset, 'dir': direction,
        'tp': tp, 'sl': sl,
        't': time.time(), 'n': n,
    })

# =============================================
# ESCANEAR
# =============================================

def scan(asset):
    cfg = ASSETS[asset]

    # Cooldown
    if time.time() - last_alert[asset] < COOLDOWN_SEC:
        remaining = (COOLDOWN_SEC - (time.time() - last_alert[asset])) / 60
        log(f'{cfg["icon"]} Cooldown {remaining:.0f}min')
        return

    d5  = fetch(asset, '5min')
    if not d5:
        log(f'{cfg["icon"]} Sin datos M5')
        return

    d15 = fetch(asset, '15min')
    d1h = fetch(asset, '1h')

    px = d5['c'][-1]
    log(f'{cfg["icon"]} Escaneando {fmt(px, cfg["is_btc"])}')

    direction, score = score_oportunidad(d5, d15, d1h)

    if not direction:
        return

    # Anti-duplicado: misma zona de precio
    key = f'{direction}-{round(px / (cfg["tp"] * 3))}'
    if key == last_key[asset]:
        log(f'{cfg["icon"]} Misma oportunidad — skip')
        return

    send_oportunidad(asset, direction, px)

# =============================================
# MAIN
# =============================================

def main():
    log('SNIPER SCANNER v2.0 — INICIANDO')
    log(f'Score mínimo: {SCORE_MIN} | Cooldown: {COOLDOWN_SEC//60}min')

    while True:
        try:
            # Seguimiento TP/SL de ops abiertas
            if active_ops:
                check_ops()

            is_weekend = datetime.now().weekday() >= 5

            # ORO — solo entre semana
            if not is_weekend:
                scan('gold')
            else:
                log('Fin de semana — ORO cerrado')

            time.sleep(5)

            # BTC — 24/7
            scan('btc')

            time.sleep(20)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(20)

if __name__ == '__main__':
    main()
