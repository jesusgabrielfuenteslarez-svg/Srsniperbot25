import os, time, requests
from datetime import datetime, timezone
from collections import deque

# =============================================
# SNIPER BOT v11.0 — SIMPLE Y AGRESIVO
# Objetivo: señales reales, 100€/día
# Solo 2 mensajes: señal + resultado
# =============================================

TG_TOKEN    = os.environ.get('TG_TOKEN',    '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ')
TG_CHAT_ID  = os.environ.get('TG_CHAT_ID',  '6467338067')
TG_GROUP_ID = os.environ.get('TG_GROUP_ID', '-5123266724')
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200},
}
LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]
GAINS = {
    'gold': {l: round(20  * l * 10,  1) for l in LOTAJES},
    'btc':  {l: round(500 * l * 0.1, 1) for l in LOTAJES},
}

# ── Umbral único — sin distinción de sesión ──
SCORE_MIN = 45   # bajado de 58/72 → más señales

# ── Estado ─────────────────────────────────
locked       = {'gold': False, 'btc': False}
lock_time    = {'gold': 0,     'btc': 0}
LOCK_TIMEOUT = 3600   # 1h máximo por trade

stats        = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
active_sigs  = []
trade_history= deque(maxlen=20)
daily_pnl    = 0.0
last_d_report= None

sl_streak    = 0   # racha actual de SL
SL_ALERT_AT  = 3   # avisar si hay 3 SL seguidos

# =============================================
# UTILIDADES
# =============================================

def now_str():
    return datetime.now().strftime('%H:%M')

def log(m):
    print(f'[{now_str()}] {m}')

def send(msg):
    for chat in [TG_CHAT_ID, TG_GROUP_ID]:
        try:
            requests.post(TG_API, json={
                'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'
            }, timeout=10)
        except:
            pass

# =============================================
# PRECIOS
# =============================================

def get_candles(symbol, interval='5min', size=40):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': interval,
            'outputsize': size, 'apikey': TWELVE_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            return None
        vals = data.get('values', [])
        if not vals:
            return None
        return {
            'c': [float(v['close']) for v in reversed(vals)],
            'h': [float(v['high'])  for v in reversed(vals)],
            'l': [float(v['low'])   for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Candles {symbol}: {e}')
        return None

def binance_candles(sym, min_price):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=40',
            timeout=8)
        k = r.json()
        d = {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
        }
        return d if d['c'][-1] > min_price else None
    except:
        return None

def gold_data():
    d = get_candles('XAU/USD')
    if d and d['c'][-1] > 2000: return d
    return binance_candles('XAUUSDT', 2000)

def btc_data():
    d = get_candles('BTC/USD')
    if d and d['c'][-1] > 10000: return d
    return binance_candles('BTCUSDT', 10000)

def get_spot(asset):
    sym = 'XAU/USD' if asset == 'gold' else 'BTC/USD'
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': sym, 'apikey': TWELVE_KEY}, timeout=6)
        p = float(r.json().get('price', 0))
        if p > 0: return p
    except:
        pass
    try:
        s = 'XAUUSDT' if asset == 'gold' else 'BTCUSDT'
        t = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={s}',
            timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except:
        pass
    return None

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
    k = 2/(n + 1)
    e = c[0]
    for p in c[1:]: e = p*k + e*(1-k)
    return e

def calc_atr(h, l, c, n=14):
    if len(c) < n + 1: return 1
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# SEÑAL — lógica simplificada
# =============================================

def get_signal(data):
    """
    Devuelve ('buy'|'sell', score) o (None, 0)
    Score mínimo: SCORE_MIN=45
    """
    c, h, l = data['c'], data['h'], data['l']
    px  = c[-1]
    r   = calc_rsi(c)
    e21 = calc_ema(c, 21)
    e50 = calc_ema(c, min(50, len(c)))
    atr = calc_atr(h, l, c)

    vb = vs = 0   # votos buy / sell

    # RSI
    if r < 30:   vb += 3
    elif r < 45: vb += 1
    if r > 70:   vs += 3
    elif r > 55: vs += 1

    # EMA tendencia
    if px > e21: vb += 2
    if px < e21: vs += 2
    if e21 > e50: vb += 1
    if e21 < e50: vs += 1

    # Momentum 3 velas consecutivas
    if len(c) >= 4:
        if all(c[i] > c[i-1] for i in range(-3, 0)): vb += 3
        if all(c[i] < c[i-1] for i in range(-3, 0)): vs += 3

    # Impulso fuerte (vela grande vs promedio)
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5,0)) / 5
        if avg > 0 and move > avg * 2.5:
            if c[-1] > c[-5]: vb += 2
            else:              vs += 2

    # Sweep de liquidez (reversión)
    if len(c) >= 6:
        prev_h = max(h[-6:-1])
        prev_l = min(l[-6:-1])
        rng = h[-1] - l[-1]
        if rng > 0:
            if l[-1] < prev_l and c[-1] > prev_l:
                wick = min(c[-2], c[-1]) - l[-1]
                if wick / rng >= 0.35: vb += 4
            if h[-1] > prev_h and c[-1] < prev_h:
                wick = h[-1] - max(c[-2], c[-1])
                if wick / rng >= 0.35: vs += 4

    # Posición en rango 20 velas
    if len(c) >= 20:
        mn = min(l[-20:]); mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if pos < 0.25: vb += 2
            if pos > 0.75: vs += 2

    # Decidir dirección
    if vb >= 5 and vb > vs + 1:
        sig = 'buy'
        score = min(vb * 9, 100)
    elif vs >= 5 and vs > vb + 1:
        sig = 'sell'
        score = min(vs * 9, 100)
    else:
        return None, 0

    return sig, score

# =============================================
# SEGUIMIENTO TP/SL
# =============================================

def check_active_signals():
    global active_sigs, daily_pnl, sl_streak, stats
    to_rm = []
    now = time.time()

    for s in active_sigs:
        cfg = ASSETS[s['asset']]
        btc = s['asset'] == 'btc'
        fmt = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')

        # Timeout 1h — cerrar automáticamente
        if now - s['t'] > LOCK_TIMEOUT:
            locked[s['asset']] = False
            to_rm.append(s)
            log(f'{cfg["icon"]} Timeout — cerrando operación')
            continue

        p = get_spot(s['asset'])
        if not p:
            continue

        hit_tp = (s['d'] == 'buy'  and p >= s['tp']) or \
                 (s['d'] == 'sell' and p <= s['tp'])
        hit_sl = (s['d'] == 'buy'  and p <= s['sl']) or \
                 (s['d'] == 'sell' and p >= s['sl'])

        if hit_tp or hit_sl:
            locked[s['asset']] = False
            key = 'tp' if hit_tp else 'sl'
            stats[s['asset']][key] += 1
            trade_history.append(key)

            tp_t = stats['gold']['tp'] + stats['btc']['tp']
            sl_t = stats['gold']['sl'] + stats['btc']['sl']
            tot  = tp_t + sl_t
            pct  = round(tp_t/tot*100) if tot else 0
            sig_num = tp_t + sl_t
            asset_label = 'ORO' if s['asset'] == 'gold' else 'BITCOIN'

            if hit_tp:
                sl_streak = 0
                daily_pnl += GAINS[s['asset']].get(0.05, 0)
                send(
                    f'✅ TP #{sig_num}\n\n'
                    f'{cfg["icon"]} {asset_label}\n\n'
                    f'TP Totales: {tp_t}\n'
                    f'SL Totales: {sl_t}\n\n'
                    f'Efectividad: {pct}%\n\n'
                    f'Hora: {now_str()}'
                )
            else:
                sl_streak += 1
                daily_pnl -= GAINS[s['asset']].get(0.05, 0) * 0.5
                send(
                    f'❌ SL #{sig_num}\n\n'
                    f'{cfg["icon"]} {asset_label}\n\n'
                    f'TP Totales: {tp_t}\n'
                    f'SL Totales: {sl_t}\n\n'
                    f'Efectividad: {pct}%\n\n'
                    f'Hora: {now_str()}'
                )
                # Alerta racha SL
                if sl_streak >= SL_ALERT_AT:
                    send(
                        f'⚠️ {sl_streak} SL seguidos\n'
                        f'Recalibrando... próxima señal en breve'
                    )

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
    t = n.strftime('%Y-%m-%d')
    if n.hour != 23 or n.minute != 0 or last_d_report == t:
        return
    last_d_report = t
    tg = stats['gold']['tp']; sg = stats['gold']['sl']
    tb = stats['btc']['tp'];  sb = stats['btc']['sl']
    tot = tg+sg+tb+sb; tp = tg+tb
    pct = round(tp/tot*100) if tot else 0
    send(
        f'📊 RESUMEN DEL DÍA\n\n'
        f'Total señales: {tot}\n\n'
        f'✅ TP: {tp}\n'
        f'❌ SL: {tot-tp}\n\n'
        f'Efectividad: {pct}%\n\n'
        f'🥇 ORO\n'
        f'TP: {tg}\n'
        f'SL: {sg}\n\n'
        f'₿ BITCOIN\n'
        f'TP: {tb}\n'
        f'SL: {sb}'
    )
    # Reset diario
    for k in stats:
        stats[k]['tp'] = stats[k]['sl'] = 0
    daily_pnl = 0.0

# =============================================
# ANALIZAR ACTIVO
# =============================================

def analyze(asset, data):
    cfg = ASSETS[asset]
    btc = asset == 'btc'
    fmt = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')
    px  = data['c'][-1]

    # Si ya hay una operación abierta en este activo, esperar
    if locked[asset]:
        log(f'{cfg["icon"]} {fmt(px)} — operación abierta, esperando')
        return

    sig, score = get_signal(data)
    log(f'{cfg["icon"]} {fmt(px)} sig:{sig} score:{score}')

    if not sig or score < SCORE_MIN:
        return

    # Calcular TP/SL
    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo = 'BUY' if isBuy else 'SELL'
    asset_label = 'ORO' if asset == 'gold' else 'BITCOIN'

    tp_t = stats['gold']['tp'] + stats['btc']['tp']
    sl_t = stats['gold']['sl'] + stats['btc']['sl']
    sig_num = tp_t + sl_t + 1

    gains_lines = '\n'.join([
        f'0.01 → {GAINS[asset][0.01]:.0f}€',
        f'0.02 → {GAINS[asset][0.02]:.0f}€',
        f'0.05 → {GAINS[asset][0.05]:.0f}€',
        f'0.08 → {GAINS[asset][0.08]:.0f}€',
        f'0.10 → {GAINS[asset][0.10]:.0f}€',
        f'0.15 → {GAINS[asset][0.15]:.0f}€',
    ])

    send(
        f'🆕 SEÑAL #{sig_num}\n\n'
        f'{cfg["icon"]} {asset_label} - {tipo}\n\n'
        f'Hora: {now_str()}\n\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} (+{cfg["tp"]} puntos)\n'
        f'SL: {fmt(sl)} (-{cfg["sl"]} puntos)\n\n'
        f'Ganancia estimada:\n'
        f'{gains_lines}'
    )
    log(f'SEÑAL #{sig_num} {tipo} {asset_label} {fmt(px)} score:{score}')

    locked[asset]    = True
    lock_time[asset] = time.time()

    active_sigs.append({
        'asset': asset, 'd': sig,
        'e': px, 'tp': tp, 'sl': sl,
        't': time.time(),
    })

# =============================================
# MAIN LOOP
# =============================================

def main():
    log('SNIPER BOT v11.0 INICIANDO')

    while True:
        try:
            daily_report()
            check_active_signals()

            weekday = datetime.now().weekday()  # 5=Sab, 6=Dom
            is_weekend = weekday >= 5

            # ORO — solo entre semana
            if not is_weekend:
                gd = gold_data()
                if gd: analyze('gold', gd)
                else:  log('Sin datos ORO')
            else:
                log('Fin de semana — ORO cerrado')

            time.sleep(3)

            # BTC — 24/7
            bd = btc_data()
            if bd: analyze('btc', bd)
            else:  log('Sin datos BTC')

            time.sleep(15)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
