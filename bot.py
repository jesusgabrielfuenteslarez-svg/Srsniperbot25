import os
import time
import json
import requests
from datetime import datetime, date
from collections import deque

# ================================================================
# SNIPER BOT v17.0 — EDICION DEFINITIVA 10/10
# Precios   : Binance FAPI (= MT5, gratis, ilimitado)
# Estrategia: Elder Triple Pantalla MTF
# Features  : Trend Rider | Objetivo Diario | Survival Mode
#             Solo 1 señal/activo | Filtro Noticias ForexFactory
#             Filtro Volumen | Detector Liquidez | EV Filter
#             Clasificador Mercado | Profit Lock | Daily Stop
#             Break-Even | Trailing Stop | Memoria Adaptativa
#             Session Kill Switch | Horas Sniper
# ================================================================
# VARIABLES DE ENTORNO (Railway):
#   TG_TOKEN, TG_CHAT_ID, TG_GROUP_ID
# ================================================================

TG_TOKEN    = os.environ['TG_TOKEN']
TG_CHAT_ID  = os.environ['TG_CHAT_ID']
TG_GROUP_ID = os.environ['TG_GROUP_ID']
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

FAPI = 'https://fapi.binance.com'

# ── Activos ───────────────────────────────────────────────────
ASSETS = {
    'gold': {
        'name': 'XAU/USD', 'icon': '🥇', 'symbol': 'XAUUSDT',
        'tp': 20, 'sl': 10, 'val_pto': 0.1,
        'be_trigger': 8, 'trail_step': None, 'atr_min': 0.6,
        'daily_target_contrib': 15,  # EUR estimado por TP a 0.10
    },
    'btc': {
        'name': 'BTC/USD', 'icon': '₿', 'symbol': 'BTCUSDT',
        'tp': 500, 'sl': 200, 'val_pto': 0.01,
        'be_trigger': 250, 'trail_step': 150, 'atr_min': 40.0,
        'daily_target_contrib': 50,
    },
}

LOTAJES = [0.05, 0.08, 0.10, 0.15]

# ── Umbrales base ─────────────────────────────────────────────
BASE_THRESHOLD   = 75
OUT_SESSION_THR  = 85
SURVIVAL_THR     = 85   # umbral en Survival Mode
MAX_PER_HOUR     = 10
MAX_PER_HOUR_SRV = 2    # max señales/hora en Survival Mode

# ── Sesiones Sniper UTC (horas de alta calidad) ───────────────
SNIPER_SESSIONS  = [(7, 21)]   # Londres + NY completo
OVERLAP_START    = 12          # inicio overlap
OVERLAP_END      = 16          # fin overlap

# ── Daily Stop ────────────────────────────────────────────────
DAILY_SL_LIMIT   = 3
STATE_FILE       = 'bot_state.json'
MEMORY_FILE      = 'bot_memory.json'

# ── Objetivo diario (EUR estimado) ───────────────────────────
DAILY_TARGET_1   = 100   # → Survival Mode
DAILY_TARGET_2   = 150   # → Apagar hasta mañana

# ── Profit Lock ───────────────────────────────────────────────
PROFIT_LOCK_TP   = 5
PROFIT_LOCK_THR  = 82

# ── Trend Rider ───────────────────────────────────────────────
TREND_RIDER_MAX  = 3     # max reentradas seguidas
TREND_RIDER_CD   = 180   # cooldown entre reentradas (seg)

# ── Noticias de alto impacto (palabras clave) ─────────────────
HIGH_IMPACT_KEYWORDS = [
    'Non-Farm', 'NFP', 'CPI', 'FOMC', 'Fed Rate', 'Powell',
    'Interest Rate', 'GDP', 'Unemployment', 'Inflation',
    'PPI', 'Retail Sales', 'ISM',
]

# ── Cooldown y seguimiento ────────────────────────────────────
COOLDOWN       = 300
MAX_SIGNAL_AGE = 7200

# =============================================================
# ESTADO GLOBAL
# =============================================================

current_threshold  = BASE_THRESHOLD
consecutive_sl     = 0
paused_until       = 0
trade_history      = deque(maxlen=100)
active_signals     = []
last_signal        = {'gold': None, 'btc': None}
last_signal_time   = {'gold': 0,   'btc': 0}

hour_signals       = []
last_hour_summary  = -1
stats              = {'gold': {'tp':0,'sl':0}, 'btc': {'tp':0,'sl':0}}
last_summary_date  = None
SUMMARY_HOUR       = 23

# Daily
daily_sl_count     = 0
daily_tp_count     = 0
daily_stop_date    = None
daily_stopped      = False
daily_pnl_eur      = 0.0   # P&L estimado del dia

# Modos
profit_lock        = False
survival_mode      = False
day_target_hit     = False   # True = apagado hasta mañana

# Trend Rider
trend_rider        = {'gold': 0, 'btc': 0}   # contador reentradas
trend_rider_time   = {'gold': 0, 'btc': 0}

# Session Kill Switch
session_kill       = {'london': False, 'ny': False}
session_sl_count   = {'london': 0,     'ny': 0}
SESSION_KILL_SL    = 3   # SLs en sesion para bloquearla

# Cache
_cache    = {}
CACHE_TTL = 14

# Noticias
_news_cache      = []
_news_cache_time = 0
NEWS_CACHE_TTL   = 900   # 15 min

# Memoria adaptativa
memory = {
    'best_hour':    {},   # {hora: {tp, sl}}
    'best_session': {},   # {session: {tp, sl}}
    'best_asset':   {'gold': {'tp':0,'sl':0}, 'btc': {'tp':0,'sl':0}},
    'best_dir':     {'buy': 0, 'sell': 0},
}

# =============================================================
# PERSISTENCIA
# =============================================================

def load_state():
    global daily_sl_count, daily_tp_count, daily_stop_date, daily_stopped
    global profit_lock, survival_mode, day_target_hit, daily_pnl_eur
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                s = json.load(f)
            today = date.today().isoformat()
            if s.get('date') == today:
                daily_sl_count  = s.get('sl_count', 0)
                daily_tp_count  = s.get('tp_count', 0)
                daily_stopped   = s.get('stopped',  False)
                profit_lock     = s.get('p_lock',   False)
                survival_mode   = s.get('survival', False)
                day_target_hit  = s.get('day_done', False)
                daily_pnl_eur   = s.get('pnl',      0.0)
                daily_stop_date = s.get('date')
            else:
                save_state(reset=True)
    except Exception as e:
        log(f'load_state error: {e}')

def save_state(reset=False):
    global daily_sl_count, daily_tp_count, daily_stopped, daily_stop_date
    global profit_lock, survival_mode, day_target_hit, daily_pnl_eur
    today = date.today().isoformat()
    if reset:
        daily_sl_count = daily_tp_count = 0
        daily_stopped  = profit_lock = survival_mode = day_target_hit = False
        daily_pnl_eur  = 0.0
        daily_stop_date = today
        session_kill['london'] = session_kill['ny'] = False
        session_sl_count['london'] = session_sl_count['ny'] = 0
        trend_rider['gold'] = trend_rider['btc'] = 0
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({
                'date':     today,
                'sl_count': daily_sl_count,
                'tp_count': daily_tp_count,
                'stopped':  daily_stopped,
                'p_lock':   profit_lock,
                'survival': survival_mode,
                'day_done': day_target_hit,
                'pnl':      daily_pnl_eur,
            }, f)
    except Exception as e:
        log(f'save_state error: {e}')

def load_memory():
    global memory
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE) as f:
                memory = json.load(f)
    except:
        pass

def save_memory():
    try:
        with open(MEMORY_FILE, 'w') as f:
            json.dump(memory, f)
    except:
        pass

def check_new_day():
    global daily_stop_date, current_threshold
    today = date.today().isoformat()
    if daily_stop_date != today:
        log('Nuevo dia — reset completo')
        save_state(reset=True)
        current_threshold = BASE_THRESHOLD

# =============================================================
# UTILIDADES
# =============================================================

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

def utc_hour():
    return datetime.utcnow().hour

def is_sniper_hour():
    h = utc_hour()
    return any(s <= h < e for s, e in SNIPER_SESSIONS)

def session_name():
    h = utc_hour()
    if 7  <= h < 12: return 'Londres'
    if 12 <= h < 16: return 'Londres+NY ⚡'
    if 16 <= h < 21: return 'New York'
    return 'Fuera sesion'

def current_session_key():
    h = utc_hour()
    if 7  <= h < 16: return 'london'
    if 12 <= h < 21: return 'ny'
    return None

def is_session_killed():
    sk = current_session_key()
    if sk and session_kill.get(sk):
        return True, sk
    return False, None

def est_profit(asset_key, lot=0.10):
    cfg = ASSETS[asset_key]
    return round(cfg['tp'] * cfg['val_pto'] * lot / 0.01)

# =============================================================
# PRECIOS — Binance FAPI
# =============================================================

def fapi_klines(symbol, interval='1m', limit=120):
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
            return None
        # closes y volumes
        closes  = [float(k[4]) for k in data]
        volumes = [float(k[5]) for k in data]
        _cache[key] = {'t': now, 'data': closes, 'vol': volumes}
        return closes
    except Exception as e:
        log(f'FAPI error {symbol} {interval}: {e}')
        return None

def fapi_volumes(symbol, interval='1m', limit=120):
    key = f'{symbol}-{interval}'
    if key in _cache:
        return _cache[key].get('vol')
    fapi_klines(symbol, interval, limit)
    return _cache.get(key, {}).get('vol')

def fapi_spot(symbol):
    try:
        r = requests.get(f'{FAPI}/fapi/v1/ticker/price',
                         params={'symbol': symbol}, timeout=5)
        return float(r.json()['price'])
    except:
        return None

def resample(prices_1m, factor):
    out = []
    for i in range(factor - 1, len(prices_1m), factor):
        out.append(prices_1m[i])
    return out if len(out) >= 5 else None

def get_prices(asset_key):
    sym  = ASSETS[asset_key]['symbol']
    p1m  = fapi_klines(sym, '1m', 120)
    if not p1m or len(p1m) < 30:
        return None, None, None
    p5m  = resample(p1m, 5)
    p15m = resample(p1m, 15)
    spot = fapi_spot(sym)
    if spot and spot > 0:
        if p1m:  p1m[-1]  = spot
        if p5m:  p5m[-1]  = spot
        if p15m: p15m[-1] = spot
    return p1m, p5m, p15m

def get_spot_price(asset_key):
    return fapi_spot(ASSETS[asset_key]['symbol'])

# =============================================================
# NOTICIAS — ForexFactory scraping
# Evita operar en CPI, NFP, FOMC, Powell, etc.
# =============================================================

def fetch_news():
    global _news_cache, _news_cache_time
    now = time.time()
    if now - _news_cache_time < NEWS_CACHE_TTL:
        return _news_cache
    try:
        r = requests.get(
            'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
            timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
        )
        data = r.json()
        high = [e for e in data if e.get('impact') == 'High']
        _news_cache      = high
        _news_cache_time = now
        return high
    except:
        return _news_cache

def is_news_window():
    """
    True si hay noticia de alto impacto en los proximos 30 min
    o en los ultimos 15 min.
    """
    try:
        news = fetch_news()
        now_dt = datetime.utcnow()
        for ev in news:
            try:
                ev_dt = datetime.strptime(ev['date'] + ' ' + ev['time'], '%m-%d-%Y %I:%M%p')
            except:
                continue
            diff = (ev_dt - now_dt).total_seconds() / 60
            if -15 <= diff <= 30:
                title = ev.get('title', '')
                if any(kw.lower() in title.lower() for kw in HIGH_IMPACT_KEYWORDS):
                    log(f'  📰 Noticia: {title} en {round(diff)}min — bloqueando')
                    return True, title
        return False, ''
    except:
        return False, ''

# =============================================================
# INDICADORES
# =============================================================

def calc_rsi(prices, n=14):
    if len(prices) < n + 1: return 50
    g = l = 0
    for i in range(len(prices) - n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else:     l += abs(d)
    ag, al = g/n, l/n
    if al == 0: return 100
    return round(100 - (100 / (1 + ag/al)), 1)

def calc_ema(prices, n):
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
    return prices[-1] - prices[-1-n] if len(prices) >= n+1 else 0

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow + sig + 2: return 0, 0
    def sma(d, n): return sum(d[-n:]) / n if len(d) >= n else 0
    ao = [sma(prices[:i], fast) - sma(prices[:i], slow)
          for i in range(slow, len(prices)+1)]
    if len(ao) < sig + 1: return 0, 0
    return round(ao[-1] - sma(ao, sig), 4), round(ao[-2] - sma(ao[:-1], sig), 4)

def calc_atr(prices, n=14):
    if len(prices) < n+1: return 0
    return sum(abs(prices[i] - prices[i-1]) for i in range(-n, 0)) / n

def detect_manipulation(prices):
    if len(prices) < 15: return False, 0
    atr = calc_atr(prices, 10)
    if atr == 0: return False, 0
    ratio = abs(prices[-1] - prices[-2]) / atr
    return ratio > 3.0, round(ratio, 1)

def detect_range(prices):
    if len(prices) < 20:
        return {'is_range': False, 'high': 0, 'low': 0, 'size': 0}
    recent    = prices[-20:]
    high, low = max(recent), min(recent)
    size      = high - low
    flat      = abs(calc_ma(prices, 5) - calc_ma(prices, 20)) < size * 0.3
    return {'is_range': flat and size > 0, 'high': high, 'low': low, 'size': size}

# =============================================================
# FILTRO DE VOLUMEN REAL
# Evita entrar en rupturas sin respaldo de volumen
# =============================================================

def volume_confirms(asset_key, interval='1m', lookback=10):
    """
    True si el volumen actual esta por encima de la media
    de los ultimos `lookback` periodos (ruptura con volumen real).
    """
    sym  = ASSETS[asset_key]['symbol']
    vols = fapi_volumes(sym, interval, 60)
    if not vols or len(vols) < lookback + 1:
        return True  # sin datos: no bloquear
    avg = sum(vols[-(lookback+1):-1]) / lookback
    return vols[-1] >= avg * 0.85   # al menos 85% de la media

# =============================================================
# DETECTOR DE LIQUIDEZ
# Detecta barridas, falsas rupturas y caza de stops
# =============================================================

def detect_liquidity_hunt(prices, atr):
    """
    Detecta si la ultima vela fue una barrida de liquidez:
    - rompio un maximo/minimo reciente
    - pero cerro de vuelta dentro del rango anterior
    Señal de trampa institucional → NO entrar
    """
    if len(prices) < 10 or atr == 0:
        return False
    recent_high = max(prices[-10:-1])
    recent_low  = min(prices[-10:-1])
    px          = prices[-1]
    prev        = prices[-2]
    spike_up    = prev > recent_high and px < recent_high  # falsa ruptura arriba
    spike_down  = prev < recent_low  and px > recent_low   # falsa ruptura abajo
    return spike_up or spike_down

# =============================================================
# EXPECTED VALUE FILTER
# Solo opera si la esperanza matematica es positiva
# EV = (winrate * tp_pips) - ((1-winrate) * sl_pips)
# =============================================================

def calc_expected_value(prob, tp_pips, sl_pips):
    """
    Calcula el valor esperado en pips.
    prob: probabilidad estimada (0-1)
    Retorna EV en pips. Positivo = vale la pena.
    """
    ev = (prob * tp_pips) - ((1 - prob) * sl_pips)
    return round(ev, 2)

def ev_is_positive(prob, asset_key):
    cfg = ASSETS[asset_key]
    ev  = calc_expected_value(prob / 100, cfg['tp'], cfg['sl'])
    log(f'  EV={ev:.1f} pips')
    return ev > 0

# =============================================================
# CLASIFICADOR DE ESTADO DE MERCADO
# TRENDING | RANGING | VOLATILE | DEAD
# =============================================================

def classify_market(p5m, atr_min):
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
    if atr < atr_min * 0.8:
        return 'DEAD', atr
    if abs(p5m[-1] - p5m[-2]) > atr * 3.0:
        return 'VOLATILE', atr
    sep = abs(ma5 - ma20) / (size or 1)
    if sep > 0.25 and mom > atr * 0.5:
        return 'TRENDING', atr
    return 'RANGING', atr

# =============================================================
# TENDENCIA MTF — Elder Triple Pantalla
# =============================================================

def detect_trend_mtf(p1m, p5m, p15m):
    result = {
        'dir': 'neutral', 'strength': 0, 'pullback': False,
        'ema21_5m': 0, 'ma50_5m': 0, 'macro': 'neutral',
        'momentum_1m': 0,
    }
    if p15m and len(p15m) >= 6:
        ema15   = calc_ema(p15m, min(6, len(p15m)))
        ma50_15 = calc_ma(p15m, min(len(p15m), 8))
        if ema15 > ma50_15:   result['macro'] = 'up'
        elif ema15 < ma50_15: result['macro'] = 'down'

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

    macro = result['macro']
    if macro == trend5 and trend5 != 'neutral':
        result['dir'] = trend5; result['strength'] = 2
    elif trend5 != 'neutral':
        result['dir'] = trend5; result['strength'] = 1

    for i in range(-1, -6, -1):
        if abs(i) >= len(p5m): break
        if trend5 == 'up'   and p5m[i] > p5m[i-1]: result['strength'] += 1
        elif trend5 == 'down' and p5m[i] < p5m[i-1]: result['strength'] += 1
        else: break

    if trend5 == 'up'   and px < ema21_5 and px > ma50_5: result['pullback'] = True
    if trend5 == 'down' and px > ema21_5 and px < ma50_5: result['pullback'] = True

    if p1m and len(p1m) >= 4:
        last3 = p1m[-3:]
        if all(last3[i] > last3[i-1] for i in range(1, 3)):
            result['momentum_1m'] = 1
        elif all(last3[i] < last3[i-1] for i in range(1, 3)):
            result['momentum_1m'] = -1

    return result

# =============================================================
# SALUD DE ESTRATEGIA
# =============================================================

def analyze_strategy_health():
    if len(trade_history) < 5:
        return 'ok', 'Pocos datos'
    rec = list(trade_history)[-10:]
    tp  = sum(1 for t in rec if t == 'tp')
    sl  = sum(1 for t in rec if t == 'sl')
    tot = tp + sl
    eff = (tp / tot * 100) if tot > 0 else 50
    if   eff < 40 and tot >= 8: return 'broken',  f'En revision {round(eff)}%'
    elif eff < 55 and tot >= 5: return 'warning', f'Racha neg {round(eff)}%'
    elif eff >= 80:              return 'strong',  f'Solida {round(eff)}%'
    else:                        return 'ok',      f'Normal {round(eff)}%'

# =============================================================
# PROBABILIDAD — Confluencia MTF
# =============================================================

def calc_prob(p5m, p1m, direction, range_info, trend):
    prob    = 35
    px      = p5m[-1] if p5m else p1m[-1]
    rsi     = calc_rsi(p5m) if p5m else 50
    ac_now, ac_prev = calc_ac(p5m) if p5m and len(p5m) >= 40 else (0, 0)
    mom     = calc_mom(p5m) if p5m else 0
    size    = range_info['size'] or 1
    macro   = trend.get('macro', 'neutral')
    mom_1m  = trend.get('momentum_1m', 0)

    if direction == 'buy':
        if trend['dir'] == 'up':
            prob += 15
            if macro == 'up':              prob += 12
            if trend['strength'] >= 3:     prob += 8
        elif trend['dir'] == 'neutral':    prob += 2
        if trend['pullback'] and trend['dir'] == 'up': prob += 10
        if mom_1m == 1:                    prob += 8
        elif mom_1m == -1:                 prob -= 6
        if rsi < 25:  prob += 18
        elif rsi < 35: prob += 12
        elif rsi < 45: prob += 6
        elif rsi > 65: prob -= 10
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0:                    prob += 5
        elif ac_now < 0 and mom > 0:        prob += 3
    else:
        if trend['dir'] == 'down':
            prob += 15
            if macro == 'down':            prob += 12
            if trend['strength'] >= 3:     prob += 8
        elif trend['dir'] == 'neutral':    prob += 2
        if trend['pullback'] and trend['dir'] == 'down': prob += 10
        if mom_1m == -1:                   prob += 8
        elif mom_1m == 1:                  prob -= 6
        if rsi > 75:  prob += 18
        elif rsi > 65: prob += 12
        elif rsi > 55: prob += 6
        elif rsi < 35: prob -= 10
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0:                    prob += 5
        elif ac_now > 0 and mom < 0:        prob += 3

    if range_info['is_range']:
        prob += 5
        if direction == 'buy':
            d = (px - range_info['low']) / size
            if d < 0.15: prob += 10
            elif d < 0.30: prob += 5
        else:
            d = (range_info['high'] - px) / size
            if d < 0.15: prob += 10
            elif d < 0.30: prob += 5

    ema21 = trend.get('ema21_5m', 0)
    if ema21 > 0:
        dist = abs(px - ema21) / px * 100
        if dist < 0.1: prob += 7
        elif dist < 0.3: prob += 3

    return min(max(round(prob), 35), 97)

# =============================================================
# MODOS ESPECIALES — Profit Lock / Survival / Objetivo Diario
# =============================================================

def check_daily_modes():
    global profit_lock, survival_mode, day_target_hit, current_threshold

    # Profit Lock
    if not profit_lock and daily_tp_count >= PROFIT_LOCK_TP:
        profit_lock = True
        current_threshold = max(current_threshold, PROFIT_LOCK_THR)
        save_state()
        send_alert(
            f'🔒 <b>PROFIT LOCK</b>\n'
            f'{daily_tp_count} TPs hoy\n'
            f'Umbral → {PROFIT_LOCK_THR}%\n{now_str()}'
        )

    # Survival Mode
    if not survival_mode and daily_pnl_eur >= DAILY_TARGET_1:
        survival_mode = True
        current_threshold = max(current_threshold, SURVIVAL_THR)
        save_state()
        send_alert(
            f'🛡 <b>SURVIVAL MODE</b>\n'
            f'Objetivo +{DAILY_TARGET_1}€ alcanzado!\n'
            f'Modo conservador activo\n'
            f'Max {MAX_PER_HOUR_SRV} señales/hora\n'
            f'Umbral → {SURVIVAL_THR}%\n{now_str()}'
        )

    # Objetivo 2 — apagar el dia
    if not day_target_hit and daily_pnl_eur >= DAILY_TARGET_2:
        day_target_hit = True
        save_state()
        send_alert(
            f'🏆 <b>OBJETIVO DIARIO COMPLETO</b>\n'
            f'+{round(daily_pnl_eur)}€ conseguidos hoy!\n'
            f'Bot pausado hasta mañana\n'
            f'Descansa, lo logramos 💪\n{now_str()}'
        )

# =============================================================
# SESSION KILL SWITCH
# =============================================================

def check_session_kill(session_key):
    if session_sl_count.get(session_key, 0) >= SESSION_KILL_SL:
        if not session_kill.get(session_key):
            session_kill[session_key] = True
            name = 'Londres' if session_key == 'london' else 'New York'
            save_state()
            send_alert(
                f'🚫 <b>SESSION KILL: {name}</b>\n'
                f'{SESSION_KILL_SL} SLs en esta sesion\n'
                f'Bloqueada hasta mañana\n{now_str()}'
            )

# =============================================================
# TREND RIDER — Reentradas inteligentes
# =============================================================

def can_trend_ride(asset_key, direction, trend, market_state):
    """
    True si se puede hacer reentrada en Trend Rider.
    Solo en TRENDING, max 3 reentradas, con cooldown.
    """
    if market_state != 'TRENDING':
        return False
    if trend_rider[asset_key] >= TREND_RIDER_MAX:
        return False
    if time.time() - trend_rider_time[asset_key] < TREND_RIDER_CD:
        return False
    # Tendencia sigue alineada MTF
    if direction == 'buy'  and trend['dir'] == 'up'   and trend['macro'] == 'up':
        return True
    if direction == 'sell' and trend['dir'] == 'down' and trend['macro'] == 'down':
        return True
    return False

# =============================================================
# MEMORIA ADAPTATIVA
# =============================================================

def update_memory(asset_key, direction, result):
    h   = str(datetime.now().hour)
    ses = current_session_key() or 'other'
    key = 'tp' if result == 'tp' else 'sl'

    if h not in memory['best_hour']:
        memory['best_hour'][h] = {'tp': 0, 'sl': 0}
    memory['best_hour'][h][key] += 1

    if ses not in memory['best_session']:
        memory['best_session'][ses] = {'tp': 0, 'sl': 0}
    memory['best_session'][ses][key] += 1

    memory['best_asset'][asset_key][key] += 1
    memory['best_dir'][direction] = memory['best_dir'].get(direction, 0) + (1 if key == 'tp' else -1)
    save_memory()

def memory_insight():
    """Genera texto con aprendizaje del bot para el resumen diario."""
    lines = []
    # Mejor hora
    best_h = max(memory['best_hour'].items(),
                 key=lambda x: x[1]['tp'] / max(x[1]['tp']+x[1]['sl'], 1),
                 default=(None, None))
    if best_h[0]:
        tot = best_h[1]['tp'] + best_h[1]['sl']
        if tot >= 3:
            pct = round(best_h[1]['tp'] / tot * 100)
            lines.append(f'⏰ Mejor hora: {best_h[0]}:00 ({pct}%)')
    # Mejor activo
    for ak, v in memory['best_asset'].items():
        tot = v['tp'] + v['sl']
        if tot >= 5:
            pct = round(v['tp'] / tot * 100)
            cfg = ASSETS[ak]
            lines.append(f'{cfg["icon"]} {cfg["name"]}: {pct}% ({tot} ops)')
    return '\n'.join(lines) if lines else ''

# =============================================================
# GESTION BE / TRAILING
# =============================================================

def update_signal_management(sig, price):
    cfg    = ASSETS[sig['asset']]
    is_buy = sig['direction'] == 'buy'
    be     = cfg.get('be_trigger')
    if be and not sig.get('be_activated'):
        trig = (is_buy and price >= sig['entry'] + be) or \
               (not is_buy and price <= sig['entry'] - be)
        if trig:
            new_sl = sig['entry']
            if (is_buy and new_sl > sig['sl']) or (not is_buy and new_sl < sig['sl']):
                sig['sl'] = new_sl
                sig['be_activated'] = True
                log(f'  BE {cfg["icon"]} SL→entrada {sig["sl"]:.2f}')
    trail = cfg.get('trail_step')
    if trail:
        if is_buy:
            best = sig.get('best_price', sig['entry'])
            if price > best:
                sig['best_price'] = price
                steps = int((price - sig['entry']) / trail)
                if steps > 0:
                    new_sl = sig['entry'] + (steps-1) * trail
                    if new_sl > sig['sl']:
                        sig['sl'] = new_sl
                        log(f'  Trail ₿ SL→{round(sig["sl"]):,}')
        else:
            best = sig.get('best_price', sig['entry'])
            if price < best:
                sig['best_price'] = price
                steps = int((sig['entry'] - price) / trail)
                if steps > 0:
                    new_sl = sig['entry'] - (steps-1) * trail
                    if new_sl < sig['sl']:
                        sig['sl'] = new_sl
                        log(f'  Trail ₿ SL→{round(sig["sl"]):,}')

# =============================================================
# SEGUIMIENTO TP / SL
# =============================================================

def check_active_signals():
    global active_signals, consecutive_sl, current_threshold, paused_until
    global daily_sl_count, daily_stopped, daily_tp_count, daily_pnl_eur
    global trend_rider, trend_rider_time

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

        hit_tp = (sig['direction']=='buy'  and price >= sig['tp']) or \
                 (sig['direction']=='sell' and price <= sig['tp'])
        hit_sl = (sig['direction']=='buy'  and price <= sig['sl']) or \
                 (sig['direction']=='sell' and price >= sig['sl'])

        if hit_tp:
            sig['result']    = 'tp'
            stats[sig['asset']]['tp'] += 1
            trade_history.append('tp')
            consecutive_sl   = 0
            daily_tp_count  += 1
            gain             = est_profit(sig['asset'])
            daily_pnl_eur   += gain
            update_memory(sig['asset'], sig['direction'], 'tp')
            if current_threshold > BASE_THRESHOLD:
                current_threshold = max(current_threshold - 3, BASE_THRESHOLD)

            # Trend Rider: registrar para posible reentrada
            trend_rider[sig['asset']]      = sig.get('rider_count', 0)
            trend_rider_time[sig['asset']] = time.time()

            save_state()
            check_daily_modes()

            send_alert(
                f'✅ <b>TP {cfg["icon"]} +{cfg["tp"]} pips</b>\n'
                f'Entrada {fmt(sig["entry"])} → {fmt(price)}\n'
                f'~+{gain}€ | P&L dia: ~+{round(daily_pnl_eur)}€\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

        elif hit_sl:
            sig['result']    = 'sl'
            stats[sig['asset']]['sl'] += 1
            trade_history.append('sl')
            consecutive_sl  += 1
            daily_sl_count  += 1
            daily_pnl_eur   -= est_profit(sig['asset']) * 0.5  # SL = ~mitad del TP
            update_memory(sig['asset'], sig['direction'], 'sl')

            # Trend Rider reset
            trend_rider[sig['asset']] = 0

            # Session kill counter
            sk = current_session_key()
            if sk:
                session_sl_count[sk] = session_sl_count.get(sk, 0) + 1
                check_session_kill(sk)

            save_state()

            # Daily Stop
            if daily_sl_count >= DAILY_SL_LIMIT:
                daily_stopped = True
                save_state()
                send_alert(
                    f'🛑 <b>DAILY STOP</b>\n'
                    f'{DAILY_SL_LIMIT} SL diarios.\n'
                    f'Pausado hasta las 00:00\n{now_str()}'
                )
            elif consecutive_sl >= 3:
                paused_until      = time.time() + 1800
                current_threshold = min(current_threshold + 5, 92)
                consecutive_sl    = 0
                send_alert(
                    f'⏸ <b>PAUSA 30 MIN</b>\n'
                    f'3 SL consecutivos\n'
                    f'Umbral → {current_threshold}%\n{now_str()}'
                )
            else:
                health, _ = analyze_strategy_health()
                if health in ('warning', 'broken'):
                    current_threshold = min(current_threshold + 3, 92)

            send_alert(
                f'❌ <b>SL {cfg["icon"]} -{cfg["sl"]} pips</b>\n'
                f'Entrada {fmt(sig["entry"])} → {fmt(price)}\n'
                f'P&L dia: ~{round(daily_pnl_eur)}€\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

    for s in to_remove:
        for hs in hour_signals:
            if hs.get('id') == s.get('id'):
                hs['result'] = s.get('result', 'open')
        if s in active_signals:
            active_signals.remove(s)

# =============================================================
# RESUMEN HORARIO
# =============================================================

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
    pct    = round((tp_c / closed)*100) if closed > 0 else 0
    stars  = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
    hora   = f'{(now.hour-1)%24:02d}:00'
    health, hmsg = analyze_strategy_health()
    htxt   = f'\n⚠️ {hmsg}' if health=='broken' else \
             f'\n🔶 {hmsg}' if health=='warning' else \
             f'\n💚 {hmsg}' if health=='strong'  else ''
    mode   = ' 🛡SRV' if survival_mode else ' 🔒LOCK' if profit_lock else ''

    send_alert(
        f'<b>📊 {hora}–{now.hour:02d}:00{mode}</b>\n'
        f'✅ {tp_c}  ❌ {sl_c}  ⏳ {open_c}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'P&L dia: ~+{round(daily_pnl_eur)}€\n'
        f'Total: {total}{htxt}'
    )
    if closed >= 3:
        if pct < 50:
            current_threshold = min(current_threshold + 5, 92)
        elif pct >= 80 and current_threshold > BASE_THRESHOLD:
            current_threshold = max(current_threshold - 3, BASE_THRESHOLD)
    hour_signals   = []
    consecutive_sl = 0

# =============================================================
# RESUMEN DIARIO
# =============================================================

def check_daily_summary():
    global last_summary_date
    now   = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour != SUMMARY_HOUR or now.minute != 0 or last_summary_date == today:
        return
    last_summary_date = today
    tp_g = stats['gold']['tp']; sl_g = stats['gold']['sl']
    tp_b = stats['btc']['tp'];  sl_b = stats['btc']['sl']
    total_tp = tp_g + tp_b
    total_sl = sl_g + sl_b
    total    = total_tp + total_sl
    pct   = round((total_tp/total)*100) if total > 0 else 0
    stars = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
    health, hmsg = analyze_strategy_health()
    insight = memory_insight()
    ins_txt = f'\n\n🧠 Memoria:\n{insight}' if insight else ''

    send_alert(
        f'<b>📅 CIERRE {now.strftime("%d/%m")}</b>\n'
        f'✅ TP: {total_tp}  ❌ SL: {total_sl}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'P&L estimado: ~+{round(daily_pnl_eur)}€\n'
        f'Total: {total} operaciones\n'
        f'🥇 ORO  {tp_g}TP / {sl_g}SL\n'
        f'₿ BTC  {tp_b}TP / {sl_b}SL\n'
        f'Umbral: {current_threshold}% | {hmsg}'
        f'{ins_txt}'
    )
    for k in stats:
        stats[k]['tp'] = stats[k]['sl'] = 0

# =============================================================
# ANALISIS Y SEÑAL — nucleo v17
# =============================================================

def analyze_and_alert(asset_key, p1m, p5m, p15m, is_rider=False, rider_dir=None, rider_count=0):
    global current_threshold, paused_until, hour_signals
    global trend_rider, trend_rider_time

    cfg    = ASSETS[asset_key]
    is_btc = asset_key == 'btc'
    fmt    = (lambda v: f'{round(v):,}') if is_btc else (lambda v: f'{v:.2f}')

    # ── Guardas globales ──────────────────────
    if day_target_hit:
        log(f'  Objetivo dia completo — descansando')
        return
    if daily_stopped:
        log(f'  Daily stop activo')
        return
    if time.time() < paused_until:
        log(f'  Pausa {round((paused_until-time.time())/60)}min')
        return
    if not is_sniper_hour():
        log(f'  Fuera de horas Sniper')
        return

    # ── Session Kill Switch ────────────────────
    killed, killed_name = is_session_killed()
    if killed:
        log(f'  Sesion {killed_name} bloqueada')
        return

    # ── Solo 1 señal abierta por activo ───────
    if not is_rider:
        open_for_asset = [s for s in active_signals if s['asset'] == asset_key]
        if open_for_asset:
            log(f'  {cfg["icon"]} Ya hay señal abierta — esperando')
            return

    if not p5m or len(p5m) < 10:
        return

    px = p5m[-1]

    # ── Clasificador de mercado ────────────────
    market_state, atr = classify_market(p5m, cfg['atr_min'])
    if market_state in ('DEAD', 'VOLATILE'):
        log(f'  {cfg["icon"]} {market_state} — skip')
        return

    # ── Anti-manipulacion + Liquidez ──────────
    is_manip, ratio = detect_manipulation(p5m)
    if is_manip:
        log(f'  {cfg["icon"]} Manip x{ratio}')
        return
    if detect_liquidity_hunt(p5m, atr):
        log(f'  {cfg["icon"]} Barrida liquidez detectada — skip')
        return

    # ── Filtro de Noticias ─────────────────────
    news_block, news_title = is_news_window()
    if news_block:
        log(f'  📰 Bloqueado por noticia: {news_title}')
        return

    # ── Filtro de Volumen ──────────────────────
    if not volume_confirms(asset_key):
        log(f'  {cfg["icon"]} Volumen insuficiente — skip')
        return

    # ── Modos especiales ──────────────────────
    check_daily_modes()

    # ── Analisis ──────────────────────────────
    rsi        = calc_rsi(p5m)
    range_info = detect_range(p5m)
    trend      = detect_trend_mtf(p1m, p5m, p15m)
    ac_now, ac_prev = calc_ac(p5m) if len(p5m) >= 40 else (0, 0)
    macro      = trend['macro']
    mom_1m     = trend['momentum_1m']

    block_buy  = (macro == 'down' and trend['strength'] >= 3)
    block_sell = (macro == 'up'   and trend['strength'] >= 3)

    # ── Umbral efectivo ───────────────────────
    umbral_base = max(current_threshold, BASE_THRESHOLD)
    if not is_sniper_hour():
        umbral_base = OUT_SESSION_THR
    if market_state == 'RANGING':
        umbral_base = min(umbral_base + 3, 92)
    if survival_mode:
        umbral_base = max(umbral_base, SURVIVAL_THR)

    # ── Trend Rider: forzar direccion ─────────
    if is_rider and rider_dir:
        sig = rider_dir
    else:
        sig = None
        # TRENDING: continuacion
        if market_state == 'TRENDING':
            if not block_buy and trend['dir']=='up' and macro=='up':
                if rsi < 65 and trend['strength'] >= 2 and mom_1m >= 0: sig = 'buy'
            if not block_sell and trend['dir']=='down' and macro=='down':
                if rsi > 35 and trend['strength'] >= 2 and mom_1m <= 0: sig = 'sell'
            if not sig:
                if not block_buy  and trend['dir']=='up'   and trend['pullback'] and mom_1m >= 0: sig = 'buy'
                if not block_sell and trend['dir']=='down' and trend['pullback'] and mom_1m <= 0: sig = 'sell'
        # RANGING: extremos
        if market_state == 'RANGING' and range_info['is_range'] and range_info['size'] > 0:
            sz = range_info['size']
            if not block_buy  and (px - range_info['low'])  / sz < 0.20 and mom_1m >= 0: sig = 'buy'
            if not block_sell and (range_info['high'] - px) / sz < 0.20 and mom_1m <= 0: sig = 'sell'
        # Fallback
        if not sig:
            if not block_buy  and trend['dir']=='up'   and trend['strength'] >= 2 and rsi < 60: sig = 'buy'
            if not block_sell and trend['dir']=='down' and trend['strength'] >= 2 and rsi > 40: sig = 'sell'
        if not sig:
            if not block_buy  and rsi < 28 and mom_1m >= 0: sig = 'buy'
            if not block_sell and rsi > 72 and mom_1m <= 0: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} '
        f'Tend:{trend["dir"]}({trend["strength"]}) Macro:{macro} '
        f'{market_state} 1m:{mom_1m} -> {sig or "sin senal"}'
        f'{" [RIDER]" if is_rider else ""}')

    if not sig: return

    # ── Expected Value Filter ─────────────────
    prob = calc_prob(p5m, p1m, sig, range_info, trend)
    if not ev_is_positive(prob, asset_key):
        log(f'  EV negativo — skip')
        return

    if prob < umbral_base:
        log(f'  {prob}% < {umbral_base}% skip')
        return

    # ── Limite horario ────────────────────────
    hora_actual  = datetime.now().hour
    senales_hora = [s for s in hour_signals if s['hour'] == hora_actual]
    max_h = MAX_PER_HOUR_SRV if survival_mode else MAX_PER_HOUR
    if len(senales_hora) >= max_h:
        log(f'  Limite {max_h}/h')
        return

    # ── Cooldown ──────────────────────────────
    if not is_rider:
        sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
        if (last_signal[asset_key] == sig_key and
                time.time() - last_signal_time[asset_key] < COOLDOWN):
            log('  Cooldown')
            return
        last_signal[asset_key]      = sig_key
        last_signal_time[asset_key] = time.time()

    # ── Construir señal ───────────────────────
    isBuy  = sig == 'buy'
    tp     = px + cfg['tp']  if isBuy else px - cfg['tp']
    sl     = px - cfg['sl']  if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC↑'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC↓'
    else:                                  ac_txt = 'AC~'

    if trend['dir'] != 'neutral' and macro == trend['dir']:
        ctx = f"MTF {trend['dir'].upper()} ({trend['strength']}v)"
    elif trend['dir'] != 'neutral':
        ctx = f"Tend {trend['dir'].upper()} ({trend['strength']}v)"
    elif range_info['is_range']:
        ctx = 'Rango'
    else:
        ctx = 'RSI extremo'

    mkt_ico = '📈' if market_state=='TRENDING' else '↔️'

    if prob >= 90:   lote_rec, lote_ico = '0.10–0.15', '💪'
    elif prob >= 82: lote_rec, lote_ico = '0.05–0.10', '👍'
    else:            lote_rec, lote_ico = '0.05',      '👌'

    lots_txt = '\n'.join([
        f'  {lot:.2f} → +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01):.0f}€'
        for lot in LOTAJES
    ])

    if cfg.get('be_trigger') and not cfg.get('trail_step'):
        mgmt = f'BE: +{cfg["be_trigger"]}p → SL a entrada\n'
    elif cfg.get('trail_step'):
        mgmt = f'BE: +{cfg["be_trigger"]}p | Trail: +{cfg["trail_step"]}p\n'
    else:
        mgmt = ''

    rider_txt  = f' 🔄 RIDER #{rider_count+1}' if is_rider else ''
    lock_txt   = ' 🔒' if profit_lock else ''
    srv_txt    = ' 🛡' if survival_mode else ''
    tipo       = 'BUY 🟢' if isBuy else 'SELL 🔴'
    sig_id     = f'{asset_key}-{int(time.time())}'
    num_h      = len(senales_hora) + 1

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]}  {prob}%{lock_txt}{srv_txt}{rider_txt}</b>\n'
        f'RSI {rsi} | {ac_txt} | {ctx}\n'
        f'{mkt_ico} {market_state} | {session_name()}\n'
        f'Entrada: <b>{fmt(px)}</b>\n'
        f'TP: {fmt(tp)}  (+{cfg["tp"]}p)\n'
        f'SL: {fmt(sl)}  (-{cfg["sl"]}p)\n'
        f'{mgmt}'
        f'──────────────\n'
        f'{lote_ico} Lote: {lote_rec}\n'
        f'{lots_txt}\n'
        f'──────────────\n'
        f'#{num_h} | {now_str()}'
    )
    log(f'SEÑAL {tipo} {fmt(px)} {prob}% | {ctx} | {market_state}{rider_txt}')

    new_rc = rider_count + 1 if is_rider else 0
    entry  = {
        'id': sig_id, 'asset': asset_key, 'direction': sig,
        'entry': px, 'tp': tp, 'sl': sl,
        'time': time.time(), 'result': 'open',
        'prob': prob, 'hour': hora_actual,
        'be_activated': False, 'best_price': px,
        'rider_count': new_rc,
        'market_state': market_state,
        'trend_dir': trend['dir'], 'macro': macro,
    }
    active_signals.append(entry)
    hour_signals.append(entry)

    # ── Registrar Trend Rider ──────────────────
    if is_rider:
        trend_rider[asset_key]      = new_rc
        trend_rider_time[asset_key] = time.time()

# =============================================================
# MAIN LOOP
# =============================================================

def main():
    load_state()
    load_memory()
    log('SNIPER BOT v17.0 INICIANDO')
    send_alert(
        '<b>🚀 SNIPER BOT v17.0 ACTIVO</b>\n'
        '📡 Binance FAPI (= MT5)\n'
        '📊 MTF 15m+5m+1m | Elder Triple Pantalla\n'
        '🔄 Trend Rider | 🎯 Objetivo Diario\n'
        '📰 Filtro Noticias | 📈 Filtro Volumen\n'
        '🧨 Detector Liquidez | 🎲 EV Filter\n'
        '🧠 Memoria Adaptativa | 🚫 Session Kill\n'
        '1 señal/activo | BE Auto | Trail BTC\n'
        f'Meta: +{DAILY_TARGET_1}€ → Survival | +{DAILY_TARGET_2}€ → Off\n'
        f'Umbral sesion: {BASE_THRESHOLD}% | Fuera: {OUT_SESSION_THR}%'
    )

    while True:
        try:
            check_new_day()
            check_hourly_summary()
            check_daily_summary()
            check_active_signals()

            # ── Analisis ORO ──────────────────
            p1m_g, p5m_g, p15m_g = get_prices('gold')
            if p1m_g:
                analyze_and_alert('gold', p1m_g, p5m_g, p15m_g)
                # Trend Rider ORO
                last_g = next((s for s in reversed(active_signals)
                               if s['asset']=='gold' and s['result']=='open'), None)
                # El rider se activa tras un TP (en check_active_signals),
                # aqui comprobamos si hay condicion para nueva entrada
                if (trend_rider['gold'] < TREND_RIDER_MAX and
                        time.time() - trend_rider_time['gold'] < 30 and
                        trend_rider_time['gold'] > 0):
                    # TP reciente — intentar reentrada
                    rc = trend_rider['gold']
                    analyze_and_alert('gold', p1m_g, p5m_g, p15m_g,
                                      is_rider=True, rider_dir=None, rider_count=rc)

            time.sleep(3)

            # ── Analisis BTC ──────────────────
            p1m_b, p5m_b, p15m_b = get_prices('btc')
            if p1m_b:
                analyze_and_alert('btc', p1m_b, p5m_b, p15m_b)
                if (trend_rider['btc'] < TREND_RIDER_MAX and
                        time.time() - trend_rider_time['btc'] < 30 and
                        trend_rider_time['btc'] > 0):
                    rc = trend_rider['btc']
                    analyze_and_alert('btc', p1m_b, p5m_b, p15m_b,
                                      is_rider=True, rider_dir=None, rider_count=rc)

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('SNIPER BOT v17.0 Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
