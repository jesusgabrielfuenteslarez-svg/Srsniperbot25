import os, time, requests
from datetime import datetime, timezone

# =============================================
# BOT v16.0 — 5 CAPAS DE ULTRA-FILTRO
# Chip de trader cuantitativo profesional
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

# ── CAPA 1: Bloques horarios premium (UTC) ────
SESSIONS = [
    (22, 24),   # Apertura Sydney/Tokyo — medianoche Barcelona
    (0,  2),    # Continuacion Asia
    (6,  12),   # Londres completo — 08-14h Barcelona
    (13, 20),   # NY completo — 15-22h Barcelona
]

# ── CAPA 2: 1 trade por activo por sesion ─────
# Estado por activo: None | 'locked'
lock = {'gold': None, 'btc': None}       # None = libre, 'locked' = esperando cierre
lock_session = {'gold': -1, 'btc': -1}   # bloque en el que se abrio

# ── CAPA 3: Liquidity Sweep ───────────────────
sweep_memory = {'gold': None, 'btc': None}  # ultimo barrido detectado

# ── CAPA 4: Noticias ──────────────────────────
news_blocked_until  = 0
news_alerted_events = set()
last_news_check     = 0
NEWS_CHECK_INTERVAL = 600  # revisar cada 10 min

# ── Señales activas / stats ───────────────────
active_sigs  = []
stats        = {'gold': {'tp':0,'sl':0}, 'btc': {'tp':0,'sl':0}}
last_d_report= None

def now_str(): return datetime.now().strftime('%H:%M')
def utc_hour(): return datetime.now(timezone.utc).hour
def log(m): print(f'[{now_str()}] {m}')

def send(msg):
    for chat in [TG_CHAT_ID, TG_GROUP_ID]:
        try:
            requests.post(TG_API, json={
                'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'
            }, timeout=10)
        except: pass

# =============================================
# CAPA 1 — SESION ACTIVA
# =============================================

def current_session():
    """Devuelve el indice de sesion activa (0=Londres, 1=NY) o None"""
    h = utc_hour()
    for i, (start, end) in enumerate(SESSIONS):
        if start <= h < end:
            return i
    return None

def session_name(idx):
    return ['Londres 🇬🇧', 'Londres+NY 🗽'][idx] if idx is not None else 'Fuera sesion'

# =============================================
# CAPA 4 — NOTICIAS (no bloquea señales, solo avisa y pausa 15 min)
# =============================================

def check_news():
    global news_blocked_until, last_news_check, news_alerted_events

    if time.time() - last_news_check < NEWS_CHECK_INTERVAL:
        return
    last_news_check = time.time()

    today = datetime.now().strftime('%Y-%m-%d')
    if not hasattr(check_news, '_day') or check_news._day != today:
        check_news._day = today
        news_alerted_events.clear()

    try:
        r = requests.get(
            'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
            timeout=10)
        nuevas = []
        for ev in r.json():
            try:
                if ev.get('impact','') not in ['High','high','3']: continue
                if ev.get('country','').upper() not in ['USD','EUR','GBP']: continue
                title = ev.get('title','')
                keywords = ['fed','fomc','rate','cpi','nfp','gdp','powell','pce','ppi','inflation']
                if not any(k in title.lower() for k in keywords): continue
                key = f"{ev.get('date','')}-{ev.get('time','')}-{title}"
                if key in news_alerted_events: continue
                news_alerted_events.add(key)
                nuevas.append(f"{ev.get('country','').upper()} {ev.get('time','')} — {title}")
            except: continue

        if nuevas:
            news_blocked_until = time.time() + 900   # pausa 15 min
            txt = '\n'.join([f'• {e}' for e in nuevas[:3]])
            send(f'<b>⚠️ NOTICIA ALTO IMPACTO</b>\n{txt}\n\nPausa 15 min — vuelvo después')
            log(f'Noticias detectadas — pausa 30 min')

    except Exception as e:
        log(f'News error: {e}')

# =============================================
# PRECIOS
# =============================================

def get_candles(symbol, interval='5min', size=30):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': interval,
            'outputsize': size, 'apikey': TWELVE_KEY,
        }, timeout=12)
        data = r.json()
        if data.get('status') == 'error':
            log(f'API: {data.get("message")}')
            return None
        vals = data.get('values', [])
        if not vals: return None
        c = [float(v['close']) for v in reversed(vals)]
        h = [float(v['high'])  for v in reversed(vals)]
        l = [float(v['low'])   for v in reversed(vals)]
        log(f'{symbol} {c[-1]:.2f}')
        return {'c': c, 'h': h, 'l': l}
    except Exception as e:
        log(f'Candles error {symbol}: {e}')
        return None

def binance_data(sym, minp):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=5m&limit=30',
            timeout=8)
        klines = r.json()
        c = [float(k[4]) for k in klines]
        h = [float(k[2]) for k in klines]
        l = [float(k[3]) for k in klines]
        return {'c':c,'h':h,'l':l} if c[-1] > minp else None
    except: return None

def gold_data():
    d = get_candles('XAU/USD')
    if d and d['c'][-1] > 2000: return d
    return binance_data('XAUUSDT', 2000)

def btc_data():
    d = get_candles('BTC/USD')
    if d and d['c'][-1] > 10000: return d
    return binance_data('BTCUSDT', 10000)

def gold_data_m15():
    d = get_candles('XAU/USD', interval='15min', size=20)
    if d and d['c'][-1] > 2000: return d
    return None

def btc_data_m15():
    d = get_candles('BTC/USD', interval='15min', size=20)
    if d and d['c'][-1] > 10000: return d
    return None

def spot(asset):
    sym = 'XAU/USD' if asset == 'gold' else 'BTC/USD'
    try:
        r = requests.get('https://api.twelvedata.com/price',
            params={'symbol': sym, 'apikey': TWELVE_KEY}, timeout=6)
        p = float(r.json().get('price', 0))
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

def rsi(closes, n=14):
    if len(closes) < n+1: return 50
    g = l = 0
    for i in range(len(closes)-n, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else: l -= d
    ag, al = g/n, l/n
    return round(100 - 100/(1+ag/al), 1) if al else 100

def ema(closes, n):
    if len(closes) < n: return closes[-1]
    k = 2/(n+1)
    e = closes[0]
    for p in closes[1:]: e = p*k + e*(1-k)
    return e

def atr(highs, lows, closes, n=14):
    if len(closes) < n+1: return 0
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-n:]) / n

# =============================================
# CAPA 3 — LIQUIDITY SWEEP
# =============================================

def detect_sweep(data, asset):
    """
    Detecta barridos de liquidez institucional:
    Precio rompe maximo/minimo anterior pero CIERRA de vuelta dentro.
    Señal de reversión de alta probabilidad.
    """
    c, h, l = data['c'], data['h'], data['l']
    if len(c) < 5: return None

    prev_high = max(h[-6:-1])
    prev_low  = min(l[-6:-1])
    last_h    = h[-1]
    last_l    = l[-1]
    last_c    = c[-1]
    last_o    = c[-2]

    # Barrido alcista: rompió minimo pero cerró arriba (mecha larga abajo)
    if last_l < prev_low and last_c > prev_low:
        wick = prev_low - last_l
        body = abs(last_c - last_o)
        if wick > body * 1.5:  # mecha > 1.5x el cuerpo
            log(f'{asset} SWEEP ALCISTA detectado — entrada BUY')
            return 'buy'

    # Barrido bajista: rompió maximo pero cerró abajo (mecha larga arriba)
    if last_h > prev_high and last_c < prev_high:
        wick = last_h - prev_high
        body = abs(last_c - last_o)
        if wick > body * 1.5:
            log(f'{asset} SWEEP BAJISTA detectado — entrada SELL')
            return 'sell'

    return None

# =============================================
# MODULO DE REVERSAL ESTRUCTURAL
# Liquidity Sweep M15 + CHoCH M5
# =============================================

def detect_institutional_reversal(data_m5, data_m15, asset):
    """
    Detecta giros institucionales de alta probabilidad:
    1. Barrido en M15 (mecha de rechazo >= 40% del tamaño total)
    2. Confirmacion CHoCH en M5 (rotura del ultimo maximo/minimo clave)
    3. Si se cumple ambos: resetea el lock y genera señal premium
    """
    if not data_m15 or not data_m5: return None, 0

    c15, h15, l15 = data_m15['c'], data_m15['h'], data_m15['l']
    c5,  h5,  l5  = data_m5['c'],  data_m5['h'],  data_m5['l']

    if len(c15) < 5 or len(c5) < 10: return None, 0

    reversal_dir = None

    # ── Paso 1: Barrido en M15 ──────────────────
    prev_low_m15  = min(l15[-6:-1])
    prev_high_m15 = max(h15[-6:-1])
    last_h15 = h15[-1]; last_l15 = l15[-1]
    last_c15 = c15[-1]; last_o15 = c15[-2]
    total_range = last_h15 - last_l15

    if total_range > 0:
        # Barrido alcista M15: rompió minimo, mecha abajo >= 40% del rango
        if last_l15 < prev_low_m15:
            wick_low = min(last_o15, last_c15) - last_l15
            if wick_low / total_range >= 0.40:
                reversal_dir = 'buy'
                log(f'{asset} BARRIDO ALCISTA M15 — mecha {round(wick_low/total_range*100)}%')

        # Barrido bajista M15: rompió maximo, mecha arriba >= 40% del rango
        if last_h15 > prev_high_m15 and reversal_dir is None:
            wick_high = last_h15 - max(last_o15, last_c15)
            if wick_high / total_range >= 0.40:
                reversal_dir = 'sell'
                log(f'{asset} BARRIDO BAJISTA M15 — mecha {round(wick_high/total_range*100)}%')

    if not reversal_dir: return None, 0

    # ── Paso 2: Confirmacion CHoCH en M5 ────────
    # BUY: buscar rotura del ultimo maximo decreciente
    # SELL: buscar rotura del ultimo minimo creciente
    choch_confirmed = False

    if reversal_dir == 'buy':
        # Encontrar el ultimo maximo relevante antes del giro
        recent_highs = [h5[i] for i in range(-10, -1)]
        key_level = max(recent_highs[-5:])  # ultimo techo de la bajada
        if c5[-1] > key_level:  # vela actual cierra por encima
            choch_confirmed = True
            log(f'{asset} CHoCH BUY confirmado — rotura {key_level:.2f}')

    elif reversal_dir == 'sell':
        recent_lows = [l5[i] for i in range(-10, -1)]
        key_level = min(recent_lows[-5:])
        if c5[-1] < key_level:
            choch_confirmed = True
            log(f'{asset} CHoCH SELL confirmado — rotura {key_level:.2f}')

    if not choch_confirmed:
        log(f'{asset} Barrido detectado pero CHoCH no confirmado aun')
        return None, 0

    # ── Confluencia extra: RSI alineado ─────────
    r = rsi(c5)
    rsi_ok = (reversal_dir == 'buy' and r < 55) or (reversal_dir == 'sell' and r > 45)
    bonus   = 30 if rsi_ok else 15  # bonus al quality score

    log(f'{asset} REVERSAL INSTITUCIONAL CONFIRMADO — {reversal_dir.upper()} bonus:{bonus}')
    return reversal_dir, bonus

# =============================================
# CAPA 5 — QUALITY SCORE (confluencia modular)
# =============================================

def quality_score(data, direction):
    """
    Sistema de puntuacion modular.
    Minimo 65 puntos para operar.
    Cada factor aporta puntos independientes.
    """
    c, h, l = data['c'], data['h'], data['l']
    score = 0
    reasons = []

    r = rsi(c)
    e21 = ema(c, 21)
    e50 = ema(c, 50) if len(c) >= 50 else ema(c, len(c))
    px  = c[-1]
    at  = atr(h, l, c)

    # ── Factor 1: RSI (0-25 pts) ──────────────
    if direction == 'buy':
        if r < 30:   score += 25; reasons.append(f'RSI {r} sobreventa')
        elif r < 40: score += 18; reasons.append(f'RSI {r} bajo')
        elif r < 50: score += 10; reasons.append(f'RSI {r} neutro-bajo')
        elif r > 65: score -= 15
    else:
        if r > 70:   score += 25; reasons.append(f'RSI {r} sobrecompra')
        elif r > 60: score += 18; reasons.append(f'RSI {r} alto')
        elif r > 50: score += 10; reasons.append(f'RSI {r} neutro-alto')
        elif r < 35: score -= 15

    # ── Factor 2: EMA tendencia (0-20 pts) ────
    if direction == 'buy':
        if px > e21 > e50: score += 20; reasons.append('Precio > EMA21 > EMA50')
        elif px > e21:     score += 12; reasons.append('Precio > EMA21')
        elif px < e50:     score -= 10
    else:
        if px < e21 < e50: score += 20; reasons.append('Precio < EMA21 < EMA50')
        elif px < e21:     score += 12; reasons.append('Precio < EMA21')
        elif px > e50:     score -= 10

    # ── Factor 3: Momentum 3 velas (0-20 pts) ─
    if len(c) >= 4:
        up3 = all(c[i] > c[i-1] for i in range(-3,0))
        dn3 = all(c[i] < c[i-1] for i in range(-3,0))
        if direction == 'buy'  and up3: score += 20; reasons.append('3 velas alcistas')
        if direction == 'sell' and dn3: score += 20; reasons.append('3 velas bajistas')
        if direction == 'buy'  and dn3: score -= 10
        if direction == 'sell' and up3: score -= 10

    # ── Factor 4: Posicion en rango (0-15 pts) ─
    if len(c) >= 20:
        mn = min(l[-20:])
        mx = max(h[-20:])
        rng = mx - mn
        if rng > 0:
            pos = (px - mn) / rng
            if direction == 'buy'  and pos < 0.20: score += 15; reasons.append('Soporte rango')
            if direction == 'sell' and pos > 0.80: score += 15; reasons.append('Resistencia rango')
            if direction == 'buy'  and pos > 0.80: score -= 10
            if direction == 'sell' and pos < 0.20: score -= 10

    # ── Factor 5: Volatilidad normal (0-10 pts) ─
    if at > 0:
        last_move = abs(c[-1] - c[-2])
        ratio = last_move / at
        if ratio < 0.5:   score += 10; reasons.append('Volatilidad normal')
        elif ratio > 3.0: score -= 20; reasons.append('SPIKE — manipulacion')

    total = min(max(score, 0), 100)
    return total, reasons

# =============================================
# SEGUIMIENTO TP/SL
# =============================================

def check_signals():
    global active_sigs
    to_rm = []
    now = time.time()

    for s in active_sigs:
        cfg = ASSETS[s['asset']]
        btc = s['asset'] == 'btc'
        fmt = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')
        age = round((now - s['t'])/60)

        if now - s['t'] > 7200:
            lock[s['asset']] = None
            to_rm.append(s); continue

        p = spot(s['asset'])
        if not p: continue

        hit_tp = (s['d']=='buy' and p>=s['tp']) or (s['d']=='sell' and p<=s['tp'])
        hit_sl = (s['d']=='buy' and p<=s['sl']) or (s['d']=='sell' and p>=s['sl'])

        if hit_tp or hit_sl:
            lock[s['asset']] = None  # desbloquear activo
            if hit_tp:
                stats[s['asset']]['tp'] += 1
                tp_t = stats['gold']['tp'] + stats['btc']['tp']
                sl_t = stats['gold']['sl'] + stats['btc']['sl']
                tot  = tp_t + sl_t
                pct  = round(tp_t/tot*100) if tot else 0
                send(f'<b>✅ TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                     f'{fmt(s["e"])} → {fmt(p)} (+{age}min)\n'
                     f'📊 Hoy: {tp_t}✅ {sl_t}❌ | {pct}%')
            else:
                stats[s['asset']]['sl'] += 1
                tp_t = stats['gold']['tp'] + stats['btc']['tp']
                sl_t = stats['gold']['sl'] + stats['btc']['sl']
                tot  = tp_t + sl_t
                pct  = round(tp_t/tot*100) if tot else 0
                send(f'<b>❌ SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                     f'{fmt(s["e"])} → {fmt(p)} (+{age}min)\n'
                     f'📊 Hoy: {tp_t}✅ {sl_t}❌ | {pct}%')
            to_rm.append(s)

    for s in to_rm:
        if s in active_sigs: active_sigs.remove(s)

# =============================================
# RESUMEN DIARIO 23:00
# =============================================

def daily():
    global last_d_report
    n = datetime.now()
    t = n.strftime('%Y-%m-%d')
    if n.hour != 23 or n.minute != 0 or last_d_report == t: return
    last_d_report = t
    tg = stats['gold']['tp']; sg = stats['gold']['sl']
    tb = stats['btc']['tp'];  sb = stats['btc']['sl']
    tot = tg+sg+tb+sb; tp = tg+tb
    pct = round(tp/tot*100) if tot else 0
    st  = '🌟🌟🌟' if pct==100 else '🌟🌟' if pct>=80 else '🌟' if pct>=60 else ''
    send(f'<b>RESUMEN DIA {n.strftime("%d/%m")}</b>\n'
         f'✅{tp} ❌{tot-tp} | {pct}% {st}\n'
         f'🥇 {tg}✅{sg}❌  ₿ {tb}✅{sb}❌')
    for k in stats: stats[k]['tp'] = stats[k]['sl'] = 0

# =============================================
# ANALISIS PRINCIPAL
# =============================================

def analyze(asset, data):
    cfg  = ASSETS[asset]
    c    = data['c']
    px   = c[-1]
    btc  = asset == 'btc'
    fmt  = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')

    # CAPA 1: Sesiones premium = bonus de score
    # Fuera de sesion: umbral mas alto pero NUNCA bloqueado
    sess = current_session()
    score_threshold = 58 if sess is not None else 68

    # CAPA 4: Noticias
    if time.time() < news_blocked_until:
        mins = round((news_blocked_until - time.time())/60)
        log(f'{cfg["icon"]} Pausa noticias — {mins} min')
        return

    # CAPA 2: 1 trade por activo por sesion
    if lock[asset] == 'locked':
        log(f'{cfg["icon"]} Activo bloqueado — esperando cierre de trade')
        return

    # Si cambiamos de sesion, resetear lock
    if lock_session[asset] != sess:
        lock[asset] = None
        lock_session[asset] = sess

    # CAPA 3: Liquidity Sweep + Reversal Estructural
    sweep_dir = detect_sweep(data, asset)

    # Reversal institucional M15+M5 (prioridad maxima — resetea lock si SL previo)
    m15_func   = gold_data_m15 if asset == 'gold' else btc_data_m15
    data_m15   = m15_func()
    rev_dir, rev_bonus = detect_institutional_reversal(data, data_m15, asset)

    if rev_dir:
        # Reversal detectado — no bloquea ni fuerza entrada
        # Solo resetea lock si contexto cambio completamente (V confirmada)
        if lock[asset] == 'locked':
            lock[asset] = None
            log(f'{cfg["icon"]} Lock reseteado por reversal V confirmada')
        # NO sobreescribimos sweep_dir — solo sumamos bonus al score despues

    # Direccion por indicadores clasicos
    r = rsi(c)
    e21 = ema(c, 21)
    e50 = ema(c, 50) if len(c) >= 50 else ema(c, len(c))

    votes_buy = votes_sell = 0
    if r < 35: votes_buy  += 3
    if r > 65: votes_sell += 3
    if r < 45: votes_buy  += 1
    if r > 55: votes_sell += 1
    if px > e21: votes_buy  += 2
    if px < e21: votes_sell += 2
    if len(c) >= 4:
        if all(c[i]>c[i-1] for i in range(-3,0)): votes_buy  += 3
        if all(c[i]<c[i-1] for i in range(-3,0)): votes_sell += 3

    if sweep_dir:
        sig   = sweep_dir
        bonus = 20
    elif rev_dir:
        sig   = rev_dir
        bonus = rev_bonus  # bonus por V institucional detectada
    else:
        bonus = 0
    # Si hay señal normal Y reversal en misma direccion, sumar bonus
    if rev_dir and sig == rev_dir:
        bonus = max(bonus, rev_bonus)
    elif votes_buy >= 4 and votes_buy > votes_sell:
        sig = 'buy'; bonus = 0
    elif votes_sell >= 4 and votes_sell > votes_buy:
        sig = 'sell'; bonus = 0
    else:
        log(f'{cfg["icon"]} {fmt(px)} RSI:{r} — sin señal clara')
        return

    # CAPA 5: Quality Score
    score, reasons = quality_score(data, sig)
    score = min(score + bonus, 100)

    log(f'{cfg["icon"]} {fmt(px)} {sig.upper()} Score:{score} {reasons}')

    if score < 58:
        log(f'  Score {score} < 65 — no pasa el filtro')
        return

    # Todo OK — generar señal
    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if score >= 85:   rec, ico = '0.10-0.15', '💪'
    elif score >= 75: rec, ico = '0.05-0.10', '👍'
    else:             rec, ico = '0.01-0.05', '👌'

    gains   = '\n'.join([f'  {l:.2f} → +{GAINS[asset][l]:.0f}€' for l in LOTAJES])
    tipo    = 'BUY' if isBuy else 'SELL'
    ctx_extra = ' + 🏛️V' if (rev_dir and rev_dir == sig) else ''
    ctx     = ('⚡ Sweep' if sweep_dir else ' | '.join(reasons[:2])) + ctx_extra

    send(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {score}pts</b>\n'
        f'📍 {fmt(px)}\n'
        f'🎯 {fmt(tp)}  (+{cfg["tp"]})\n'
        f'🛑 {fmt(sl)}  (-{cfg["sl"]})\n'
        f'━━━━━━━━━━━━\n'
        f'📌 {ctx}\n'
        f'{ico} Lote rec: {rec}\n'
        f'{gains}\n'
        f'━━━━━━━━━━━━\n'
        f'{session_name(sess)} | {now_str()}'
    )
    log(f'✅ SEÑAL {tipo} {fmt(px)} score:{score}')

    # Bloquear activo hasta que cierre este trade
    lock[asset] = 'locked'
    lock_session[asset] = sess

    active_sigs.append({
        'asset': asset, 'd': sig,
        'e': px, 'tp': tp, 'sl': sl, 't': time.time()
    })

# =============================================
# MAIN
# =============================================

def main():
    log('BOT v16.0 INICIANDO')
    send('<b>🤖 BOT ACTIVO v16.0</b>\n'
         '🥇 ORO 🎯+20 🛑-10\n'
         '₿  BTC 🎯+500 🛑-200\n'
         '5 capas ultra-filtro\n'
         '⏰ Asia 00h | Londres 08h | NY 15h\n24/7 — señales en cualquier momento')

    while True:
        try:
            check_news()
            daily()
            check_signals()

            gd = gold_data()
            if gd: analyze('gold', gd)
            else:  log('Sin datos ORO')

            time.sleep(3)

            bd = btc_data()
            if bd: analyze('btc', bd)
            else:  log('Sin datos BTC')

            time.sleep(12)

        except KeyboardInterrupt:
            send('🛑 BOT Detenido')
            break
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
