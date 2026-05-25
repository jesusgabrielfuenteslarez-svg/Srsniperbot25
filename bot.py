import time
import requests
from datetime import datetime
from collections import deque

# =============================================
# SNIPER BOT v13.0 — INTELIGENCIA REAL
# Basado en: Trading in the Zone + Elder
# =============================================
# ANTES DE SUBIR:
# Registrate en twelvedata.com (gratis)
# y pega tu API Key abajo
# =============================================

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

TWELVE_API_KEY = 'TU_API_KEY_AQUI'

LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10,  'val_pto': 1.0},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200, 'val_pto': 0.01},
}

# ── Umbrales base ─────────────────────────────
BASE_THRESHOLD   = 75   # umbral base — confluencia filtra calidad
STRONG_THRESHOLD = 85   # señal fuerte
OUT_SESSION_THR  = 92   # fuera de sesion Londres/NY
MAX_PER_HOUR     = 10
MIN_STRONG       = 5

# ── Sesiones UTC ──────────────────────────────
SESSIONS = [(7, 16), (12, 21)]

# ── Estado adaptativo ─────────────────────────
current_threshold = BASE_THRESHOLD
consecutive_sl    = 0
paused_until      = 0

# ── Memoria de mercado (detectar cambios) ─────
# Guardamos ultimas 50 operaciones para distinguir
# mala racha vs estrategia rota
trade_history     = deque(maxlen=50)
volatility_memory = deque(maxlen=20)  # volatilidad reciente

# ── Señales activas ───────────────────────────
active_signals   = []
last_signal      = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0,    'btc': 0}
COOLDOWN         = 300
MAX_SIGNAL_AGE   = 7200

# ── Stats ─────────────────────────────────────
hour_signals      = []
last_hour_summary = -1
stats = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
last_summary_date = None
SUMMARY_HOUR      = 23

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
# PRECIOS — Twelve Data (= MT4/MT5)
# =============================================

def get_twelve_prices(symbol, outputsize=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': '5min',
            'outputsize': outputsize, 'apikey': TWELVE_API_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            log(f'Twelve error [{symbol}]: {data.get("message")}')
            return None
        values = data.get('values', [])
        return [float(v['close']) for v in reversed(values)] if values else None
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

def binance_klines(sym):
    r = requests.get(
        f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=60',
        timeout=10)
    prices = [float(k[4]) for k in r.json()]
    t = requests.get(
        f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}',
        timeout=5).json()
    if 'bidPrice' in t:
        prices[-1] = (float(t['bidPrice']) + float(t['askPrice'])) / 2
    return prices

def get_gold_prices():
    prices = get_twelve_prices('XAU/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 2000:
        spot = get_twelve_spot('XAU/USD')
        if spot and spot > 2000: prices[-1] = spot
        return prices
    try:
        p = binance_klines('XAUUSDT')
        if p and p[-1] > 2000: return p
    except: pass
    return None

def get_btc_prices():
    prices = get_twelve_prices('BTC/USD', 60)
    if prices and len(prices) >= 15 and prices[-1] > 10000:
        spot = get_twelve_spot('BTC/USD')
        if spot and spot > 10000: prices[-1] = spot
        return prices
    try:
        p = binance_klines('BTCUSDT')
        if p and p[-1] > 10000: return p
    except: pass
    return None

def get_spot_price(asset_key):
    symbol = 'XAU/USD' if asset_key == 'gold' else 'BTC/USD'
    price = get_twelve_spot(symbol)
    if price: return price
    try:
        sym = 'XAUUSDT' if asset_key == 'gold' else 'BTCUSDT'
        t = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}',
            timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except: pass
    return None

# =============================================
# INDICADORES TECNICOS
# =============================================

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
    return prices[-1] - prices[-1-n] if len(prices) >= n+1 else 0

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow+sig+2: return 0, 0
    def sma(d, n): return sum(d[-n:])/n if len(d) >= n else 0
    ao = [sma(prices[:i], fast) - sma(prices[:i], slow)
          for i in range(slow, len(prices)+1)]
    if len(ao) < sig+1: return 0, 0
    return round(ao[-1]-sma(ao,sig),4), round(ao[-2]-sma(ao[:-1],sig),4)

def calc_volatility(prices, n=10):
    """Volatilidad reciente = rango promedio de las ultimas n velas"""
    if len(prices) < n+1: return 0
    ranges = [abs(prices[i] - prices[i-1]) for i in range(-n, 0)]
    return sum(ranges) / n

def detect_trend(prices):
    """
    Elder Triple Screen simplificado:
    Tendencia = direccion de EMA21
    Impulso   = pendiente de las ultimas 5 velas
    Estructura = precio sobre/bajo MA50
    """
    if len(prices) < 50:
        return {'dir': 'neutral', 'strength': 0, 'pullback': False}

    ema21     = calc_ema(prices, 21)
    ma50      = calc_ma(prices, 50)
    px        = prices[-1]
    ema21_prev = calc_ema(prices[:-1], 21)

    # Direccion de la tendencia
    if ema21 > ema21_prev and px > ma50:
        trend_dir = 'up'
    elif ema21 < ema21_prev and px < ma50:
        trend_dir = 'down'
    else:
        trend_dir = 'neutral'

    # Fuerza: cuantas velas consecutivas en la misma direccion
    strength = 0
    for i in range(-1, -8, -1):
        if abs(i) >= len(prices): break
        if trend_dir == 'up'   and prices[i] > prices[i-1]: strength += 1
        elif trend_dir == 'down' and prices[i] < prices[i-1]: strength += 1
        else: break

    # Pullback: precio retrocede hacia EMA pero tendencia sigue
    pullback = False
    if trend_dir == 'up'   and px < ema21 and px > ma50: pullback = True
    if trend_dir == 'down' and px > ema21 and px < ma50: pullback = True

    return {'dir': trend_dir, 'strength': strength, 'pullback': pullback,
            'ema21': ema21, 'ma50': ma50}

def detect_manipulation(prices):
    """
    Detecta movimientos bruscos sospechosos (manipulacion, spike, evento inesperado).
    Si una vela mueve mas de 3x la volatilidad promedio = alerta.
    """
    if len(prices) < 15: return False, 0
    vol = calc_volatility(prices, 10)
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
        'is_range': abs(calc_ma(prices,5)-calc_ma(prices,20)) < size*0.3 and size > 0,
        'high': high, 'low': low, 'size': size
    }

# =============================================
# ANALISIS DE MERCADO — ESTRATEGIA ROTA VS MALA RACHA
# Elder + Douglas: cada operacion es independiente,
# pero el patron de resultados revela la salud de la estrategia
# =============================================

def analyze_strategy_health():
    """
    Distingue entre mala racha (normal) y estrategia rota (ajustar).
    - Mala racha: hasta 3-4 SL seguidos puede pasar con cualquier estrategia buena
    - Estrategia rota: efectividad cae por debajo del 50% en 10+ operaciones
    - Mercado cambiado: volatilidad aumenta/baja drasticamente
    """
    global current_threshold

    if len(trade_history) < 5:
        return 'ok', 'Pocos datos aun'

    recientes = list(trade_history)[-10:]
    tp_rec = sum(1 for t in recientes if t == 'tp')
    sl_rec = sum(1 for t in recientes if t == 'sl')
    total_rec = tp_rec + sl_rec
    eff_rec = (tp_rec / total_rec * 100) if total_rec > 0 else 50

    # Todos los resultados
    tp_all = sum(1 for t in trade_history if t == 'tp')
    sl_all = sum(1 for t in trade_history if t == 'sl')
    total_all = tp_all + sl_all
    eff_all = (tp_all / total_all * 100) if total_all > 0 else 50

    if eff_rec < 40 and total_rec >= 8:
        return 'broken', f'Estrategia en revision: {round(eff_rec)}% ultimas {total_rec} ops'
    elif eff_rec < 55 and total_rec >= 5:
        return 'warning', f'Racha negativa: {round(eff_rec)}% — ajustando filtros'
    elif eff_rec >= 80:
        return 'strong', f'Estrategia solida: {round(eff_rec)}%'
    else:
        return 'ok', f'Normal: {round(eff_rec)}% ultimas {total_rec} ops'

# =============================================
# CALCULO DE PROBABILIDAD — CONFLUENCIA REAL
# No solo RSI — suma de factores independientes
# como ensenado por Douglas: cada factor es una
# pieza de evidencia, no una certeza
# =============================================

def calc_prob(prices, direction, range_info, trend):
    """
    Probabilidad por confluencia de factores independientes:
    - Tendencia (Elder): direccion y fuerza
    - RSI (sobreventa/sobrecompra)
    - AC SMO (momentum)
    - Posicion en rango
    - Pullback hacia media (zona de valor)
    - Volatilidad normal (no manipulacion)
    """
    prob = 35  # base
    px   = prices[-1]
    rsi  = calc_rsi(prices)
    ac_now, ac_prev = calc_ac(prices)
    mom  = calc_mom(prices)
    size = range_info['size'] or 1

    # ── Factor 1: Tendencia alineada (Elder) ──
    if direction == 'buy':
        if trend['dir'] == 'up':
            prob += 15
            if trend['strength'] >= 3: prob += 8  # tendencia fuerte
        elif trend['dir'] == 'neutral':
            prob += 5
        # Pullback en tendencia = zona de valor = alta probabilidad
        if trend['pullback'] and trend['dir'] == 'up':
            prob += 12

    else:  # sell
        if trend['dir'] == 'down':
            prob += 15
            if trend['strength'] >= 3: prob += 8
        elif trend['dir'] == 'neutral':
            prob += 5
        if trend['pullback'] and trend['dir'] == 'down':
            prob += 12

    # ── Factor 2: RSI ──────────────────────────
    if direction == 'buy':
        if rsi < 25:   prob += 18  # sobreventa extrema
        elif rsi < 35: prob += 12
        elif rsi < 45: prob += 6
        elif rsi > 60: prob -= 8   # contra tendencia
    else:
        if rsi > 75:   prob += 18
        elif rsi > 65: prob += 12
        elif rsi > 55: prob += 6
        elif rsi < 40: prob -= 8

    # ── Factor 3: AC SMO (momentum Elder) ──────
    if direction == 'buy':
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0:                    prob += 5
        elif ac_now < 0 and mom > 0:        prob += 3  # divergencia positiva
    else:
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0:                    prob += 5
        elif ac_now > 0 and mom < 0:        prob += 3

    # ── Factor 4: Posicion en rango ────────────
    if range_info['is_range']:
        prob += 8
        if direction == 'buy':
            dist = (px - range_info['low']) / size
            if dist < 0.15: prob += 15  # muy cerca del soporte
            elif dist < 0.30: prob += 8
        else:
            dist = (range_info['high'] - px) / size
            if dist < 0.15: prob += 15
            elif dist < 0.30: prob += 8

    # ── Factor 5: Precio cerca de media (zona de valor) ──
    if 'ema21' in trend:
        dist_ema = abs(px - trend['ema21']) / px * 100
        if dist_ema < 0.1: prob += 8   # muy cerca de EMA21
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

    if   pct == 100: stars = '🌟🌟🌟'
    elif pct >= 80:  stars = '🌟🌟'
    elif pct >= 60:  stars = '🌟'
    else:            stars = ''

    hora_pasada = f'{(now.hour - 1) % 24:02d}:00'

    # Diagnostico de estrategia
    health, health_msg = analyze_strategy_health()
    health_txt = ''
    if health == 'broken':
        health_txt = f'\n⚠️ {health_msg}'
    elif health == 'warning':
        health_txt = f'\n🔶 {health_msg}'
    elif health == 'strong':
        health_txt = f'\n💚 {health_msg}'

    send_alert(
        f'<b>RESUMEN {hora_pasada} - {now.hour:02d}:00</b>\n'
        f'TP: {tp_c}  SL: {sl_c}  Curso: {open_c}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'Total: {total} senales'
        f'{health_txt}'
    )

    # Ajuste adaptativo
    if closed >= 3:
        if pct < 60:
            current_threshold = min(current_threshold + 5, 92)
            log(f'Efectividad {pct}% — umbral sube a {current_threshold}%')
        elif pct >= 80 and current_threshold > BASE_THRESHOLD:
            current_threshold = max(current_threshold - 3, BASE_THRESHOLD)
            log(f'Efectividad {pct}% — umbral baja a {current_threshold}%')

    hour_signals = []
    consecutive_sl = 0

# =============================================
# RESUMEN DIARIO
# =============================================

def check_daily_summary():
    global last_summary_date
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour == SUMMARY_HOUR and now.minute == 0 and last_summary_date != today:
        last_summary_date = today
        tp_g = stats['gold']['tp']; sl_g = stats['gold']['sl']
        tp_b = stats['btc']['tp'];  sl_b = stats['btc']['sl']
        total_tp = tp_g + tp_b
        total_sl = sl_g + sl_b
        total    = total_tp + total_sl
        pct = round((total_tp / total) * 100) if total > 0 else 0
        if   pct == 100: stars = '🌟🌟🌟'
        elif pct >= 80:  stars = '🌟🌟'
        elif pct >= 60:  stars = '🌟'
        else:            stars = ''
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
# SEGUIMIENTO TP / SL
# =============================================

def check_active_signals():
    global active_signals, consecutive_sl, current_threshold, paused_until

    to_remove = []
    now = time.time()

    for sig in active_signals:
        cfg = ASSETS[sig['asset']]
        is_btc = sig['asset'] == 'btc'
        fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'
        age_min = round((now - sig['time']) / 60)

        if now - sig['time'] > MAX_SIGNAL_AGE:
            sig['result'] = 'open'
            to_remove.append(sig)
            continue

        price = get_spot_price(sig['asset'])
        if not price: continue

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
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)

        elif hit_sl:
            sig['result'] = 'sl'
            stats[sig['asset']]['sl'] += 1
            trade_history.append('sl')
            consecutive_sl += 1

            # 3 SL seguidos = pausa + ajuste
            if consecutive_sl >= 3:
                paused_until = time.time() + 1800
                current_threshold = min(current_threshold + 5, 92)
                send_alert(
                    f'<b>PAUSA 30 MIN</b>\n'
                    f'3 SL consecutivos\n'
                    f'Revisando condiciones de mercado\n'
                    f'Nuevo umbral: {current_threshold}%\n'
                    f'{now_str()}'
                )
                consecutive_sl = 0
            else:
                # Ajuste suave si efectividad reciente baja
                health, _ = analyze_strategy_health()
                if health in ('warning', 'broken'):
                    current_threshold = min(current_threshold + 3, 92)

            send_alert(
                f'<b>SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
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
# ANALISIS Y ENVIO DE SEÑAL
# =============================================

def analyze_and_alert(asset_key, prices):
    global current_threshold, paused_until, hour_signals

    cfg = ASSETS[asset_key]
    px  = prices[-1]
    is_btc = asset_key == 'btc'
    fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'

    # ── Pausa activa ──────────────────────────
    if time.time() < paused_until:
        mins = round((paused_until - time.time()) / 60)
        log(f'  Pausa activa — {mins} min')
        return

    # ── Detectar manipulacion / evento brusco ─
    is_manip, manip_ratio = detect_manipulation(prices)
    if is_manip:
        log(f'  {cfg["icon"]} Movimiento brusco x{manip_ratio} — esperando estabilizacion')
        return  # no operar en picos de manipulacion

    # ── Analisis tecnico completo ──────────────
    rsi        = calc_rsi(prices)
    range_info = detect_range(prices)
    trend      = detect_trend(prices)
    ac_now, ac_prev = calc_ac(prices)

    # ── Detectar direccion por confluencia ─────
    # No solo RSI extremo — tambien tendencia + pullback
    sig = None

    # Prioridad 1: Pullback en tendencia (Elder — zona de valor)
    if trend['dir'] == 'up'   and trend['pullback']: sig = 'buy'
    if trend['dir'] == 'down' and trend['pullback']: sig = 'sell'

    # Prioridad 2: Tendencia con momentum
    if not sig:
        if trend['dir'] == 'up'   and trend['strength'] >= 2 and rsi < 55: sig = 'buy'
        if trend['dir'] == 'down' and trend['strength'] >= 2 and rsi > 45: sig = 'sell'

    # Prioridad 3: RSI extremo (clasico)
    if not sig:
        if rsi < 30: sig = 'buy'
        elif rsi > 70: sig = 'sell'

    # Prioridad 4: Extremos de rango
    if not sig and range_info['is_range'] and range_info['size'] > 0:
        sz = range_info['size']
        if (px - range_info['low'])  / sz < 0.20: sig = 'buy'
        elif (range_info['high'] - px) / sz < 0.20: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} Tend:{trend["dir"]}({trend["strength"]}) -> {sig or "sin senal"}')
    if not sig: return

    # ── Calcular probabilidad por confluencia ──
    prob = calc_prob(prices, sig, range_info, trend)

    # ── Umbral segun sesion y salud de estrategia ──
    umbral = OUT_SESSION_THR if not is_market_open() else max(current_threshold, BASE_THRESHOLD)

    # Si estrategia en warning, subir umbral temporalmente
    health, _ = analyze_strategy_health()
    if health == 'broken':
        umbral = max(umbral, 90)
    elif health == 'warning':
        umbral = max(umbral, 82)

    if prob < umbral:
        log(f'  {prob}% < {umbral}% ({session_name()}), skip')
        return

    # ── Control por hora ──────────────────────
    hora_actual  = datetime.now().hour
    senales_hora = [s for s in hour_signals if s['hour'] == hora_actual]
    fuertes_hora = [s for s in senales_hora if s['prob'] >= STRONG_THRESHOLD]

    if len(senales_hora) >= MAX_PER_HOUR:
        log(f'  Limite {MAX_PER_HOUR}/hora')
        return

    if len(fuertes_hora) < MIN_STRONG and prob < STRONG_THRESHOLD:
        log(f'  Esperando senales fuertes ({len(fuertes_hora)}/{MIN_STRONG})')
        return

    # ── Cooldown por activo ────────────────────
    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if (last_signal[asset_key] == sig_key and
            time.time() - last_signal_time[asset_key] < COOLDOWN):
        log('  Cooldown')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC+'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC-'
    else:                                  ac_txt = 'AC~'

    # Contexto de la senal
    if trend['dir'] != 'neutral':
        ctx = f"Tend {trend['dir'].upper()} ({trend['strength']} velas)"
    elif range_info['is_range']:
        ctx = 'Rango'
    else:
        ctx = 'RSI extremo'

    # Lotaje recomendado
    if prob >= 90:
        lote_rec = '0.10 - 0.15'; lote_ico = '💪'
    elif prob >= 82:
        lote_rec = '0.05 - 0.10'; lote_ico = '👍'
    else:
        lote_rec = '0.01 - 0.05'; lote_ico = '👌'

    lots_txt = '\n'.join([
        f'{lot:.2f} +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01, 2):.2f}EUR'
        for lot in LOTAJES
    ])

    tipo   = 'BUY' if isBuy else 'SELL'
    sig_id = f'{asset_key}-{int(time.time())}'
    num_h  = len(senales_hora) + 1

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {prob}%</b>\n'
        f'RSI {rsi} | {ac_txt} | {ctx}\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} (+{cfg["tp"]})\n'
        f'SL: {fmt(sl)} (-{cfg["sl"]})\n'
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
    }
    active_signals.append(entry)
    hour_signals.append(entry)

# =============================================
# MAIN
# =============================================

def main():
    log('SNIPER BOT v13.0 INICIANDO')
    send_alert(
        '<b>SNIPER BOT v13.0 ACTIVO</b>\n'
        'ORO TP+20 SL-10 | BTC TP+500 SL-200\n'
        'Tendencia + RSI + AC + Confluencia\n'
        'Anti-manipulacion | Adaptativo\n'
        'Resumen/hora | Resumen 23:00'
    )

    while True:
        try:
            check_hourly_summary()
            check_daily_summary()
            check_active_signals()

            gold_p = get_gold_prices()
            if gold_p: analyze_and_alert('gold', gold_p)

            time.sleep(3)

            btc_p = get_btc_prices()
            if btc_p: analyze_and_alert('btc', btc_p)

            time.sleep(12)

        except KeyboardInterrupt:
            send_alert('SNIPER BOT Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
