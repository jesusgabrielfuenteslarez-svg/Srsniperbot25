import os
import time
import json
import requests
from datetime import datetime, date
from collections import deque

# =============================================
# SNIPER BOT v16.0 — ARQUITECTURA COMPLETA
# Basado en: Trading in the Zone + Elder MTF
# Precios: Binance FAPI (= MT5, gratis, ilimitado)
# NUEVO: Clasificador de mercado + Profit Lock
# =============================================
# VARIABLES DE ENTORNO REQUERIDAS (Railway):
#   TG_TOKEN, TG_CHAT_ID, TG_GROUP_ID
# =============================================

TG_TOKEN    = os.environ['TG_TOKEN']
TG_CHAT_ID  = os.environ['TG_CHAT_ID']
TG_GROUP_ID = os.environ['TG_GROUP_ID']
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

# ── Activos ───────────────────────────────────
# Binance Futuros FAPI — símbolos perpetuos
ASSETS = {
    'gold': {
        'name':       'XAU/USD',
        'icon':       '🥇',
        'symbol':     'XAUUSDT',   # perpetuo FAPI
        'tp':         20,
        'sl':         10,
        'val_pto':    0.1,
        'be_trigger': 8,           # pips → break-even
        'trail_step': None,
        'atr_min':    0.6,
    },
    'btc': {
        'name':       'BTC/USD',
        'icon':       '₿',
        'symbol':     'BTCUSDT',   # perpetuo FAPI
        'tp':         500,
        'sl':         200,
        'val_pto':    0.01,
        'be_trigger': 250,
        'trail_step': 150,
        'atr_min':    40.0,
    },
}

LOTAJES = [0.05, 0.08, 0.10, 0.15]

# ── Umbrales ──────────────────────────────────
BASE_THRESHOLD  = 75
OUT_SESSION_THR = 85
MAX_PER_HOUR    = 10

# ── Sesiones UTC ──────────────────────────────
SESSIONS = [(7, 16), (12, 21)]

# ── Daily Stop ────────────────────────────────
DAILY_SL_LIMIT = 3
STATE_FILE     = 'bot_state.json'

# ── Estado global ─────────────────────────────
current_threshold = BASE_THRESHOLD
consecutive_sl    = 0
paused_until      = 0
trade_history     = deque(maxlen=50)
active_signals    = []
last_signal       = {'gold': None, 'btc': None}
last_signal_time  = {'gold': 0,   'btc': 0}
COOLDOWN          = 300
MAX_SIGNAL_AGE    = 7200

hour_signals      = []
last_hour_summary = -1
stats             = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
last_summary_date = None
SUMMARY_HOUR      = 23

daily_sl_count    = 0
daily_stop_date   = None
daily_stopped     = False

# ── Profit Lock ───────────────────────────────
# Si el dia va bien, sube filtros para proteger ganancias
daily_tp_count    = 0
profit_lock       = False      # True = filtros elevados
PROFIT_LOCK_TP    = 5          # TPs para activar lock
PROFIT_LOCK_THR   = 82         # umbral elevado en lock

# ── Cache de velas (evita llamadas redundantes) ─
_cache = {}
CACHE_TTL = 14  # segundos

# =============================================
# PERSISTENCIA — Daily Stop con memoria
# =============================================

def load_state():
    global daily_sl_count, daily_stop_date, daily_stopped, daily_tp_count, profit_lock
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                s = json.load(f)
            today = date.today().isoformat()
            if s.get('date') == today:
                daily_sl_count  = s.get('sl_count', 0)
                daily_tp_count  = s.get('tp_count', 0)
                daily_stopped   = s.get('stopped', False)
                profit_lock     = s.get('p_lock', False)
                daily_stop_date = s.get('date')
            else:
                save_state(reset=True)
    except Exception as e:
        log(f'load_state error: {e}')

def save_state(reset=False):
    global daily_sl_count, daily_stopped, daily_stop_date, daily_tp_count, profit_lock
    today = date.today().isoformat()
    if reset:
        daily_sl_count  = 0
        daily_tp_count  = 0
        daily_stopped   = False
        profit_lock     = False
        daily_stop_date = today
    data = {
        'date':     today,
        'sl_count': daily_sl_count,
        'tp_count': daily_tp_count,
        'stopped':  daily_stopped,
        'p_lock':   profit_lock,
    }
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log(f'save_state error: {e}')

def check_new_day():
    global daily_stop_date
    today = date.today().isoformat()
    if daily_stop_date != today:
        log('Nuevo dia — reset daily stop')
        save_state(reset=True)

# =============================================
# UTILIDADES
# =============================================

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

def is_market_open():
    h = datetime.utcnow().hour
    return any(s <= h < e for s, e in SESSIONS)

def session_name():
    h = datetime.utcnow().hour
    if 7  <= h < 12: return 'Londres'
    if 12 <= h < 16: return 'Londres+NY'
    if 16 <= h < 21: return 'New York'
    return 'Fuera sesion'

# =============================================
# PRECIOS — Binance FAPI (Futuros Perpetuos)
# Gratis, ilimitado, = MT5 al pip
# =============================================

FAPI = 'https://fapi.binance.com'

def fapi_klines(symbol, interval='1m', limit=120):
    """
    Descarga velas de Binance Futuros.
    interval: '1m', '5m', '15m'
    Devuelve lista de closes float, del mas antiguo al mas reciente.
    """
    key = f'{symbol}-{interval}'
    now = time.time()
    if key in _cache and now - _cache[key]['t'] < CACHE_TTL:
        return _cache[key]['data']
    try:
        r = requests.get(f'{FAPI}/fapi/v1/klines', params={
            'symbol': symbol, 'interval': interval, 'limit': limit
        }, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) == 0:
            log(f'FAPI sin datos {symbol} {interval}')
            return None
        closes = [float(k[4]) for k in data]
        _cache[key] = {'t': now, 'data': closes}
        return closes
    except Exception as e:
        log(f'FAPI error {symbol} {interval}: {e}')
        return None

def fapi_spot(symbol):
    """Precio mark price (= spot MT5) en tiempo real."""
    try:
        r = requests.get(f'{FAPI}/fapi/v1/ticker/price',
                         params={'symbol': symbol}, timeout=5)
        return float(r.json()['price'])
    except:
        return None

def get_prices(asset_key):
    """
    Devuelve (p1m, p5m, p15m) usando Binance FAPI.
    p1m: 120 velas 1m
    p5m: 60 velas 5m   (construidas internamente desde 1m Y desde FAPI directo)
    p15m: 30 velas 15m  (construidas internamente)
    """
    sym = ASSETS[asset_key]['symbol']

    # Descarga 1m base (120 velas)
    p1m = fapi_klines(sym, '1m', 120)
    if not p1m or len(p1m) < 30:
        return None, None, None

    # 5m y 15m construidos desde 1m (sin llamada extra)
    p5m  = resample(p1m, 5)
    p15m = resample(p1m, 15)

    # Actualizar último precio con mark price en tiempo real
    spot = fapi_spot(sym)
    if spot and spot > 0:
        if p1m:  p1m[-1]  = spot
        if p5m:  p5m[-1]  = spot
        if p15m: p15m[-1] = spot

    return p1m, p5m, p15m

def get_spot_price(asset_key):
    sym = ASSETS[asset_key]['symbol']
    return fapi_spot(sym)

# =============================================
# CONSTRUCCION MTF INTERNA (sin llamadas extra)
# =============================================

def resample(prices_1m, factor):
    """Agrupa velas 1m en factor*1m. Devuelve closes."""
    out = []
    for i in range(factor - 1, len(prices_1m), factor):
        out.append(prices_1m[i])
    return out if len(out) >= 5 else None

# =============================================
# INDICADORES
# =============================================

def calc_rsi(prices, n=14):
    if len(prices) < n + 1: return 50
    g = l = 0
    for i in range(len(prices) - n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else:     l += abs(d)
    ag, al = g / n, l / n
    if al == 0: return 100
    return round(100 - (100 / (1 + ag / al)), 1)

def calc_ema(prices, n):
    if len(prices) < 2: return prices[-1]
    n   = min(n, len(prices))
    k   = 2 / (n + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_ma(prices, n):
    s = prices[-min(n, len(prices)):]
    return sum(s) / len(s)

def calc_mom(prices, n=5):
    return prices[-1] - prices[-1-n] if len(prices) >= n + 1 else 0

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow + sig + 2: return 0, 0
    def sma(d, n): return sum(d[-n:]) / n if len(d) >= n else 0
    ao = [sma(prices[:i], fast) - sma(prices[:i], slow)
          for i in range(slow, len(prices) + 1)]
    if len(ao) < sig + 1: return 0, 0
    return round(ao[-1] - sma(ao, sig), 4), round(ao[-2] - sma(ao[:-1], sig), 4)

def calc_atr(prices, n=14):
    if len(prices) < n + 1: return 0
    trs = [abs(prices[i] - prices[i-1]) for i in range(-n, 0)]
    return sum(trs) / n

def detect_manipulation(prices):
    if len(prices) < 15: return False, 0
    atr = calc_atr(prices, 10)
    if atr == 0: return False, 0
    last_move = abs(prices[-1] - prices[-2])
    ratio     = last_move / atr
    return ratio > 3.0, round(ratio, 1)

def detect_range(prices):
    if len(prices) < 20:
        return {'is_range': False, 'high': 0, 'low': 0, 'size': 0}
    recent     = prices[-20:]
    high, low  = max(recent), min(recent)
    size       = high - low
    flat       = abs(calc_ma(prices, 5) - calc_ma(prices, 20)) < size * 0.3
    return {'is_range': flat and size > 0, 'high': high, 'low': low, 'size': size}

# =============================================
# TENDENCIA MTF — Elder Triple Pantalla
# 15m = macro | 5m = setup | 1m = timing
# =============================================

def detect_trend_mtf(p1m, p5m, p15m):
    result = {
        'dir': 'neutral', 'strength': 0, 'pullback': False,
        'ema21_5m': 0, 'ma50_5m': 0, 'macro': 'neutral',
        'momentum_1m': 0,
    }

    # ── MACRO: 15m — define si buscamos buy o sell ──
    if p15m and len(p15m) >= 6:
        ema15   = calc_ema(p15m, min(6, len(p15m)))
        ma50_15 = calc_ma(p15m, min(len(p15m), 8))
        if ema15 > ma50_15:  result['macro'] = 'up'
        elif ema15 < ma50_15: result['macro'] = 'down'

    # ── SETUP: 5m ───────────────────────────────────
    if not p5m or len(p5m) < 10:
        return result

    ema21_5  = calc_ema(p5m, min(21, len(p5m)))
    ma50_5   = calc_ma(p5m, min(50, len(p5m)))
    ema_prev = calc_ema(p5m[:-1], min(21, len(p5m)-1))
    px       = p5m[-1]

    result['ema21_5m'] = ema21_5
    result['ma50_5m']  = ma50_5

    if   ema21_5 > ema_prev and px > ma50_5: trend5 = 'up'
    elif ema21_5 < ema_prev and px < ma50_5: trend5 = 'down'
    else:                                     trend5 = 'neutral'

    # Alineacion MTF suma fuerza extra
    macro = result['macro']
    if macro == trend5 and trend5 != 'neutral':
        result['dir']      = trend5
        result['strength'] = 2
    elif trend5 != 'neutral':
        result['dir']      = trend5
        result['strength'] = 1

    # Velas consecutivas en 5m
    for i in range(-1, -6, -1):
        if abs(i) >= len(p5m): break
        if trend5 == 'up'   and p5m[i] > p5m[i-1]: result['strength'] += 1
        elif trend5 == 'down' and p5m[i] < p5m[i-1]: result['strength'] += 1
        else: break

    # Pullback: retrocede hacia media pero tendencia sigue
    if trend5 == 'up'   and px < ema21_5 and px > ma50_5: result['pullback'] = True
    if trend5 == 'down' and px > ema21_5 and px < ma50_5: result['pullback'] = True

    # ── TIMING: 1m — momentum de las últimas 3 velas ──
    if p1m and len(p1m) >= 4:
        last3 = [p1m[-3], p1m[-2], p1m[-1]]
        if all(last3[i] > last3[i-1] for i in range(1, 3)):
            result['momentum_1m'] = 1   # 3 velas alcistas seguidas
        elif all(last3[i] < last3[i-1] for i in range(1, 3)):
            result['momentum_1m'] = -1  # 3 velas bajistas seguidas

    return result

# =============================================
# SALUD DE ESTRATEGIA
# =============================================

def analyze_strategy_health():
    if len(trade_history) < 5:
        return 'ok', 'Pocos datos'
    recientes = list(trade_history)[-10:]
    tp  = sum(1 for t in recientes if t == 'tp')
    sl  = sum(1 for t in recientes if t == 'sl')
    tot = tp + sl
    eff = (tp / tot * 100) if tot > 0 else 50
    if   eff < 40 and tot >= 8: return 'broken',  f'En revision: {round(eff)}% ult {tot}'
    elif eff < 55 and tot >= 5: return 'warning', f'Racha neg: {round(eff)}%'
    elif eff >= 80:              return 'strong',  f'Solida: {round(eff)}%'
    else:                        return 'ok',      f'Normal: {round(eff)}% ult {tot}'

# =============================================
# PROBABILIDAD — Confluencia MTF
# =============================================

def calc_prob(p5m, p1m, direction, range_info, trend):
    prob = 35
    px   = p5m[-1] if p5m else p1m[-1]
    rsi  = calc_rsi(p5m) if p5m else 50
    ac_now, ac_prev = calc_ac(p5m) if p5m and len(p5m) >= 40 else (0, 0)
    mom  = calc_mom(p5m) if p5m else 0
    size = range_info['size'] or 1
    macro   = trend.get('macro', 'neutral')
    mom_1m  = trend.get('momentum_1m', 0)

    # ── Factor 1: Alineacion MTF (peso maximo) ──
    if direction == 'buy':
        if trend['dir'] == 'up':
            prob += 15
            if macro == 'up':   prob += 12   # doble alineacion
            if trend['strength'] >= 3: prob += 8
        elif trend['dir'] == 'neutral':
            prob += 2
        if trend['pullback'] and trend['dir'] == 'up':
            prob += 10
        # Timing 1m a favor
        if mom_1m == 1:  prob += 8
        elif mom_1m == -1: prob -= 6   # timing en contra
    else:
        if trend['dir'] == 'down':
            prob += 15
            if macro == 'down': prob += 12
            if trend['strength'] >= 3: prob += 8
        elif trend['dir'] == 'neutral':
            prob += 2
        if trend['pullback'] and trend['dir'] == 'down':
            prob += 10
        if mom_1m == -1: prob += 8
        elif mom_1m == 1: prob -= 6

    # ── Factor 2: RSI ──────────────────────────
    if direction == 'buy':
        if rsi < 25:   prob += 18
        elif rsi < 35: prob += 12
        elif rsi < 45: prob += 6
        elif rsi > 65: prob -= 10   # contra tendencia fuerte
    else:
        if rsi > 75:   prob += 18
        elif rsi > 65: prob += 12
        elif rsi > 55: prob += 6
        elif rsi < 35: prob -= 10

    # ── Factor 3: AC momentum ─────────────────
    if direction == 'buy':
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0:                    prob += 5
        elif ac_now < 0 and mom > 0:        prob += 3
    else:
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0:                    prob += 5
        elif ac_now > 0 and mom < 0:        prob += 3

    # ── Factor 4: Rango ───────────────────────
    if range_info['is_range']:
        prob += 5
        if direction == 'buy':
            dist = (px - range_info['low']) / size
            if dist < 0.15: prob += 10
            elif dist < 0.30: prob += 5
        else:
            dist = (range_info['high'] - px) / size
            if dist < 0.15: prob += 10
            elif dist < 0.30: prob += 5

    # ── Factor 5: Precio cerca de EMA21 5m ───
    ema21 = trend.get('ema21_5m', 0)
    if ema21 > 0:
        dist_ema = abs(px - ema21) / px * 100
        if dist_ema < 0.1: prob += 7
        elif dist_ema < 0.3: prob += 3

    return min(max(round(prob), 35), 97)

# =============================================
# GESTION BE / TRAILING
# =============================================

def update_signal_management(sig, price):
    cfg    = ASSETS[sig['asset']]
    is_buy = sig['direction'] == 'buy'

    # Break-Even
    be = cfg.get('be_trigger')
    if be and not sig.get('be_activated'):
        trigger = is_buy and price >= sig['entry'] + be
        trigger = trigger or (not is_buy and price <= sig['entry'] - be)
        if trigger:
            new_sl = sig['entry']
            if (is_buy and new_sl > sig['sl']) or (not is_buy and new_sl < sig['sl']):
                sig['sl']          = new_sl
                sig['be_activated'] = True
                log(f'  BE {cfg["icon"]} SL → entrada {sig["sl"]:.2f}')

    # Trailing (BTC)
    trail = cfg.get('trail_step')
    if trail:
        if is_buy:
            best = sig.get('best_price', sig['entry'])
            if price > best:
                sig['best_price'] = price
                steps = int((price - sig['entry']) / trail)
                if steps > 0:
                    new_sl = sig['entry'] + (steps - 1) * trail
                    if new_sl > sig['sl']:
                        sig['sl'] = new_sl
                        log(f'  Trail BTC SL → {round(sig["sl"]):,}')
        else:
            best = sig.get('best_price', sig['entry'])
            if price < best:
                sig['best_price'] = price
                steps = int((sig['entry'] - price) / trail)
                if steps > 0:
                    new_sl = sig['entry'] - (steps - 1) * trail
                    if new_sl < sig['sl']:
                        sig['sl'] = new_sl
                        log(f'  Trail BTC SL → {round(sig["sl"]):,}')

# =============================================
# SEGUIMIENTO TP / SL
# =============================================

def check_active_signals():
    global active_signals, consecutive_sl, current_threshold, paused_until
    global daily_sl_count, daily_stopped

    to_remove = []
    now       = time.time()

    for sig in active_signals:
        cfg     = ASSETS[sig['asset']]
        is_btc  = sig['asset'] == 'btc'
        fmt     = (lambda v: f'{round(v):,}') if is_btc else (lambda v: f'{v:.2f}')
        age_min = round((now - sig['time']) / 60)

        if now - sig['time'] > MAX_SIGNAL_AGE:
            sig['result'] = 'open'
            to_remove.append(sig)
            continue

        price = get_spot_price(sig['asset'])
        if not price: continue

        update_signal_management(sig, price)

        hit_tp = (sig['direction'] == 'buy'  and price >= sig['tp']) or \
                 (sig['direction'] == 'sell' and price <= sig['tp'])
        hit_sl = (sig['direction'] == 'buy'  and price <= sig['sl']) or \
                 (sig['direction'] == 'sell' and price >= sig['sl'])

        if hit_tp:
            sig['result'] = 'tp'
            stats[sig['asset']]['tp'] += 1
            trade_history.append('tp')
            consecutive_sl = 0
            if current_threshold > BASE_THRESHOLD:
                current_threshold = max(current_threshold - 3, BASE_THRESHOLD)
            send_alert(
                f'✅ <b>TP {cfg["icon"]} +{cfg["tp"]} pips</b>\n'
                f'Entrada {fmt(sig["entry"])} → {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

        elif hit_sl:
            sig['result']   = 'sl'
            stats[sig['asset']]['sl'] += 1
            trade_history.append('sl')
            consecutive_sl += 1
            daily_sl_count += 1
            save_state()

            # Daily Stop
            if daily_sl_count >= DAILY_SL_LIMIT:
                daily_stopped = True
                save_state()
                send_alert(
                    f'🛑 <b>DAILY STOP</b>\n'
                    f'{DAILY_SL_LIMIT} SL diarios alcanzados.\n'
                    f'Bot en pausa hasta las 00:00\n'
                    f'{now_str()}'
                )
            elif consecutive_sl >= 3:
                paused_until      = time.time() + 1800
                current_threshold = min(current_threshold + 5, 92)
                consecutive_sl    = 0
                send_alert(
                    f'⏸ <b>PAUSA 30 MIN</b>\n'
                    f'3 SL consecutivos\n'
                    f'Umbral → {current_threshold}%\n'
                    f'{now_str()}'
                )
            else:
                health, _ = analyze_strategy_health()
                if health in ('warning', 'broken'):
                    current_threshold = min(current_threshold + 3, 92)

            send_alert(
                f'❌ <b>SL {cfg["icon"]} -{cfg["sl"]} pips</b>\n'
                f'Entrada {fmt(sig["entry"])} → {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

    for s in to_remove:
        for hs in hour_signals:
            if hs.get('id') == s.get('id'):
                hs['result'] = s.get('result', 'open')
        if s in active_signals:
            active_signals.remove(s)

# =============================================
# RESUMEN HORARIO
# =============================================

def check_hourly_summary():
    global last_hour_summary, hour_signals, current_threshold, consecutive_sl
    now = datetime.now()
    if now.hour == last_hour_summary or now.minute != 0:
        return
    last_hour_summary = now.hour
    if not hour_signals:
        hour_signals = []
        return

    tp_c   = sum(1 for s in hour_signals if s['result'] == 'tp')
    sl_c   = sum(1 for s in hour_signals if s['result'] == 'sl')
    open_c = sum(1 for s in hour_signals if s['result'] == 'open')
    total  = len(hour_signals)
    closed = tp_c + sl_c
    pct    = round((tp_c / closed) * 100) if closed > 0 else 0
    stars  = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
    hora_pasada = f'{(now.hour-1)%24:02d}:00'
    health, health_msg = analyze_strategy_health()
    htxt = f'\n⚠️ {health_msg}' if health=='broken' else \
           f'\n🔶 {health_msg}' if health=='warning' else \
           f'\n💚 {health_msg}' if health=='strong'  else ''

    send_alert(
        f'<b>📊 RESUMEN {hora_pasada}–{now.hour:02d}:00</b>\n'
        f'✅ TP: {tp_c}  ❌ SL: {sl_c}  ⏳ Curso: {open_c}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'Total: {total} senales{htxt}'
    )

    if closed >= 3:
        if pct < 50:
            current_threshold = min(current_threshold + 5, 92)
        elif pct >= 80 and current_threshold > BASE_THRESHOLD:
            current_threshold = max(current_threshold - 3, BASE_THRESHOLD)

    hour_signals   = []
    consecutive_sl = 0

# =============================================
# RESUMEN DIARIO
# =============================================

def check_daily_summary():
    global last_summary_date
    now   = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour == SUMMARY_HOUR and now.minute == 0 and last_summary_date != today:
        last_summary_date = today
        tp_g = stats['gold']['tp']; sl_g = stats['gold']['sl']
        tp_b = stats['btc']['tp'];  sl_b = stats['btc']['sl']
        total_tp = tp_g + tp_b
        total_sl = sl_g + sl_b
        total    = total_tp + total_sl
        pct   = round((total_tp / total)*100) if total > 0 else 0
        stars = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
        health, health_msg = analyze_strategy_health()
        send_alert(
            f'<b>📅 CIERRE DIA {now.strftime("%d/%m")}</b>\n'
            f'✅ TP: {total_tp}   ❌ SL: {total_sl}\n'
            f'Efectividad: <b>{pct}%</b> {stars}\n'
            f'Total: {total} senales\n'
            f'🥇 ORO  {tp_g}TP / {sl_g}SL\n'
            f'₿ BTC  {tp_b}TP / {sl_b}SL\n'
            f'Umbral: {current_threshold}% | {health_msg}'
        )
        for k in stats:
            stats[k]['tp'] = 0
            stats[k]['sl'] = 0

# =============================================
# CLASIFICADOR DE ESTADO DE MERCADO
# TRENDING | RANGING | VOLATILE | DEAD
# =============================================

def classify_market(p5m, atr_min):
    """
    Clasifica el estado del mercado en 4 categorias:
      TRENDING  — tendencia clara, momentum real
      RANGING   — rango definido, rebotes en extremos
      VOLATILE  — spike o expansion brusca, esperando
      DEAD      — lateral muerto, sin movimiento real
    """
    if not p5m or len(p5m) < 20:
        return 'DEAD', 0

    atr    = calc_atr(p5m, min(14, len(p5m)-1))
    recent = p5m[-20:]
    high   = max(recent)
    low    = min(recent)
    size   = high - low
    ma5    = calc_ma(p5m, 5)
    ma20   = calc_ma(p5m, 20)
    mom    = abs(calc_mom(p5m, 5))

    # DEAD: volatilidad por debajo del minimo operativo
    if atr < atr_min * 0.8:
        return 'DEAD', atr

    # VOLATILE: ultima vela movio >3x ATR normal (spike)
    last_move = abs(p5m[-1] - p5m[-2])
    if last_move > atr * 3.0:
        return 'VOLATILE', atr

    # TRENDING: media rapida separada de lenta y momentum claro
    separation = abs(ma5 - ma20) / (size or 1)
    if separation > 0.25 and mom > atr * 0.5:
        return 'TRENDING', atr

    # RANGING: precio oscila dentro de rango sin tendencia clara
    if size > 0 and separation < 0.15:
        return 'RANGING', atr

    # Default: mercado en transicion (tratamos como RANGING)
    return 'RANGING', atr


# =============================================
# PROFIT LOCK — Proteccion de ganancias diarias
# =============================================

def check_profit_lock():
    """
    Si el dia acumula PROFIT_LOCK_TP TPs,
    activa modo conservador: sube umbral a PROFIT_LOCK_THR
    y avisa por Telegram una sola vez.
    """
    global profit_lock, current_threshold
    if profit_lock:
        return
    if daily_tp_count >= PROFIT_LOCK_TP:
        profit_lock       = True
        current_threshold = max(current_threshold, PROFIT_LOCK_THR)
        save_state()
        send_alert(
            f'🔒 <b>PROFIT LOCK ACTIVO</b>\n'
            f'{daily_tp_count} TPs conseguidos hoy.\n'
            f'Modo conservador: umbral → {PROFIT_LOCK_THR}%\n'
            f'Protegiendo ganancias del dia\n'
            f'{now_str()}'
        )
        log(f'Profit Lock activado — umbral {PROFIT_LOCK_THR}%')


# =============================================
# ANALISIS Y SEÑAL — núcleo v16
# =============================================

def analyze_and_alert(asset_key, p1m, p5m, p15m):
    global current_threshold, paused_until, hour_signals, daily_tp_count

    cfg    = ASSETS[asset_key]
    is_btc = asset_key == 'btc'
    fmt    = (lambda v: f'{round(v):,}') if is_btc else (lambda v: f'{v:.2f}')

    if daily_stopped:
        log(f'  Daily stop activo')
        return
    if time.time() < paused_until:
        log(f'  Pausa {round((paused_until-time.time())/60)}min')
        return
    if not p5m or len(p5m) < 10:
        log(f'  {cfg["icon"]} Sin datos 5m')
        return

    px = p5m[-1]

    # ── Clasificador de mercado ────────────────
    market_state, atr = classify_market(p5m, cfg['atr_min'])
    log(f'  {cfg["icon"]} Mercado: {market_state} ATR:{atr:.2f}')

    if market_state == 'DEAD':
        log(f'  {cfg["icon"]} Mercado DEAD — standby')
        return

    if market_state == 'VOLATILE':
        log(f'  {cfg["icon"]} Mercado VOLATILE — esperando estabilizacion')
        return

    # ── Anti-manipulacion ─────────────────────
    is_manip, ratio = detect_manipulation(p5m)
    if is_manip:
        log(f'  {cfg["icon"]} Manip x{ratio} — espera')
        return

    # ── Profit Lock check ─────────────────────
    check_profit_lock()

    # ── Analisis ──────────────────────────────
    rsi        = calc_rsi(p5m)
    range_info = detect_range(p5m)
    trend      = detect_trend_mtf(p1m, p5m, p15m)
    ac_now, ac_prev = calc_ac(p5m) if len(p5m) >= 40 else (0, 0)
    macro      = trend['macro']
    mom_1m     = trend['momentum_1m']

    # ── Bloqueo anti-tendencia ─────────────────
    block_buy  = (macro == 'down' and trend['strength'] >= 3)
    block_sell = (macro == 'up'   and trend['strength'] >= 3)

    # ── Ajuste de umbral segun estado mercado ──
    # RANGING: ser un poco mas exigente (mas falsos cruces)
    umbral_base = max(current_threshold, BASE_THRESHOLD)
    if market_state == 'RANGING':
        umbral_base = min(umbral_base + 3, 92)

    # ── Deteccion de direccion ─────────────────
    sig = None

    # TRENDING: priorizar continuacion fuerte
    if market_state == 'TRENDING':
        if not block_buy and trend['dir'] == 'up' and macro == 'up':
            if rsi < 65 and trend['strength'] >= 2 and mom_1m >= 0:
                sig = 'buy'
        if not block_sell and trend['dir'] == 'down' and macro == 'down':
            if rsi > 35 and trend['strength'] >= 2 and mom_1m <= 0:
                sig = 'sell'
        # Pullback en tendencia
        if not sig:
            if not block_buy  and trend['dir'] == 'up'   and trend['pullback'] and mom_1m >= 0:
                sig = 'buy'
            if not block_sell and trend['dir'] == 'down' and trend['pullback'] and mom_1m <= 0:
                sig = 'sell'

    # RANGING: priorizar extremos de rango
    if market_state == 'RANGING':
        if range_info['is_range'] and range_info['size'] > 0:
            sz = range_info['size']
            if not block_buy  and (px - range_info['low'])  / sz < 0.20 and mom_1m >= 0:
                sig = 'buy'
            if not block_sell and (range_info['high'] - px) / sz < 0.20 and mom_1m <= 0:
                sig = 'sell'

    # Fallback comun (aplica en ambos estados si no hay señal aun)
    if not sig:
        if not block_buy  and trend['dir'] == 'up'   and trend['strength'] >= 2 and rsi < 60:
            sig = 'buy'
        if not block_sell and trend['dir'] == 'down' and trend['strength'] >= 2 and rsi > 40:
            sig = 'sell'
    if not sig:
        if not block_buy  and rsi < 28 and mom_1m >= 0: sig = 'buy'
        if not block_sell and rsi > 72 and mom_1m <= 0: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} '
        f'Tend:{trend["dir"]}({trend["strength"]}) Macro:{macro} '
        f'{market_state} 1m:{mom_1m} -> {sig or "sin senal"}')

    if not sig: return

    # ── Probabilidad ──────────────────────────
    prob   = calc_prob(p5m, p1m, sig, range_info, trend)
    umbral = OUT_SESSION_THR if not is_market_open() else umbral_base

    if prob < umbral:
        log(f'  {prob}% < {umbral}% skip')
        return

    # ── Limite por hora ───────────────────────
    hora_actual  = datetime.now().hour
    senales_hora = [s for s in hour_signals if s['hour'] == hora_actual]
    if len(senales_hora) >= MAX_PER_HOUR:
        log(f'  Limite {MAX_PER_HOUR}/h')
        return

    # ── Cooldown ──────────────────────────────
    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if (last_signal[asset_key] == sig_key and
            time.time() - last_signal_time[asset_key] < COOLDOWN):
        log('  Cooldown')
        return
    last_signal[asset_key]      = sig_key
    last_signal_time[asset_key] = time.time()

    # ── Construir señal ───────────────────────
    isBuy = sig == 'buy'
    tp    = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl    = px - cfg['sl'] if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC↑'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC↓'
    else:                                  ac_txt = 'AC~'

    # Contexto legible con estado de mercado
    if trend['dir'] != 'neutral' and macro == trend['dir']:
        ctx = f"MTF {trend['dir'].upper()} ({trend['strength']}v)"
    elif trend['dir'] != 'neutral':
        ctx = f"Tend {trend['dir'].upper()} ({trend['strength']}v)"
    elif range_info['is_range']:
        ctx = 'Rango'
    else:
        ctx = 'RSI extremo'

    mkt_ico = '📈' if market_state=='TRENDING' else '↔️' if market_state=='RANGING' else '📊'

    # Lotaje recomendado
    if prob >= 90:   lote_rec, lote_ico = '0.10–0.15', '💪'
    elif prob >= 82: lote_rec, lote_ico = '0.05–0.10', '👍'
    else:            lote_rec, lote_ico = '0.05',      '👌'

    lots_txt = '\n'.join([
        f'  {lot:.2f} → +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01):.0f}€'
        for lot in LOTAJES
    ])

    # BE/Trail info
    if cfg.get('be_trigger') and not cfg.get('trail_step'):
        mgmt = f'BE: +{cfg["be_trigger"]}p → SL a entrada\n'
    elif cfg.get('trail_step'):
        mgmt = f'BE: +{cfg["be_trigger"]}p | Trail: +{cfg["trail_step"]}p\n'
    else:
        mgmt = ''

    lock_txt = ' 🔒' if profit_lock else ''
    tipo     = 'BUY 🟢' if isBuy else 'SELL 🔴'
    sig_id   = f'{asset_key}-{int(time.time())}'
    num_h    = len(senales_hora) + 1

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]}  {prob}%{lock_txt}</b>\n'
        f'RSI {rsi} | {ac_txt} | {ctx}\n'
        f'{mkt_ico} {market_state} | {session_name()}\n'
        f'Entrada: <b>{fmt(px)}</b>\n'
        f'TP: {fmt(tp)}  (+{cfg["tp"]}p)\n'
        f'SL: {fmt(sl)}  (-{cfg["sl"]}p)\n'
        f'{mgmt}'
        f'──────────────\n'
        f'{lote_ico} Lote rec: {lote_rec}\n'
        f'{lots_txt}\n'
        f'──────────────\n'
        f'#{num_h} | {now_str()}'
    )
    log(f'SEÑAL {tipo} {fmt(px)} {prob}% | {ctx} | {market_state}')

    entry = {
        'id': sig_id, 'asset': asset_key, 'direction': sig,
        'entry': px, 'tp': tp, 'sl': sl,
        'time': time.time(), 'result': 'open',
        'prob': prob, 'hour': hora_actual,
        'be_activated': False, 'best_price': px,
    }
    active_signals.append(entry)
    hour_signals.append(entry)

# =============================================
# MAIN LOOP
# =============================================

def main():
    load_state()
    log('SNIPER BOT v15.0 INICIANDO — Binance FAPI')
    send_alert(
        '<b>🚀 SNIPER BOT v15.0 ACTIVO</b>\n'
        '📡 Precios: Binance Futuros (= MT5)\n'
        '📊 MTF: 15m + 5m + 1m\n'
        '🛡 BE Auto | Trail BTC | Daily Stop\n'
        f'Umbral sesion: {BASE_THRESHOLD}% | Fuera: {OUT_SESSION_THR}%'
    )

    while True:
        try:
            check_new_day()
            check_hourly_summary()
            check_daily_summary()
            check_active_signals()

            # ORO
            p1m_g, p5m_g, p15m_g = get_prices('gold')
            if p1m_g:
                analyze_and_alert('gold', p1m_g, p5m_g, p15m_g)
            else:
                log('Sin datos ORO')

            time.sleep(3)

            # BTC
            p1m_b, p5m_b, p15m_b = get_prices('btc')
            if p1m_b:
                analyze_and_alert('btc', p1m_b, p5m_b, p15m_b)
            else:
                log('Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('SNIPER BOT v15.0 Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
