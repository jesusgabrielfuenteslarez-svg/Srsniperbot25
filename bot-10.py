import os, time, requests
from datetime import datetime, timezone
from collections import deque

# =============================================
# BOT v18.0 — VERSION DEFINITIVA
# 24/7 | Institucional | Capital primero
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

# ── Sesiones UTC — Barcelona = UTC+2 ─────────
# Sesiones premium: siempre sale al menos 1 señal
# Fuera: opera solo si movimiento excepcional
SESSIONS = [
    (22, 24, 'Sydney'),
    (0,   2, 'Asia'),
    (6,  12, 'Londres'),
    (13, 20, 'NY'),
]
SCORE_SESSION  = 58   # dentro de sesion premium
SCORE_OUT      = 72   # fuera de sesion — solo excepcional
SCORE_CONF     = 48   # alta confluencia rompe lock

# ── Objetivo diario ───────────────────────────
DAILY_TARGET   = 100
SURVIVAL_TARGET= 150

# ── Lotaje dinamico ───────────────────────────
BASE_LOT       = 0.05
MAX_CONCURRENT = 1    # max 1 activo abierto (filtro exposicion global)
MAX_ENTRIES    = 2    # max 2 entradas escalonadas

# ── Estado ────────────────────────────────────
lock           = {'gold': None, 'btc': None}
lock_time      = {'gold': 0,    'btc': 0}
LOCK_TIMEOUT   = 3600
be_moved       = {'gold': False, 'btc': False}
partial_done   = {'gold': False, 'btc': False}
entry_count    = {'gold': 0,    'btc': 0}
current_lot    = {'gold': BASE_LOT, 'btc': BASE_LOT}

news_events    = {}
news_alerted   = set()
last_news_chk  = 0

active_sigs    = []
trade_history  = deque(maxlen=30)
stats          = {'gold': {'tp':0,'sl':0}, 'btc': {'tp':0,'sl':0}}
daily_pnl      = 0.0
survival_mode  = False
day_stopped    = False
last_d_report  = None
last_sig_time  = time.time()
last_silence   = 0
SILENCE_LIMIT  = 7200

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

def utc_hour():
    return datetime.now(timezone.utc).hour

def current_session():
    h = utc_hour()
    for i, (s, e, name) in enumerate(SESSIONS):
        if s <= h < e:
            return i, name
    return None, 'Libre'

# =============================================
# NOTICIAS — ventana precisa 15 min
# =============================================

def check_news():
    global last_news_chk
    if time.time() - last_news_chk < 600:
        return
    last_news_chk = time.time()

    today = datetime.now().strftime('%Y-%m-%d')
    if not hasattr(check_news, '_day') or check_news._day != today:
        check_news._day = today
        news_alerted.clear()
        news_events.clear()

    try:
        r = requests.get(
            'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
            timeout=10)
        for ev in r.json():
            try:
                if ev.get('impact', '') not in ['High', 'high', '3']:
                    continue
                if ev.get('country', '').upper() not in ['USD', 'EUR', 'GBP']:
                    continue
                title = ev.get('title', '')
                kw = ['fed', 'fomc', 'rate', 'cpi', 'nfp', 'gdp', 'powell', 'pce', 'ppi', 'inflation']
                if not any(k in title.lower() for k in kw):
                    continue
                ev_str = f"{ev.get('date', '')} {ev.get('time', '')}"
                try:
                    ev_ts = datetime.strptime(ev_str, '%Y-%m-%d %I:%M%p').timestamp()
                except:
                    continue
                key = f"{ev.get('date', '')}-{title}"
                news_events[key] = ev_ts
                if key not in news_alerted:
                    news_alerted.add(key)
                    t_local = datetime.fromtimestamp(ev_ts).strftime('%H:%M')
                    send(f'NOTICIA: {title}\n{t_local} Barcelona\nPausa 15 min antes/despues')
            except:
                continue
    except Exception as e:
        log(f'News: {e}')

def news_blocked():
    now = time.time()
    for key, ev_ts in news_events.items():
        if abs(now - ev_ts) < 900:
            return True
    return False

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
            'c': [float(v['close'])         for v in reversed(vals)],
            'h': [float(v['high'])          for v in reversed(vals)],
            'l': [float(v['low'])           for v in reversed(vals)],
            'v': [float(v.get('volume', 0)) for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Candles {symbol}: {e}')
        return None

def binance_data(sym, minp):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=40',
            timeout=8)
        k = r.json()
        d = {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        }
        return d if d['c'][-1] > minp else None
    except:
        return None

def gold_data(tf='5min'):
    d = get_candles('XAU/USD', interval=tf)
    if d and d['c'][-1] > 2000:
        return d
    return binance_data('XAUUSDT', 2000) if tf == '5min' else None

def btc_data(tf='5min'):
    d = get_candles('BTC/USD', interval=tf)
    if d and d['c'][-1] > 10000:
        return d
    return binance_data('BTCUSDT', 10000) if tf == '5min' else None

def get_spot(asset):
    sym = 'XAU/USD' if asset == 'gold' else 'BTC/USD'
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': sym, 'apikey': TWELVE_KEY}, timeout=6)
        p = float(r.json().get('price', 0))
        if p > 0:
            return p
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
    if len(c) < n + 1:
        return 50
    g = l = 0
    for i in range(len(c) - n, len(c)):
        d = c[i] - c[i-1]
        if d > 0:
            g += d
        else:
            l -= d
    ag, al = g/n, l/n
    return round(100 - 100/(1 + ag/al), 1) if al else 100

def calc_ema(c, n):
    if len(c) < n:
        return c[-1]
    k = 2/(n + 1)
    e = c[0]
    for p in c[1:]:
        e = p * k + e * (1 - k)
    return e

def calc_atr(h, l, c, n=14):
    if len(c) < n + 1:
        return 0
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# DETECTORES INSTITUCIONALES
# =============================================

def detect_sweep(data):
    c, h, l = data['c'], data['h'], data['l']
    if len(c) < 6:
        return None, 0
    prev_h = max(h[-6:-1])
    prev_l = min(l[-6:-1])
    rng = h[-1] - l[-1]
    if rng == 0:
        return None, 0
    if l[-1] < prev_l and c[-1] > prev_l:
        wick = min(c[-2], c[-1]) - l[-1]
        if wick / rng >= 0.40:
            log('SWEEP alcista detectado')
            return 'buy', 35
    if h[-1] > prev_h and c[-1] < prev_h:
        wick = h[-1] - max(c[-2], c[-1])
        if wick / rng >= 0.40:
            log('SWEEP bajista detectado')
            return 'sell', 35
    return None, 0

def detect_choch(data_m5, data_m15):
    if not data_m15 or not data_m5:
        return None, 0
    c15, h15, l15 = data_m15['c'], data_m15['h'], data_m15['l']
    c5,  h5,  l5  = data_m5['c'],  data_m5['h'],  data_m5['l']
    if len(c15) < 5 or len(c5) < 10:
        return None, 0
    prev_l15 = min(l15[-6:-1])
    prev_h15 = max(h15[-6:-1])
    rng15 = h15[-1] - l15[-1]
    if rng15 == 0:
        return None, 0
    rev = None
    if l15[-1] < prev_l15:
        wick = min(c15[-2], c15[-1]) - l15[-1]
        if wick / rng15 >= 0.40:
            rev = 'buy'
    if h15[-1] > prev_h15 and not rev:
        wick = h15[-1] - max(c15[-2], c15[-1])
        if wick / rng15 >= 0.40:
            rev = 'sell'
    if not rev:
        return None, 0
    if rev == 'buy' and c5[-1] > max(h5[-8:-1]):
        log('CHoCH BUY confirmado')
        return 'buy', 40
    if rev == 'sell' and c5[-1] < min(l5[-8:-1]):
        log('CHoCH SELL confirmado')
        return 'sell', 40
    return None, 0

# =============================================
# QUALITY SCORE
# =============================================

def quality_score(data, direction):
    c, h, l = data['c'], data['h'], data['l']
    score = 0
    reasons = []
    r   = calc_rsi(c)
    e21 = calc_ema(c, 21)
    e50 = calc_ema(c, min(50, len(c)))
    px  = c[-1]
    at  = calc_atr(h, l, c)

    # RSI (0-25)
    if direction == 'buy':
        if r < 30:   score += 25; reasons.append(f'RSI{r}')
        elif r < 40: score += 16; reasons.append(f'RSI{r}')
        elif r < 50: score += 8
        elif r > 65: score -= 12
    else:
        if r > 70:   score += 25; reasons.append(f'RSI{r}')
        elif r > 60: score += 16; reasons.append(f'RSI{r}')
        elif r > 50: score += 8
        elif r < 35: score -= 12

    # EMA (0-20)
    if direction == 'buy':
        if px > e21 > e50: score += 20; reasons.append('EMA+')
        elif px > e21:     score += 12
        elif px < e50:     score -= 8
    else:
        if px < e21 < e50: score += 20; reasons.append('EMA-')
        elif px < e21:     score += 12
        elif px > e50:     score -= 8

    # Momentum 3 velas (0-18)
    if len(c) >= 4:
        up3 = all(c[i] > c[i-1] for i in range(-3, 0))
        dn3 = all(c[i] < c[i-1] for i in range(-3, 0))
        if direction == 'buy'  and up3: score += 18; reasons.append('3v↑')
        if direction == 'sell' and dn3: score += 18; reasons.append('3v↓')
        if direction == 'buy'  and dn3: score -= 10
        if direction == 'sell' and up3: score -= 10

    # Impulso institucional (0-15)
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i] - c[i-1]) for i in range(-5, 0)) / 5
        if avg > 0 and move > avg * 3:
            if (direction == 'buy' and c[-1] > c[-5]) or \
               (direction == 'sell' and c[-1] < c[-5]):
                score += 15; reasons.append('Impulso')

    # Rango (0-12)
    if len(c) >= 20:
        mn = min(l[-20:]); mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if direction == 'buy'  and pos < 0.20: score += 12; reasons.append('Soporte')
            if direction == 'sell' and pos > 0.80: score += 12; reasons.append('Resist')
            if direction == 'buy'  and pos > 0.80: score -= 8
            if direction == 'sell' and pos < 0.20: score -= 8

    # Volatilidad (0-10)
    if at > 0:
        last_move = abs(c[-1] - c[-2])
        ratio = last_move / at
        if ratio < 0.5:  score += 10
        elif ratio > 4:  score -= 20

    return min(max(score, 0), 100), reasons

def volume_ok(data, score):
    v = data.get('v', [])
    if not v or len(v) < 5:
        return True
    avg = sum(v[-10:-1]) / 9 if len(v) >= 10 else sum(v[:-1]) / max(len(v)-1, 1)
    return v[-1] >= avg * 1.2 or score >= 75

def ev_ok(asset):
    recent = list(trade_history)[-10:]
    if len(recent) < 5:
        return True
    tp_r = recent.count('tp')
    sl_r = recent.count('sl')
    tot  = tp_r + sl_r
    if tot == 0:
        return True
    wr = tp_r / tot
    cfg = ASSETS[asset]
    ev  = wr * cfg['tp'] - (1 - wr) * cfg['sl']
    if ev < 0:
        log(f'EV negativo ({round(ev,1)}) — skip')
        return False
    return True

# =============================================
# LOTAJE DINAMICO
# =============================================

def update_lot(asset):
    recent = list(trade_history)[-6:]
    if not recent:
        current_lot[asset] = BASE_LOT
        return
    sl_streak = 0
    for t in reversed(recent):
        if t == 'sl': sl_streak += 1
        else: break
    tp_streak = 0
    for t in reversed(recent):
        if t == 'tp': tp_streak += 1
        else: break
    if sl_streak >= 2:
        current_lot[asset] = max(BASE_LOT * 0.5, 0.01)
        log(f'Racha mala {sl_streak}SL — lote {current_lot[asset]}')
    elif tp_streak >= 3:
        current_lot[asset] = min(BASE_LOT * 1.5, 0.15)
        log(f'Racha buena {tp_streak}TP — lote {current_lot[asset]}')
    else:
        current_lot[asset] = BASE_LOT

# =============================================
# SEGUIMIENTO TP/SL + BREAKEVEN + PARCIAL
# =============================================

def check_active_signals():
    global active_sigs, daily_pnl, day_stopped, survival_mode
    to_rm = []
    now = time.time()

    for s in active_sigs:
        cfg = ASSETS[s['asset']]
        btc = s['asset'] == 'btc'
        fmt = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')
        age = round((now - s['t']) / 60)

        # Timeout
        if now - s['t'] > LOCK_TIMEOUT:
            lock[s['asset']] = None
            be_moved[s['asset']]    = False
            partial_done[s['asset']]= False
            entry_count[s['asset']] = 0
            to_rm.append(s)
            continue

        p = get_spot(s['asset'])
        if not p:
            continue

        move = (p - s['e']) if s['d'] == 'buy' else (s['e'] - p)

        # Breakeven al 50% del TP
        if not be_moved[s['asset']] and move >= cfg['tp'] * 0.5:
            be_moved[s['asset']] = True
            s['sl'] = s['e']
            send(f'🔒 BE {cfg["icon"]} — riesgo cero\nSL movido a {fmt(s["e"])}')
            log(f'BE activado {s["asset"]}')

        # Cierre parcial sugerido al 70% del TP
        if not partial_done[s['asset']] and move >= cfg['tp'] * 0.7:
            partial_done[s['asset']] = True
            gp = round(GAINS[s['asset']].get(s.get('lot', BASE_LOT), 0) * 0.5)
            send(f'💰 PARCIAL {cfg["icon"]} — cierra 50%\n+{gp}€ asegurados')
            log(f'Parcial sugerido {s["asset"]}')

        hit_tp = (s['d'] == 'buy'  and p >= s['tp']) or \
                 (s['d'] == 'sell' and p <= s['tp'])
        hit_sl = (s['d'] == 'buy'  and p <= s['sl']) or \
                 (s['d'] == 'sell' and p >= s['sl'])

        if hit_tp or hit_sl:
            lock[s['asset']]         = None
            be_moved[s['asset']]     = False
            partial_done[s['asset']] = False
            key = 'tp' if hit_tp else 'sl'
            stats[s['asset']][key]  += 1
            trade_history.append(key)

            pnl_op = GAINS[s['asset']].get(s.get('lot', BASE_LOT), 0)
            if hit_sl:
                pnl_op = -pnl_op * 0.5
            daily_pnl += pnl_op

            tp_t = stats['gold']['tp'] + stats['btc']['tp']
            sl_t = stats['gold']['sl'] + stats['btc']['sl']
            tot  = tp_t + sl_t
            pct  = round(tp_t/tot*100) if tot else 0

            sig_num_close = tp_t + sl_t  # numero total incluyendo esta operacion cerrada
            asset_label_c = 'ORO' if s['asset'] == 'gold' else 'BITCOIN'

            if hit_tp:
                entry_count[s['asset']] = s.get('entry_num', 1)
                send(
                    f'✅ TP #{sig_num_close}\n\n'
                    f'{cfg["icon"]} {asset_label_c}\n\n'
                    f'TP Totales: {tp_t}\n'
                    f'SL Totales: {sl_t}\n\n'
                    f'Efectividad: {pct}%\n\n'
                    f'Hora: {now_str()}'
                )
            else:
                entry_count[s['asset']] = 0
                send(
                    f'❌ SL #{sig_num_close}\n\n'
                    f'{cfg["icon"]} {asset_label_c}\n\n'
                    f'TP Totales: {tp_t}\n'
                    f'SL Totales: {sl_t}\n\n'
                    f'Efectividad: {pct}%\n\n'
                    f'Hora: {now_str()}'
                )

            to_rm.append(s)

            # Objetivo diario
            if daily_pnl >= SURVIVAL_TARGET and not day_stopped:
                day_stopped = True
                send(f'OBJETIVO ALCANZADO +{round(daily_pnl)}€\nBot pausado hasta manana')
            elif daily_pnl >= DAILY_TARGET and not survival_mode:
                survival_mode = True
                send(f'META +{round(daily_pnl)}€ — SURVIVAL MODE\nFiltros reforzados')

    for s in to_rm:
        if s in active_sigs:
            active_sigs.remove(s)



# =============================================
# RESUMEN DIARIO
# =============================================

def daily_report():
    global last_d_report, daily_pnl, survival_mode, day_stopped
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
    for k in stats:
        stats[k]['tp'] = stats[k]['sl'] = 0
    daily_pnl    = 0.0
    survival_mode= False
    day_stopped  = False

# =============================================
# ANALISIS PRINCIPAL
# =============================================

def analyze(asset, data):
    global last_sig_time

    if day_stopped:
        return

    cfg  = ASSETS[asset]
    c    = data['c']
    px   = c[-1]
    btc  = asset == 'btc'
    fmt  = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')

    # Noticias
    if news_blocked():
        return

    # Sesion y umbral
    sess_idx, sess_name = current_session()
    score_min = SCORE_SESSION if sess_idx is not None else SCORE_OUT
    if survival_mode:
        score_min = max(score_min + 12, 78)

    # Lock timeout
    now = time.time()
    if lock[asset] == 'locked' and now - lock_time[asset] > LOCK_TIMEOUT:
        lock[asset] = None
        be_moved[asset]    = False
        partial_done[asset]= False
        entry_count[asset] = 0
        log(f'{cfg["icon"]} Lock liberado por timeout')

    # Detectar sweep y CHoCH
    sweep_dir, sweep_bonus = detect_sweep(data)
    m15 = gold_data('15min') if asset == 'gold' else btc_data('15min')
    choch_dir, choch_bonus = detect_choch(data, m15)

    is_high_conf  = bool(sweep_dir or choch_dir)
    hc_dir        = choch_dir or sweep_dir
    hc_bonus      = max(choch_bonus, sweep_bonus)

    # Lock — romper si alta confluencia
    if lock[asset] == 'locked':
        if is_high_conf and hc_dir:
            # Segunda entrada solo si primera ya en BE
            if be_moved[asset] and entry_count[asset] < MAX_ENTRIES:
                lock[asset] = None
                log(f'{cfg["icon"]} Lock roto — 2a entrada en tendencia (BE activo)')
            else:
                log(f'{cfg["icon"]} Locked — esperando BE para 2a entrada')
                return
        else:
            log(f'{cfg["icon"]} Locked — sin alta confluencia')
            return

    # Votos de direccion
    r   = calc_rsi(c)
    e21 = calc_ema(c, 21)
    vb = vs = 0
    if r < 35: vb += 3
    if r > 65: vs += 3
    if r < 45: vb += 1
    if r > 55: vs += 1
    if px > e21: vb += 2
    if px < e21: vs += 2
    if len(c) >= 4:
        if all(c[i] > c[i-1] for i in range(-3, 0)): vb += 3
        if all(c[i] < c[i-1] for i in range(-3, 0)): vs += 3
    if len(c) >= 6:
        move = abs(c[-1] - c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5, 0)) / 5
        if avg > 0 and move > avg * 3:
            if c[-1] > c[-5]: vb += 2
            else:              vs += 2

    if hc_dir:
        sig = hc_dir
    elif vb >= 4 and vb > vs:
        sig = 'buy'
    elif vs >= 4 and vs > vb:
        sig = 'sell'
    else:
        log(f'{cfg["icon"]} {fmt(px)} RSI:{r} — sin señal')
        return

    # Score
    score, reasons = quality_score(data, sig)
    score = min(score + hc_bonus, 100)
    log(f'{cfg["icon"]} {fmt(px)} {sig.upper()} score:{score} min:{score_min}')

    if score < score_min:
        log(f'  {score} < {score_min} — skip')
        return

    # Volumen + EV
    if not volume_ok(data, score):
        return
    if not ev_ok(asset):
        return

    # Exposicion global
    otros = sum(1 for s in active_sigs if s['asset'] != asset)
    if otros >= MAX_CONCURRENT:
        log(f'  Exposicion global maxima')
        return

    # Lotaje dinamico
    update_lot(asset)
    lot = current_lot[asset]

    entry_num = entry_count[asset] + 1
    if entry_num > MAX_ENTRIES:
        log(f'  Max entradas alcanzadas')
        return

    isBuy = sig == 'buy'
    tp    = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl    = px - cfg['sl'] if isBuy else px + cfg['sl']

    if score >= 85:   rec, ico = '0.10-0.15', '💪'
    elif score >= 72: rec, ico = '0.05-0.10', '👍'
    else:             rec, ico = '0.01-0.05', '👌'

    tipo  = 'BUY' if isBuy else 'SELL'
    ctx   = 'CHoCH M15' if choch_dir==sig else 'Sweep' if sweep_dir==sig else ' '.join(reasons[:2])
    tp_t  = stats['gold']['tp'] + stats['btc']['tp']
    sl_t  = stats['gold']['sl'] + stats['btc']['sl']
    tot_t = tp_t + sl_t
    pct_t = round(tp_t/tot_t*100) if tot_t else 0
    sig_num = tp_t + sl_t + 1  # numero total de señal del dia (incluyendo esta)
    e_lbl = f' E{entry_num}/2' if entry_num > 1 else ''

    asset_label = 'ORO' if asset == 'gold' else 'BITCOIN'
    tp_pts  = cfg['tp']
    sl_pts  = cfg['sl']
    tp_sign = f'+{tp_pts}'
    sl_sign = f'-{sl_pts}'

    gains_lines = '\n'.join([
        f'0.01 → {GAINS[asset][0.01]:.0f}€',
        f'0.02 → {GAINS[asset][0.02]:.0f}€',
        f'0.05 → {GAINS[asset][0.05]:.0f}€',
        f'0.08 → {GAINS[asset][0.08]:.0f}€',
        f'0.10 → {GAINS[asset][0.10]:.0f}€',
        f'0.15 → {GAINS[asset][0.15]:.0f}€',
    ])

    send(
        f'🆕 SEÑAL #{sig_num}{e_lbl}\n\n'
        f'{cfg["icon"]} {asset_label} - {tipo}\n\n'
        f'Hora: {now_str()}\n\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} ({tp_sign} puntos)\n'
        f'SL: {fmt(sl)} ({sl_sign} puntos)\n\n'
        f'Ganancia estimada:\n'
        f'{gains_lines}'
    )
    log(f'SEÑAL {tipo} {fmt(px)} score:{score} lote:{lot}')

    last_sig_time       = time.time()
    lock[asset]         = 'locked'
    lock_time[asset]    = time.time()
    entry_count[asset]  = entry_num

    active_sigs.append({
        'asset': asset, 'd': sig,
        'e': px, 'tp': tp, 'sl': sl,
        't': time.time(), 'lot': lot,
        'entry_num': entry_num,
    })

# =============================================
# MAIN
# =============================================

def main():
    log('BOT v18.0 INICIANDO')
    send(
        'BOT v18.0 ACTIVO\n'
        'ORO TP+20 SL-10 | BTC TP+500 SL-200\n'
        'BE auto | Parcial | Survival +100/150EUR\n'
        '24/7 | Londres 08h NY 15h (Barcelona)'
    )

    while True:
        try:
            check_news()
            daily_report()
            check_active_signals()

            if not day_stopped:
                weekday = datetime.now().weekday()  # 5=Sabado, 6=Domingo
                is_weekend = weekday >= 5

                if not is_weekend:
                    gd = gold_data()
                    if gd: analyze('gold', gd)
                    else:  log('Sin datos ORO')
                else:
                    log('Fin de semana — ORO cerrado, solo BTC')

                time.sleep(3)
                bd = btc_data()
                if bd: analyze('btc', bd)
                else:  log('Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send('BOT Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
