import os
import time
import json
import requests
from datetime import datetime, date
from collections import deque

# =============================================
# SNIPER BOT v14.0 — SISTEMA CON VENTAJA REAL
# Basado en: Trading in the Zone + Elder MTF
# =============================================
# VARIABLES DE ENTORNO REQUERIDAS (Railway):
#   TG_TOKEN, TG_CHAT_ID, TG_GROUP_ID, TWELVE_API_KEY
# =============================================

TG_TOKEN    = os.environ['TG_TOKEN']
TG_CHAT_ID  = os.environ['TG_CHAT_ID']
TG_GROUP_ID = os.environ['TG_GROUP_ID']
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

TWELVE_API_KEY = os.environ['TWELVE_API_KEY']

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10,  'val_pto': 0.1,
             'be_trigger': 8,   # pips para activar break-even
             'trail_step': None },
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200, 'val_pto': 0.01,
             'be_trigger': None,
             'trail_step': 150 },  # trailing cada 150 pips
}

LOTAJES = [0.05, 0.08, 0.10, 0.15]

# ── Umbrales ──────────────────────────────────
BASE_THRESHOLD   = 75   # v14: subido de 70 a 75
OUT_SESSION_THR  = 85
MAX_PER_HOUR     = 10

# ── Sesiones UTC ──────────────────────────────
SESSIONS = [(7, 16), (12, 21)]

# ── Daily Stop ────────────────────────────────
DAILY_SL_LIMIT  = 3     # max SL diarios globales
STATE_FILE      = 'bot_state.json'

# ── Volatilidad mínima ATR (evitar mercados muertos) ──
ATR_MIN_GOLD = 0.8   # USD por vela 5m
ATR_MIN_BTC  = 50.0

# ── Estado ────────────────────────────────────
current_threshold = BASE_THRESHOLD
consecutive_sl    = 0
paused_until      = 0
trade_history     = deque(maxlen=50)
active_signals    = []
last_signal       = {'gold': None, 'btc': None}
last_signal_time  = {'gold': 0,    'btc': 0}
COOLDOWN          = 300
MAX_SIGNAL_AGE    = 7200

hour_signals      = []
last_hour_summary = -1
stats = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
last_summary_date = None
SUMMARY_HOUR      = 23

# ── Estado diario (persistente) ───────────────
daily_sl_count  = 0
daily_stop_date = None
daily_stopped   = False

# =============================================
# PERSISTENCIA — Daily Stop con memoria
# =============================================

def load_state():
    global daily_sl_count, daily_stop_date, daily_stopped
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                s = json.load(f)
            today = date.today().isoformat()
            if s.get('date') == today:
                daily_sl_count  = s.get('sl_count', 0)
                daily_stopped   = s.get('stopped', False)
                daily_stop_date = s.get('date')
            else:
                # Nuevo dia — resetear
                save_state(reset=True)
    except Exception as e:
        log(f'load_state error: {e}')

def save_state(reset=False):
    global daily_sl_count, daily_stop_date, daily_stopped
    today = date.today().isoformat()
    if reset:
        daily_sl_count = 0
        daily_stopped  = False
        daily_stop_date = today
    data = {'date': today, 'sl_count': daily_sl_count, 'stopped': daily_stopped}
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log(f'save_state error: {e}')

def check_new_day():
    """Resetea contadores si es un nuevo dia."""
    global daily_sl_count, daily_stop_date, daily_stopped
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
# PRECIOS — Twelve Data + Binance fallback
# =============================================

def get_twelve_prices(symbol, outputsize=120, interval='1min'):
    """Descarga en 1m base. El MTF se construye internamente."""
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': interval,
            'outputsize': outputsize, 'apikey': TWELVE_API_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            log(f'Twelve error [{symbol}]: {data.get("message")}')
            return None
        values = data.get('values', [])
        if not values:
            return None
        # Devuelve lista de closes ordenados del mas antiguo al mas reciente
        return [float(v['close']) for v in reversed(values)]
    except Exception as e:
        log(f'Twelve excepcion: {e}')
        return None

def get_twelve_spot(symbol):
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': symbol, 'apikey': TWELVE_API_KEY}, timeout=8)
        price = float(r.json().get('price', 0))
        return price if price > 0 else None
    except:
        return None

def binance_klines_1m(sym, limit=120):
    r = requests.get(
        f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m&limit={limit}',
        timeout=10)
    return [float(k[4]) for k in r.json()]

def binance_spot(sym):
    t = requests.get(
        f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}',
        timeout=5).json()
    if 'bidPrice' in t:
        return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    return None

def get_prices_1m(asset_key):
    """Descarga velas de 1m. Devuelve lista de closes."""
    symbol_td = 'XAU/USD' if asset_key == 'gold' else 'BTC/USD'
    symbol_bn = 'XAUUSDT' if asset_key == 'gold' else 'BTCUSDT'
    min_price  = 2000 if asset_key == 'gold' else 10000

    prices = get_twelve_prices(symbol_td, outputsize=120, interval='1min')
    if prices and len(prices) >= 30 and prices[-1] > min_price:
        spot = get_twelve_spot(symbol_td)
        if spot and spot > min_price:
            prices[-1] = spot
        return prices
    try:
        p = binance_klines_1m(symbol_bn, 120)
        if p and p[-1] > min_price:
            spot = binance_spot(symbol_bn)
            if spot: p[-1] = spot
            return p
    except:
        pass
    return None

def get_spot_price(asset_key):
    symbol_td = 'XAU/USD' if asset_key == 'gold' else 'BTC/USD'
    symbol_bn = 'XAUUSDT' if asset_key == 'gold' else 'BTCUSDT'
    price = get_twelve_spot(symbol_td)
    if price: return price
    try:
        return binance_spot(symbol_bn)
    except:
        return None

# =============================================
# CONSTRUCCION MTF INTERNA
# 1m → 5m → 15m sin llamadas extra a la API
# =============================================

def resample_to_tf(prices_1m, factor):
    """
    Agrupa velas de 1m en velas de factor*1m.
    Devuelve lista de closes de la TF agrupada.
    """
    out = []
    for i in range(factor - 1, len(prices_1m), factor):
        out.append(prices_1m[i])  # close de la ultima vela del grupo
    return out if len(out) >= 5 else None

def build_mtf(prices_1m):
    """
    Devuelve:
      prices_1m  — timing fino (120 velas)
      prices_5m  — setup       (~24 velas)
      prices_15m — tendencia macro (~8 velas)
    """
    p5  = resample_to_tf(prices_1m, 5)
    p15 = resample_to_tf(prices_1m, 15)
    return prices_1m, p5, p15

# =============================================
# INDICADORES TECNICOS
# =============================================

def calc_rsi(prices, n=14):
    if len(prices) < n + 1: return 50
    g = l = 0
    for i in range(len(prices) - n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g / n, l / n
    if al == 0: return 100
    return round(100 - (100 / (1 + ag / al)), 1)

def calc_ma(prices, n):
    s = prices[-min(n, len(prices)):]
    return sum(s) / len(s)

def calc_ema(prices, n):
    if len(prices) < n: return prices[-1]
    k = 2 / (n + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

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
    """Average True Range como medida de volatilidad."""
    if len(prices) < n + 1: return 0
    trs = [abs(prices[i] - prices[i-1]) for i in range(-n, 0)]
    return sum(trs) / n

def detect_manipulation(prices):
    if len(prices) < 15: return False, 0
    vol = calc_atr(prices, 10)
    if vol == 0: return False, 0
    last_move = abs(prices[-1] - prices[-2])
    ratio = last_move / vol
    return ratio > 3.0, round(ratio, 1)

def detect_range(prices):
    if len(prices) < 20:
        return {'is_range': False, 'high': 0, 'low': 0, 'size': 0}
    recent = prices[-20:]
    high, low = max(recent), min(recent)
    size = high - low
    return {
        'is_range': abs(calc_ma(prices, 5) - calc_ma(prices, 20)) < size * 0.3 and size > 0,
        'high': high, 'low': low, 'size': size
    }

# =============================================
# TENDENCIA — Elder Triple Screen MTF
# 15m = macro, 5m = setup, 1m = timing
# =============================================

def detect_trend_mtf(p1m, p5m, p15m):
    """
    Tendencia macro en 15m.
    Setup en 5m.
    Retorna dirección, fuerza y si hay pullback válido.
    """
    result = {'dir': 'neutral', 'strength': 0, 'pullback': False,
              'ema21_5m': 0, 'ma50_5m': 0, 'macro': 'neutral'}

    # ── Macro: 15m ────────────────────────────
    if p15m and len(p15m) >= 8:
        ema21_15 = calc_ema(p15m, min(8, len(p15m)))
        ema21_15_prev = calc_ema(p15m[:-1], min(8, len(p15m) - 1))
        if ema21_15 > ema21_15_prev: result['macro'] = 'up'
        elif ema21_15 < ema21_15_prev: result['macro'] = 'down'

    # ── Setup: 5m ─────────────────────────────
    if not p5m or len(p5m) < 15:
        return result

    ema21_5   = calc_ema(p5m, 21) if len(p5m) >= 21 else calc_ema(p5m, len(p5m))
    ma50_5    = calc_ma(p5m, min(50, len(p5m)))
    ema21_prev = calc_ema(p5m[:-1], min(21, len(p5m) - 1))
    px        = p5m[-1]

    result['ema21_5m'] = ema21_5
    result['ma50_5m']  = ma50_5

    # Dirección 5m
    if ema21_5 > ema21_prev and px > ma50_5:
        trend5 = 'up'
    elif ema21_5 < ema21_prev and px < ma50_5:
        trend5 = 'down'
    else:
        trend5 = 'neutral'

    # v14: priorizar continuación — macro y 5m deben coincidir
    macro = result['macro']
    if macro == trend5 and trend5 != 'neutral':
        result['dir']      = trend5
        result['strength'] = 2  # base por alineacion MTF
    elif trend5 != 'neutral':
        result['dir']      = trend5
        result['strength'] = 1
    else:
        result['dir'] = 'neutral'

    # Fuerza adicional: velas consecutivas en 5m
    for i in range(-1, -6, -1):
        if abs(i) >= len(p5m): break
        if trend5 == 'up'   and p5m[i] > p5m[i-1]: result['strength'] += 1
        elif trend5 == 'down' and p5m[i] < p5m[i-1]: result['strength'] += 1
        else: break

    # Pullback en 5m: precio retrocede hacia EMA pero tendencia sigue
    if trend5 == 'up'   and px < ema21_5 and px > ma50_5: result['pullback'] = True
    if trend5 == 'down' and px > ema21_5 and px < ma50_5: result['pullback'] = True

    return result

# =============================================
# SALUD DE ESTRATEGIA
# =============================================

def analyze_strategy_health():
    global current_threshold
    if len(trade_history) < 5:
        return 'ok', 'Pocos datos aun'
    recientes  = list(trade_history)[-10:]
    tp_rec     = sum(1 for t in recientes if t == 'tp')
    sl_rec     = sum(1 for t in recientes if t == 'sl')
    total_rec  = tp_rec + sl_rec
    eff_rec    = (tp_rec / total_rec * 100) if total_rec > 0 else 50
    if eff_rec < 40 and total_rec >= 8:
        return 'broken',  f'Estrategia en revision: {round(eff_rec)}% ult {total_rec}'
    elif eff_rec < 55 and total_rec >= 5:
        return 'warning', f'Racha negativa: {round(eff_rec)}%'
    elif eff_rec >= 80:
        return 'strong',  f'Estrategia solida: {round(eff_rec)}%'
    else:
        return 'ok',      f'Normal: {round(eff_rec)}% ult {total_rec}'

# =============================================
# PROBABILIDAD POR CONFLUENCIA — v14
# Prioriza continuación de tendencia MTF
# =============================================

def calc_prob(p5m, p1m, direction, range_info, trend):
    prob = 35
    px   = p5m[-1] if p5m else p1m[-1]
    rsi  = calc_rsi(p5m) if p5m else 50
    ac_now, ac_prev = calc_ac(p5m) if p5m and len(p5m) >= 40 else (0, 0)
    mom  = calc_mom(p5m) if p5m else 0
    size = range_info['size'] or 1

    # ── Factor 1: Alineacion MTF (peso alto en v14) ──
    macro = trend.get('macro', 'neutral')
    if direction == 'buy':
        if trend['dir'] == 'up':
            prob += 15
            if macro == 'up': prob += 10   # doble alineacion MTF
            if trend['strength'] >= 3: prob += 8
        elif trend['dir'] == 'neutral':
            prob += 3
        if trend['pullback'] and trend['dir'] == 'up':
            prob += 12   # pullback en tendencia = zona de valor
    else:
        if trend['dir'] == 'down':
            prob += 15
            if macro == 'down': prob += 10
            if trend['strength'] >= 3: prob += 8
        elif trend['dir'] == 'neutral':
            prob += 3
        if trend['pullback'] and trend['dir'] == 'down':
            prob += 12

    # ── Factor 2: RSI ──────────────────────────────
    if direction == 'buy':
        if rsi < 25:   prob += 18
        elif rsi < 35: prob += 12
        elif rsi < 45: prob += 6
        elif rsi > 60: prob -= 8
    else:
        if rsi > 75:   prob += 18
        elif rsi > 65: prob += 12
        elif rsi > 55: prob += 6
        elif rsi < 40: prob -= 8

    # ── Factor 3: Momentum AC ─────────────────────
    if direction == 'buy':
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0:                    prob += 5
        elif ac_now < 0 and mom > 0:        prob += 3
    else:
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0:                    prob += 5
        elif ac_now > 0 and mom < 0:        prob += 3

    # ── Factor 4: Posicion en rango ───────────────
    if range_info['is_range']:
        prob += 6
        if direction == 'buy':
            dist = (px - range_info['low']) / size
            if dist < 0.15: prob += 12
            elif dist < 0.30: prob += 6
        else:
            dist = (range_info['high'] - px) / size
            if dist < 0.15: prob += 12
            elif dist < 0.30: prob += 6

    # ── Factor 5: Precio cerca de EMA21 (5m) ─────
    ema21 = trend.get('ema21_5m', 0)
    if ema21 > 0:
        dist_ema = abs(px - ema21) / px * 100
        if dist_ema < 0.1: prob += 8
        elif dist_ema < 0.3: prob += 4

    return min(max(round(prob), 35), 97)

# =============================================
# RESUMEN POR HORA
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

    stars = '🌟🌟🌟' if pct == 100 else '🌟🌟' if pct >= 80 else '🌟' if pct >= 60 else ''
    hora_pasada = f'{(now.hour - 1) % 24:02d}:00'
    health, health_msg = analyze_strategy_health()
    health_txt = ''
    if health == 'broken':  health_txt = f'\n⚠️ {health_msg}'
    elif health == 'warning': health_txt = f'\n🔶 {health_msg}'
    elif health == 'strong':  health_txt = f'\n💚 {health_msg}'

    send_alert(
        f'<b>RESUMEN {hora_pasada} - {now.hour:02d}:00</b>\n'
        f'TP: {tp_c}  SL: {sl_c}  Curso: {open_c}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'Total: {total} senales'
        f'{health_txt}'
    )

    if closed >= 3:
        if pct < 50:
            current_threshold = min(current_threshold + 5, 92)
        elif pct >= 80 and current_threshold > BASE_THRESHOLD:
            current_threshold = max(current_threshold - 3, BASE_THRESHOLD)

    hour_signals  = []
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
        pct  = round((total_tp / total) * 100) if total > 0 else 0
        stars = '🌟🌟🌟' if pct == 100 else '🌟🌟' if pct >= 80 else '🌟' if pct >= 60 else ''
        health, health_msg = analyze_strategy_health()
        send_alert(
            f'<b>RESUMEN DIA {now.strftime("%d/%m")}</b>\n'
            f'TP: {total_tp}   SL: {total_sl}\n'
            f'Efectividad: <b>{pct}%</b> {stars}\n'
            f'Total: {total} senales\n'
            f'ORO  {tp_g}TP {sl_g}SL\n'
            f'BTC  {tp_b}TP {sl_b}SL\n'
            f'Umbral: {current_threshold}% | {health_msg}'
        )
        for k in stats:
            stats[k]['tp'] = 0
            stats[k]['sl'] = 0

# =============================================
# SEGUIMIENTO TP / SL / BREAK-EVEN / TRAILING
# =============================================

def update_signal_management(sig, price):
    """
    ORO  → Break-Even agresivo: cuando avanza be_trigger pips, SL → entrada.
    BTC  → Trailing stop: SL se arrastra cada trail_step pips de avance.
    """
    cfg     = ASSETS[sig['asset']]
    is_buy  = sig['direction'] == 'buy'
    changed = False

    # ── BREAK-EVEN (ORO) ──────────────────────
    be = cfg.get('be_trigger')
    if be and not sig.get('be_activated'):
        if is_buy  and price >= sig['entry'] + be:
            new_sl = sig['entry']
            if new_sl > sig['sl']:
                sig['sl'] = new_sl
                sig['be_activated'] = True
                changed = True
                log(f'  BE activado {cfg["icon"]} SL → {sig["sl"]:.2f}')
        elif not is_buy and price <= sig['entry'] - be:
            new_sl = sig['entry']
            if new_sl < sig['sl']:
                sig['sl'] = new_sl
                sig['be_activated'] = True
                changed = True
                log(f'  BE activado {cfg["icon"]} SL → {sig["sl"]:.2f}')

    # ── TRAILING STOP (BTC) ────────────────────
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
                        changed = True
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
                        changed = True
                        log(f'  Trail BTC SL → {round(sig["sl"]):,}')

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

        # Actualizar BE / trailing antes de evaluar TP/SL
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
                f'<b>TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                f'{fmt(sig["entry"])} → {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

        elif hit_sl:
            sig['result']    = 'sl'
            stats[sig['asset']]['sl'] += 1
            trade_history.append('sl')
            consecutive_sl  += 1
            daily_sl_count  += 1
            save_state()

            # ── Daily Stop ─────────────────────
            if daily_sl_count >= DAILY_SL_LIMIT:
                daily_stopped = True
                save_state()
                send_alert(
                    f'<b>🛑 DAILY STOP ACTIVADO</b>\n'
                    f'{DAILY_SL_LIMIT} SL alcanzados hoy.\n'
                    f'Bot pausado hasta las 00:00\n'
                    f'{now_str()}'
                )

            # ── Pausa por 3 SL consecutivos ────
            elif consecutive_sl >= 3:
                paused_until      = time.time() + 1800
                current_threshold = min(current_threshold + 5, 92)
                consecutive_sl    = 0
                send_alert(
                    f'<b>PAUSA 30 MIN</b>\n'
                    f'3 SL consecutivos\n'
                    f'Nuevo umbral: {current_threshold}%\n'
                    f'{now_str()}'
                )
            else:
                health, _ = analyze_strategy_health()
                if health in ('warning', 'broken'):
                    current_threshold = min(current_threshold + 3, 92)

            send_alert(
                f'<b>SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                f'{fmt(sig["entry"])} → {fmt(price)}\n'
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
# ANALISIS Y ENVIO DE SEÑAL — v14
# =============================================

def analyze_and_alert(asset_key, prices_1m):
    global current_threshold, paused_until, hour_signals

    cfg    = ASSETS[asset_key]
    is_btc = asset_key == 'btc'
    fmt    = (lambda v: f'{round(v):,}') if is_btc else (lambda v: f'{v:.2f}')

    # ── Daily Stop ────────────────────────────
    if daily_stopped:
        log(f'  Daily stop activo — sin operaciones')
        return

    # ── Pausa activa ──────────────────────────
    if time.time() < paused_until:
        mins = round((paused_until - time.time()) / 60)
        log(f'  Pausa activa — {mins} min')
        return

    # ── Construir MTF internamente ─────────────
    p1m, p5m, p15m = build_mtf(prices_1m)

    if not p5m or len(p5m) < 15:
        log(f'  {cfg["icon"]} Insuficientes datos 5m')
        return

    px = p5m[-1]

    # ── Filtro de volatilidad (ATR) ────────────
    atr_min = ATR_MIN_BTC if is_btc else ATR_MIN_GOLD
    atr_5m  = calc_atr(p5m, min(14, len(p5m) - 1))
    if atr_5m < atr_min:
        log(f'  {cfg["icon"]} Volatilidad baja ATR={atr_5m:.2f} < {atr_min} — standby')
        return

    # ── Anti-manipulacion ─────────────────────
    is_manip, manip_ratio = detect_manipulation(p5m)
    if is_manip:
        log(f'  {cfg["icon"]} Movimiento brusco x{manip_ratio} — esperando')
        return

    # ── Analisis tecnico ──────────────────────
    rsi        = calc_rsi(p5m)
    range_info = detect_range(p5m)
    trend      = detect_trend_mtf(p1m, p5m, p15m)
    ac_now, ac_prev = calc_ac(p5m) if len(p5m) >= 40 else (0, 0)

    # ── Deteccion de direccion — v14 prioriza continuacion ──
    sig = None

    # Prioridad 1: Continuacion limpia MTF alineada (nueva en v14)
    if trend['dir'] == 'up' and trend['macro'] == 'up':
        if rsi < 60 and trend['strength'] >= 2: sig = 'buy'
    if trend['dir'] == 'down' and trend['macro'] == 'down':
        if rsi > 40 and trend['strength'] >= 2: sig = 'sell'

    # Prioridad 2: Pullback en tendencia (zona de valor)
    if not sig:
        if trend['dir'] == 'up'   and trend['pullback']: sig = 'buy'
        if trend['dir'] == 'down' and trend['pullback']: sig = 'sell'

    # Prioridad 3: Tendencia con momentum en 5m
    if not sig:
        if trend['dir'] == 'up'   and trend['strength'] >= 2 and rsi < 55: sig = 'buy'
        if trend['dir'] == 'down' and trend['strength'] >= 2 and rsi > 45: sig = 'sell'

    # Prioridad 4: RSI extremo
    if not sig:
        if rsi < 28: sig = 'buy'
        elif rsi > 72: sig = 'sell'

    # Prioridad 5: Extremos de rango
    if not sig and range_info['is_range'] and range_info['size'] > 0:
        sz = range_info['size']
        if (px - range_info['low'])  / sz < 0.20: sig = 'buy'
        elif (range_info['high'] - px) / sz < 0.20: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} '
        f'Tend:{trend["dir"]}({trend["strength"]}) Macro:{trend["macro"]} ATR:{atr_5m:.1f} '
        f'-> {sig or "sin senal"}')
    if not sig: return

    # ── Probabilidad ──────────────────────────
    prob   = calc_prob(p5m, p1m, sig, range_info, trend)
    umbral = OUT_SESSION_THR if not is_market_open() else max(current_threshold, BASE_THRESHOLD)

    if prob < umbral:
        log(f'  {prob}% < {umbral}% ({session_name()}), skip')
        return

    # ── Control por hora (sin MIN_STRONG — eliminado en v14) ──
    hora_actual  = datetime.now().hour
    senales_hora = [s for s in hour_signals if s['hour'] == hora_actual]

    if len(senales_hora) >= MAX_PER_HOUR:
        log(f'  Limite {MAX_PER_HOUR}/hora')
        return

    # ── Cooldown por activo ────────────────────
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

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC+'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC-'
    else:                                  ac_txt = 'AC~'

    # Contexto
    if trend['dir'] != 'neutral' and trend['macro'] == trend['dir']:
        ctx = f"MTF {trend['dir'].upper()} ({trend['strength']}v)"
    elif trend['dir'] != 'neutral':
        ctx = f"Tend {trend['dir'].upper()} ({trend['strength']}v)"
    elif range_info['is_range']:
        ctx = 'Rango'
    else:
        ctx = 'RSI extremo'

    # Lotaje
    if prob >= 90:
        lote_rec = '0.10 - 0.15'; lote_ico = '💪'
    elif prob >= 82:
        lote_rec = '0.05 - 0.10'; lote_ico = '👍'
    else:
        lote_rec = '0.05';        lote_ico = '👌'

    lots_txt = '\n'.join([
        f'{lot:.2f} → +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01):.0f}EUR'
        for lot in LOTAJES
    ])

    tipo   = 'BUY' if isBuy else 'SELL'
    sig_id = f'{asset_key}-{int(time.time())}'
    num_h  = len(senales_hora) + 1

    # BE/Trail info en el mensaje
    mgmt_txt = ''
    if cfg.get('be_trigger'):
        mgmt_txt = f'BE: +{cfg["be_trigger"]} pips → SL a entrada\n'
    elif cfg.get('trail_step'):
        mgmt_txt = f'Trail: cada +{cfg["trail_step"]} pips\n'

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {prob}%</b>\n'
        f'RSI {rsi} | {ac_txt} | {ctx}\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} (+{cfg["tp"]})\n'
        f'SL: {fmt(sl)} (-{cfg["sl"]})\n'
        f'{mgmt_txt}'
        f'---\n'
        f'{lote_ico} Lote: {lote_rec}\n'
        f'{lots_txt}\n'
        f'Senal {num_h}/{MAX_PER_HOUR} | {session_name()} | {now_str()}'
    )
    log(f'{tipo} {fmt(px)} {prob}% | {ctx}')

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
# MAIN
# =============================================

def main():
    load_state()
    log('SNIPER BOT v14.0 INICIANDO')
    send_alert(
        '<b>SNIPER BOT v14.0 ACTIVO</b>\n'
        'MTF: 15m+5m+1m | Anti-manipulacion\n'
        'Break-Even ORO | Trailing BTC\n'
        'Daily Stop | Adaptativo\n'
        f'Umbral sesion: {BASE_THRESHOLD}% | Fuera: {OUT_SESSION_THR}%'
    )

    while True:
        try:
            check_new_day()
            check_hourly_summary()
            check_daily_summary()
            check_active_signals()

            gold_1m = get_prices_1m('gold')
            if gold_1m: analyze_and_alert('gold', gold_1m)

            time.sleep(3)

            btc_1m = get_prices_1m('btc')
            if btc_1m: analyze_and_alert('btc', btc_1m)

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('SNIPER BOT v14.0 Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
