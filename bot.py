import os, time, requests
from datetime import datetime

# =============================================
# SNIPER SCANNER v1.0
# Escáner profesional — ORO y BTC
# Solo avisa cuando hay una oportunidad real.
# La decisión es siempre del usuario.
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

# Umbral mínimo para considerar que hay oportunidad
CONFIDENCE_MIN = 70   # porcentaje — solo lo mejor pasa

# Cooldown entre alertas del mismo activo (minutos)
# Evita spam sin bloquear el escáner
COOLDOWN_MIN = 20

# ── Estado ─────────────────────────────────
last_alert = {'gold': 0, 'btc': 0}   # timestamp última alerta por activo
last_key   = {'gold': None, 'btc': None}  # evita repetir misma oportunidad

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

def fmt(v, asset):
    return f'{round(v):,}' if asset == 'btc' else f'{v:.2f}'

def cooldown_ok(asset):
    elapsed = (time.time() - last_alert[asset]) / 60
    return elapsed >= COOLDOWN_MIN

# =============================================
# DATOS
# =============================================

def candles(symbol, interval='5min', size=60):
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
            'c': [float(v['close']) for v in reversed(vals)],
            'h': [float(v['high'])  for v in reversed(vals)],
            'l': [float(v['low'])   for v in reversed(vals)],
            'v': [float(v.get('volume', 0)) for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Candles {symbol}: {e}')
        return None

def candles_binance(sym, min_px, interval='5m', size=60):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={size}',
            timeout=8)
        k = r.json()
        d = {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        }
        return d if d['c'][-1] > min_px else None
    except:
        return None

def get_data(asset, tf='5min'):
    if asset == 'gold':
        d = candles('XAU/USD', tf)
        if d and d['c'][-1] > 2000: return d
        return candles_binance('XAUUSDT', 2000, '5m' if tf == '5min' else '1h')
    else:
        d = candles('BTC/USD', tf)
        if d and d['c'][-1] > 10000: return d
        return candles_binance('BTCUSDT', 10000, '5m' if tf == '5min' else '1h')

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
# ESCÁNER — puntúa la oportunidad 0-100
# =============================================

def score_opportunity(d5, d1h=None):
    """
    Evalúa todos los factores internamente.
    Devuelve (dirección, confianza%) o (None, 0).
    Solo retorna si la confluencia es real y clara.
    """
    c = d5['c']; h = d5['h']; l = d5['l']; v = d5['v']
    px   = c[-1]
    r    = rsi(c)
    e21  = ema(c, 21)
    e50  = ema(c, min(50, len(c)))
    at   = atr(h, l, c)

    buy = sell = 0

    # ── Tendencia EMA ─────────────────────────
    if px > e21: buy  += 20
    else:         sell += 20
    if e21 > e50: buy  += 8
    else:          sell += 8

    # ── RSI ──────────────────────────────────
    if   r < 25: buy  += 25    # sobreventa fuerte
    elif r < 35: buy  += 16
    elif r < 42: buy  += 8
    elif r > 75: sell += 25    # sobrecompra fuerte
    elif r > 65: sell += 16
    elif r > 58: sell += 8

    # penalización RSI en contra
    if r > 60 and buy  > sell: buy  -= 12
    if r < 40 and sell > buy:  sell -= 12

    # ── Momentum ─────────────────────────────
    if len(c) >= 4:
        up3 = all(c[i] > c[i-1] for i in range(-3, 0))
        dn3 = all(c[i] < c[i-1] for i in range(-3, 0))
        if up3: buy  += 18
        if dn3: sell += 18
        if dn3 and buy  > sell: buy  -= 10
        if up3 and sell > buy:  sell -= 10

    # ── Impulso institucional ─────────────────
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5, 0)) / 5
        if avg > 0 and move > avg * 2.5:
            if c[-1] > c[-5]: buy  += 14
            else:              sell += 14

    # ── Sweep de liquidez ─────────────────────
    # El más relevante — instituciones barren stops antes de mover
    if len(c) >= 6:
        prev_h = max(h[-6:-1])
        prev_l = min(l[-6:-1])
        rng    = h[-1] - l[-1]
        if rng > 0:
            # Barrido bajista + recuperación → BUY
            if l[-1] < prev_l and c[-1] > prev_l:
                wick = min(c[-2], c[-1]) - l[-1]
                if wick / rng >= 0.35:
                    buy += 22    # señal institucional clara
            # Barrido alcista + rechazo → SELL
            if h[-1] > prev_h and c[-1] < prev_h:
                wick = h[-1] - max(c[-2], c[-1])
                if wick / rng >= 0.35:
                    sell += 22

    # ── Posición en rango 20 velas ────────────
    if len(c) >= 20:
        mn  = min(l[-20:]); mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if pos < 0.18: buy  += 10   # fondo del rango
            if pos > 0.82: sell += 10   # techo del rango
            if pos > 0.72 and buy  > sell: buy  -= 8
            if pos < 0.28 and sell > buy:  sell -= 8

    # ── Volumen confirma ─────────────────────
    if v and len(v) >= 5:
        avg_v = sum(v[-6:-1]) / 5
        if avg_v > 0 and v[-1] > avg_v * 1.4:
            if buy > sell: buy  += 8
            else:          sell += 8

    # ── Volatilidad — filtro de caos ──────────
    if at > 0:
        last_move = abs(c[-1] - c[-2])
        if last_move > at * 4.5:
            buy  = int(buy  * 0.70)
            sell = int(sell * 0.70)

    # ── Confirmación H1 ───────────────────────
    if d1h and len(d1h['c']) >= 10:
        c1   = d1h['c']
        e21h = ema(c1, 21)
        if c1[-1] > e21h: buy  += 10
        else:              sell += 10

    buy  = min(buy,  100)
    sell = min(sell, 100)

    # Necesita ventaja clara Y umbral mínimo
    if buy  >= CONFIDENCE_MIN and buy  > sell + 15:
        return 'buy',  buy
    if sell >= CONFIDENCE_MIN and sell > buy  + 15:
        return 'sell', sell

    return None, 0

# =============================================
# ENVIAR ALERTA
# =============================================

def send_alert(asset, direction, confidence, px):
    cfg  = ASSETS[asset]
    isBuy = direction == 'buy'
    tp   = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl   = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo = 'BUY' if isBuy else 'SELL'

    msg = (
        f'{cfg["icon"]} {cfg["label"]}\n\n'
        f'Oportunidad detectada\n\n'
        f'Confianza: {confidence}%\n\n'
        f'{tipo}\n\n'
        f'Entrada: {fmt(px, asset)}\n'
        f'TP: {fmt(tp, asset)}\n'
        f'SL: {fmt(sl, asset)}\n\n'
        f'Hora: {now_str()}'
    )
    send(msg)
    log(f'ALERTA {tipo} {cfg["label"]} {fmt(px, asset)} confianza:{confidence}%')

    last_alert[asset] = time.time()
    last_key[asset]   = f'{direction}-{round(px / (cfg["tp"] * 2))}'

# =============================================
# ESCANEAR ACTIVO
# =============================================

def scan(asset):
    cfg = ASSETS[asset]

    # Sin cooldown — no spam
    if not cooldown_ok(asset):
        remaining = COOLDOWN_MIN - (time.time() - last_alert[asset]) / 60
        log(f'{cfg["icon"]} Cooldown: {remaining:.0f}min restantes')
        return

    d5  = get_data(asset, '5min')
    if not d5:
        log(f'{cfg["icon"]} Sin datos')
        return

    d1h = get_data(asset, '1h')
    px  = d5['c'][-1]

    direction, confidence = score_opportunity(d5, d1h)
    log(f'{cfg["icon"]} {fmt(px, asset)} | dir:{direction} confianza:{confidence}%')

    if not direction:
        return   # sin oportunidad — silencio total

    # Evitar repetir exactamente la misma oportunidad
    key = f'{direction}-{round(px / (cfg["tp"] * 2))}'
    if key == last_key[asset]:
        log(f'{cfg["icon"]} Misma oportunidad — skip')
        return

    send_alert(asset, direction, confidence, px)

# =============================================
# MAIN
# =============================================

def main():
    log('SNIPER SCANNER v1.0 — INICIANDO')

    while True:
        try:
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
