import os, time, requests, threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string

# =============================================
# SNIPER SCANNER v4.1
# Score mínimo: 80 puntos
# 5 filtros obligatorios — todos deben pasar
# Objetivo: >60% tasa de acierto
# =============================================

TG_TOKEN    = os.environ.get('TG_TOKEN',    '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ')
TG_CHAT_ID  = os.environ.get('TG_CHAT_ID',  '6467338067')
TG_GROUP_ID = os.environ.get('TG_GROUP_ID', '-5123266724')
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'
PORT        = int(os.environ.get('PORT', 5000))

ASSETS = {
    'gold': {
        'icon': '🥇', 'label': 'ORO',
        'tp': 10, 'sl': 5,
        'sym12': 'XAU/USD', 'symB': 'XAUUSDT',
        'min_px': 2000,
    },
    'btc': {
        'icon': '₿', 'label': 'BITCOIN',
        'tp': 250, 'sl': 125,
        'sym12': 'BTC/USD', 'symB': 'BTCUSDT',
        'min_px': 10000,
    },
}

LOTS     = [0.01, 0.02, 0.05, 0.10, 0.15, 0.50]
EUR_RATE = 0.92   # USD → EUR

# ── Umbrales de score ───────────────────────
SCORE_MIN       = 80   # mínimo para generar señal
SCORE_AFTER_SL3 = 90   # tras 3 SL consecutivos

# ── Cooldowns ───────────────────────────────
COOLDOWN = 0   # sin cooldown — escaneo continuo

# ── Máximo operaciones por activo por día ───
MAX_OPS_DAY = 5

# ── Estado ─────────────────────────────────
state = {
    'status':       'INICIANDO',
    'price':        {'gold': '—', 'btc': '—'},
    'last_scan':    {'gold': '—', 'btc': '—'},
    'last_signal':  None,
    'tp_count':     0,
    'sl_count':     0,
    'sig_count':    0,
    'logs':         [],
    'active_ops':   [],
    # por activo
    'last_alert':   {'gold': 0, 'btc': 0},
    'last_key':     {'gold': None, 'btc': None},
    'sl_streak':    {'gold': 0, 'btc': 0},   # SL consecutivos
    'ops_today':    {'gold': 0, 'btc': 0},   # ops del día
    'day_date':     None,                     # para reset diario
    'op_n':         0,
    # Aprendizaje por resultados
    'learn':  {
        'tendencia': {'tp':0,'sl':0},
        'liquidez':  {'tp':0,'sl':0},
        'confirmacion': {'tp':0,'sl':0},
        'volumen':   {'tp':0,'sl':0},
        'momentum':  {'tp':0,'sl':0},
    },
    'history': [],   # últimas 100 ops con filtros activos
}
lock = threading.Lock()

# =============================================
# UTILIDADES
# =============================================

def now_str():
    return datetime.now().strftime('%H:%M')

def log(msg):
    ts   = datetime.now().strftime('%H:%M:%S')
    line = f'{ts}  {msg}'
    print(line, flush=True)
    with lock:
        state['logs'].append(line)
        if len(state['logs']) > 80:
            state['logs'].pop(0)

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

def gain_eur(tp_pts, lot):
    return round(tp_pts * lot * 10 * EUR_RATE, 2)

def reset_daily():
    """Reset contadores al inicio de cada día."""
    today = datetime.now().strftime('%Y-%m-%d')
    with lock:
        if state['day_date'] != today:
            state['day_date']   = today
            state['ops_today']  = {'gold': 0, 'btc': 0}
            state['sl_streak']  = {'gold': 0, 'btc': 0}
            state['tp_count']   = 0
            state['sl_count']   = 0
            state['sig_count']  = 0
            log('📅 Reset diario')

# =============================================
# DATOS
# =============================================

def candles_twelve(sym, interval='5min', size=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': sym, 'interval': interval,
            'outputsize': size, 'apikey': TWELVE_KEY,
        }, timeout=12)
        d = r.json()
        if d.get('status') == 'error': return None
        vals = d.get('values', [])
        if len(vals) < 30: return None
        return {
            'c': [float(v['close'])  for v in reversed(vals)],
            'h': [float(v['high'])   for v in reversed(vals)],
            'l': [float(v['low'])    for v in reversed(vals)],
            'v': [float(v.get('volume', 0)) for v in reversed(vals)],
        }
    except Exception as e:
        log(f'Twelve {sym}: {e}')
        return None

def candles_binance(sym, interval='5m', size=60):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines'
            f'?symbol={sym}&interval={interval}&limit={size}',
            timeout=8)
        k = r.json()
        if not k or len(k) < 30: return None
        return {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        }
    except Exception as e:
        log(f'Binance {sym}: {e}')
        return None

def fetch(asset, tf='5min'):
    cfg = ASSETS[asset]
    d = candles_twelve(cfg['sym12'], tf)
    if d and d['c'][-1] > cfg['min_px']: return d
    iv = {'5min':'5m','15min':'15m','1h':'1h'}.get(tf,'5m')
    return candles_binance(cfg['symB'], iv)

def fetch_spot(asset):
    cfg = ASSETS[asset]
    try:
        p = float(requests.get('https://api.twelvedata.com/price',
            params={'symbol': cfg['sym12'], 'apikey': TWELVE_KEY},
            timeout=6).json().get('price', 0))
        if p > cfg['min_px']: return p
    except: pass
    try:
        t = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={cfg["symB"]}',
            timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice']) + float(t['askPrice'])) / 2
    except: pass
    return None

# =============================================
# INDICADORES
# =============================================

def calc_ema(c, n):
    if len(c) < n: return c[-1]
    k = 2/(n+1); e = c[0]
    for p in c[1:]: e = p*k+e*(1-k)
    return e

def calc_rsi(c, n=14):
    if len(c) < n+1: return 50
    g = l = 0
    for i in range(len(c)-n, len(c)):
        d = c[i]-c[i-1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g/n, l/n
    return round(100-100/(1+ag/al), 1) if al else 100

def calc_atr(h, l, c, n=14):
    if len(c) < n+1: return 1
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# 5 FILTROS OBLIGATORIOS
# Cada uno devuelve (puntos_buy, puntos_sell)
# Si un filtro falla devuelve (0, 0) → bloquea
# =============================================

def filtro_tendencia(c, h, l):
    """
    EMA20 > EMA50 + precio sobre EMA20 → BUY
    EMA20 < EMA50 + precio bajo EMA20  → SELL
    Sin tendencia clara → (0, 0)
    Peso: 20 puntos
    """
    e20 = calc_ema(c, 20)
    e50 = calc_ema(c, 50)
    px  = c[-1]
    gap = abs(e20 - e50) / e50 * 100   # % separación entre EMAs

    # Tendencia fuerte: EMAs bien separadas
    if e20 > e50 and px > e20:
        pts = 20 if gap > 0.05 else 10   # 20 si tendencia fuerte, 10 si débil
        return pts, 0
    if e20 < e50 and px < e20:
        pts = 20 if gap > 0.05 else 10
        return 0, pts

    return 0, 0   # sin tendencia → filtro bloqueante

def filtro_liquidez(c, h, l):
    """
    Barrido de máximos o mínimos recientes.
    El precio rompe un nivel y vuelve dentro.
    Peso: 25 puntos
    """
    if len(c) < 10: return 0, 0

    prev_h = max(h[-10:-1])
    prev_l = min(l[-10:-1])
    rng    = h[-1] - l[-1]

    if rng == 0: return 0, 0

    # Barrido de mínimos → potencial BUY
    if l[-1] < prev_l and c[-1] > prev_l:
        wick_pct = (min(c[-2], c[-1]) - l[-1]) / rng
        if wick_pct >= 0.30:
            pts = 25 if wick_pct >= 0.50 else 18
            return pts, 0

    # Barrido de máximos → potencial SELL
    if h[-1] > prev_h and c[-1] < prev_h:
        wick_pct = (h[-1] - max(c[-2], c[-1])) / rng
        if wick_pct >= 0.30:
            pts = 25 if wick_pct >= 0.50 else 18
            return 0, pts

    return 0, 0   # sin barrido → filtro bloqueante

def filtro_confirmacion(c, h, l):
    """
    Tras el barrido: vela de rechazo o CHoCH.
    Peso: 25 puntos
    """
    if len(c) < 6: return 0, 0

    body    = abs(c[-1] - c[-2])
    rng_vel = h[-1] - l[-1]
    if rng_vel == 0: return 0, 0

    body_pct = body / rng_vel   # % de cuerpo vs mecha

    # Vela alcista con cuerpo sólido (confirmación BUY)
    if c[-1] > c[-2] and body_pct >= 0.40:
        pts = 25 if body_pct >= 0.60 else 15
        # CHoCH adicional: cierra sobre máximo previo
        if c[-1] > max(c[-4:-2]):
            pts = min(pts + 5, 25)
        return pts, 0

    # Vela bajista con cuerpo sólido (confirmación SELL)
    if c[-1] < c[-2] and body_pct >= 0.40:
        pts = 25 if body_pct >= 0.60 else 15
        if c[-1] < min(c[-4:-2]):
            pts = min(pts + 5, 25)
        return 0, pts

    return 0, 0   # sin confirmación → filtro bloqueante

def filtro_volumen(v):
    """
    Volumen actual > promedio reciente.
    Peso: 15 puntos
    """
    if not v or len(v) < 10: return 8, 8   # sin datos → puntos parciales
    avg = sum(v[-11:-1]) / 10
    if avg == 0: return 8, 8
    ratio = v[-1] / avg
    if ratio >= 1.5:  return 15, 15   # volumen alto — confirma ambos lados
    if ratio >= 1.2:  return 10, 10
    if ratio >= 1.0:  return 5,  5
    return 0, 0   # volumen bajo → filtro bloqueante

def filtro_momentum(c, h, l):
    """
    Desplazamiento real del precio.
    Mercado lateral → no operar.
    Peso: 15 puntos
    """
    if len(c) < 10: return 0, 0
    at = calc_atr(h, l, c)
    if at == 0: return 0, 0

    # Rango de las últimas 5 velas vs ATR
    rng5 = max(h[-5:]) - min(l[-5:])
    ratio = rng5 / at

    if ratio < 0.5: return 0, 0   # mercado muerto → bloqueante

    # Dirección del momentum
    move = c[-1] - c[-5]
    if move > at * 0.3:    return 15, 0   # momentum alcista
    if move < -at * 0.3:   return 0, 15   # momentum bajista
    return 5, 5   # movimiento leve — puntos parciales

# =============================================
# SCORE FINAL — todos los filtros deben pasar
# =============================================

def score_oportunidad(d5, d1h=None):
    c=d5['c']; h=d5['h']; l=d5['l']; v=d5['v']

    # Aplicar los 5 filtros
    tb1, ts1 = filtro_tendencia(c, h, l)
    tb2, ts2 = filtro_liquidez(c, h, l)
    tb3, ts3 = filtro_confirmacion(c, h, l)
    tb4, ts4 = filtro_volumen(v)
    tb5, ts5 = filtro_momentum(c, h, l)

    score_buy  = tb1 + tb2 + tb3 + tb4 + tb5
    score_sell = ts1 + ts2 + ts3 + ts4 + ts5

    log(f'  Filtros → T:{tb1}/{ts1} L:{tb2}/{ts2} C:{tb3}/{ts3} V:{tb4}/{ts4} M:{tb5}/{ts5}')
    log(f'  Score → BUY:{score_buy} SELL:{score_sell}')

    # Filtros obligatorios: tendencia Y liquidez deben tener puntos
    # Si alguno de los dos es 0 en la dirección ganadora → bloqueado
    if score_buy > score_sell:
        if tb1 == 0 or tb2 == 0: return None, 0   # tendencia o liquidez falló
        if score_buy >= SCORE_MIN:
            return 'buy', score_buy
    elif score_sell > score_buy:
        if ts1 == 0 or ts2 == 0: return None, 0
        if score_sell >= SCORE_MIN:
            return 'sell', score_sell

    return None, 0

# =============================================
# COOLDOWN ADAPTATIVO
# =============================================



# =============================================
# SEGUIMIENTO TP/SL
# =============================================

def check_ops():
    to_rm = []
    now   = time.time()
    with lock:
        ops = list(state['active_ops'])

    for op in ops:
        cfg = ASSETS[op['asset']]
        if now - op['t'] > 7200:
            to_rm.append(op)
            log(f'{cfg["icon"]} Op #{op["n"]} timeout')
            continue

        p = fetch_spot(op['asset'])
        if not p: continue

        hit_tp = (op['dir']=='buy'  and p >= op['tp']) or \
                 (op['dir']=='sell' and p <= op['tp'])
        hit_sl = (op['dir']=='buy'  and p <= op['sl']) or \
                 (op['dir']=='sell' and p >= op['sl'])

        if not hit_tp and not hit_sl: continue

        cfg = ASSETS[op['asset']]
        n   = op['n']

        resultado = 'tp' if hit_tp else 'sl'
        if hit_tp:
            send(f'✅ TP\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'✅ TP #{n} {cfg["label"]}')
            with lock:
                state['tp_count'] += 1
                state['sl_streak'][op['asset']] = 0
        else:
            send(f'❌ SL\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'❌ SL #{n} {cfg["label"]}')
            with lock:
                state['sl_count'] += 1
                state['sl_streak'][op['asset']] += 1

        # Aprendizaje — registrar filtros activos y resultado
        with lock:
            filtros = op.get('filtros', {})
            for fname, activo in filtros.items():
                if activo and fname in state['learn']:
                    state['learn'][fname][resultado] += 1
            state['history'].append({
                'n': n, 'asset': op['asset'], 'dir': op['dir'],
                'resultado': resultado, 'score': op.get('score', 0),
                'hora': datetime.now().strftime('%H:%M'),
            })
            if len(state['history']) > 100:
                state['history'].pop(0)

        to_rm.append(op)

    with lock:
        for op in to_rm:
            if op in state['active_ops']:
                state['active_ops'].remove(op)

# =============================================
# ENVIAR OPORTUNIDAD
# =============================================

def send_op(asset, direction, px, score, filtros=None, manual=False):
    cfg   = ASSETS[asset]
    isBuy = direction == 'buy'
    tp    = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl    = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo  = 'BUY' if isBuy else 'SELL'

    with lock:
        state['op_n']         += 1
        state['sig_count']    += 1
        state['ops_today'][asset] += 1
        n = state['op_n']

    gains = '\n'.join([f'{l:.2f} → +{gain_eur(cfg["tp"],l)}€' for l in LOTS])
    hora  = now_str()

    msg = (f'{cfg["icon"]} {cfg["label"]}\n\n'
           f'{tipo}\n\n'
           f'Entrada: {fmt(px,asset)}\n'
           f'TP: {fmt(tp,asset)}\n'
           f'SL: {fmt(sl,asset)}\n\n'
           f'Ganancia estimada:\n{gains}\n\n'
           f'Hora: {hora}')
    send(msg)
    etiq = '📲 MANUAL' if manual else '📨 AUTO'
    log(f'{etiq} #{n} {tipo} {cfg["label"]} {fmt(px,asset)} score:{score}')

    with lock:
        state['last_alert'][asset] = time.time()
        state['last_key'][asset]   = f'{direction}-{round(px/(cfg["tp"]*3))}'
        state['last_signal'] = {
            'n': n, 'asset': asset, 'dir': direction,
            'px': fmt(px,asset), 'tp': fmt(tp,asset),
            'sl': fmt(sl,asset), 'time': hora,
            'icon': cfg['icon'], 'label': cfg['label'],
            'score': score,
        }
        state['active_ops'].append({
            'asset': asset, 'dir': direction,
            'tp': tp, 'sl': sl,
            't': time.time(), 'n': n,
            'score': score,
            'filtros': filtros or {},
            'manual': manual,
        })

# =============================================
# ESCANEAR ACTIVO
# =============================================

def scan(asset):
    cfg = ASSETS[asset]
    now = time.time()

    reset_daily()

    log(f'{cfg["icon"]} Escaneando {cfg["label"]}...')

    d5  = fetch(asset, '5min')
    if not d5: log(f'{cfg["icon"]} Sin datos M5'); return

    d1h = fetch(asset, '1h')
    px  = d5['c'][-1]

    with lock:
        state['price'][asset]     = fmt(px, asset)
        state['last_scan'][asset] = datetime.now().strftime('%H:%M:%S')

    direction, score = score_oportunidad(d5, d1h)

    if not direction or score < SCORE_MIN:
        log(f'{cfg["icon"]} Sin oportunidad (score:{score} min:{SCORE_MIN})')
        return

    # Anti-duplicado: misma zona de precio
    key = f'{direction}-{round(px / (cfg["tp"] * 3))}'
    with lock:
        if key == state['last_key'][asset]:
            log(f'{cfg["icon"]} Misma zona — skip')
            return

    send_op(asset, direction, px, score, filtros={
        'tendencia': True, 'liquidez': True,
        'confirmacion': True, 'volumen': True, 'momentum': True,
    })

# =============================================
# PANEL WEB
# =============================================

HTML = '''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper Scanner v4</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060a0f;color:#b8ccd8;font-family:'Courier New',monospace;font-size:13px}
.hdr{background:#0b1219;border-bottom:1px solid #162030;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{color:#ffc832;font-size:.95rem;font-weight:700;letter-spacing:3px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.g{background:#00e87a;box-shadow:0 0 8px #00e87a}.r{background:#ff2d55}.y{background:#ffc832;box-shadow:0 0 6px #ffc832}
.page{max-width:860px;margin:0 auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#0b1219;border:1px solid #162030;border-radius:8px;padding:14px}
.lbl{font-size:.5rem;letter-spacing:3px;color:#3a5a72;text-transform:uppercase;margin-bottom:8px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.stat{background:#080e14;border-radius:6px;padding:10px;text-align:center;border:1px solid #162030}
.sv{font-size:1.4rem;font-weight:700;color:#ffc832}
.sl{font-size:.5rem;color:#3a5a72;margin-top:3px}
.px{font-size:1.5rem;font-weight:700}
.logs{background:#040810;border-radius:6px;padding:10px;height:300px;overflow-y:auto;font-size:.68rem;line-height:1.8}
.logs p{border-bottom:1px solid #0a1520;padding:1px 0}
.logs p:last-child{color:#00e87a}
.btn{padding:9px 16px;background:transparent;border:1px solid #ffc83255;border-radius:5px;color:#ffc832;font-family:inherit;font-size:.72rem;letter-spacing:1px;cursor:pointer;transition:all .2s}
.btn:hover{background:#ffc83215}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.sig{background:#080e14;border-radius:6px;padding:12px;border-left:3px solid #ffc832}
.sig.buy{border-left-color:#00e87a}.sig.sell{border-left-color:#ff5fa0}
.buy{color:#00e87a}.sell{color:#ff5fa0}.gold{color:#ffc832}
.streak{background:#ff2d5515;border:1px solid #ff2d5535;border-radius:5px;padding:6px 10px;font-size:.68rem;color:#ff2d55;margin-top:6px}
@media(max-width:500px){.g4{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">⚡ SNIPER v4</div>
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <span><span class="dot g" id="sDot"></span><span id="sText" style="font-size:.7rem">—</span></span>
    <span style="color:#3a5a72;font-size:.65rem" id="clk">--:--:--</span>
  </div>
</div>

<div class="page">

  <div class="g2">
    <div class="card">
      <div class="lbl">🥇 ORO</div>
      <div class="px gold" id="pxG">—</div>
      <div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scG">—</div>
      <div style="font-size:.6rem;margin-top:4px" id="slG"></div>
    </div>
    <div class="card">
      <div class="lbl">₿ BITCOIN</div>
      <div class="px" style="color:#f7931a" id="pxB">—</div>
      <div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scB">—</div>
      <div style="font-size:.6rem;margin-top:4px" id="slB"></div>
    </div>
  </div>

  <div class="g4">
    <div class="stat"><div class="sv" id="stS">0</div><div class="sl">SEÑALES</div></div>
    <div class="stat"><div class="sv buy" id="stT">0</div><div class="sl">TP ✅</div></div>
    <div class="stat"><div class="sv sell" id="stL">0</div><div class="sl">SL ❌</div></div>
    <div class="stat"><div class="sv gold" id="stE">—</div><div class="sl">EFECTIVIDAD</div></div>
  </div>

  <div class="card">
    <div class="lbl">Última señal</div>
    <div id="lastSig" style="color:#3a5a72;font-size:.75rem">Sin señales aún — escaneando con score mínimo 80</div>
  </div>

  <div class="card">
    <div class="lbl">Registro en tiempo real</div>
    <div class="logs" id="logs"></div>
  </div>

  <div class="btns">
    <button class="btn" onclick="force()">🔄 Forzar análisis</button>
    <button class="btn" onclick="restart()">⚡ Reiniciar</button>
  </div>

  <!-- SEÑAL MANUAL -->
  <div class="card">
    <div class="lbl">📲 Enviar señal manual</div>
    <div class="g2" style="margin-bottom:8px">
      <div>
        <div style="font-size:.58rem;color:#3a5a72;margin-bottom:4px">ACTIVO</div>
        <select id="mAsset" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem">
          <option value="gold">🥇 ORO</option>
          <option value="btc">₿ BITCOIN</option>
        </select>
      </div>
      <div>
        <div style="font-size:.58rem;color:#3a5a72;margin-bottom:4px">DIRECCIÓN</div>
        <select id="mDir" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem">
          <option value="buy">💚 BUY</option>
          <option value="sell">🩷 SELL</option>
        </select>
      </div>
    </div>
    <div style="font-size:.58rem;color:#3a5a72;margin-bottom:4px">PRECIO DE ENTRADA (dejar vacío = precio actual)</div>
    <input id="mPx" type="number" step="0.01" placeholder="Ej: 3312.50" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#b8ccd8;font-family:inherit;font-size:.85rem;margin-bottom:8px;outline:none">
    <button class="btn" style="width:100%;font-size:.8rem" onclick="sendManual()">📨 ENVIAR SEÑAL A TELEGRAM</button>
    <div id="mResult" style="margin-top:6px;font-size:.65rem;text-align:center"></div>
  </div>

  <!-- APRENDIZAJE -->
  <div class="card">
    <div class="lbl">🧠 Aprendizaje por resultados</div>
    <div id="learnGrid" style="display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-bottom:10px"></div>
    <div class="lbl" style="margin-top:8px">Últimas operaciones</div>
    <div id="histList" style="font-size:.65rem;max-height:180px;overflow-y:auto"></div>
  </div>

</div>

<script>
setInterval(()=>document.getElementById('clk').textContent=new Date().toLocaleTimeString('es-ES'),1000);

async function poll(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());
    document.getElementById('sDot').className='dot '+(d.status==='ACTIVO'?'g':'y');
    document.getElementById('sText').textContent=d.status;
    document.getElementById('pxG').textContent=d.price.gold;
    document.getElementById('pxB').textContent=d.price.btc;
    document.getElementById('scG').textContent='Scan: '+d.last_scan.gold;
    document.getElementById('scB').textContent='Scan: '+d.last_scan.btc;

    // Streak SL
    const sg=d.sl_streak.gold, sb=d.sl_streak.btc;
    document.getElementById('slG').innerHTML=sg>0?`<span style="color:#ff2d55">⚠️ ${sg} SL seguidos</span>`:'';
    document.getElementById('slB').innerHTML=sb>0?`<span style="color:#ff2d55">⚠️ ${sb} SL seguidos</span>`:'';

    document.getElementById('stS').textContent=d.sig_count;
    document.getElementById('stT').textContent=d.tp_count;
    document.getElementById('stL').textContent=d.sl_count;
    const tot=d.tp_count+d.sl_count;
    const pct=tot>0?Math.round(d.tp_count/tot*100):null;
    const el=document.getElementById('stE');
    el.textContent=pct!==null?pct+'%':'—';
    el.style.color=pct===null?'var(--gold,#ffc832)':pct>=60?'#00e87a':pct>=45?'#ffc832':'#ff2d55';

    const ls=d.last_signal;
    if(ls){
      const b=ls.dir==='buy';
      document.getElementById('lastSig').innerHTML=
        `<div class="sig ${ls.dir}">
          <div style="font-size:.85rem;font-weight:700" class="${b?'buy':'sell'}">${ls.icon} ${ls.label} — ${b?'BUY':'SELL'} #${ls.n} <span style="font-size:.6rem;color:#3a5a72">(score ${ls.score})</span></div>
          <div style="margin-top:5px;font-size:.72rem">Entrada: <b>${ls.px}</b> · TP: <b class="buy">${ls.tp}</b> · SL: <b class="sell">${ls.sl}</b></div>
          <div style="font-size:.6rem;color:#3a5a72;margin-top:2px">${ls.time}</div>
        </div>`;
    }

    const logsEl=document.getElementById('logs');
    logsEl.innerHTML=d.logs.slice().reverse().map(l=>`<p>${l}</p>`).join('');
  }catch(e){}
}

async function force(){
  await fetch('/api/force',{method:'POST'});
  setTimeout(poll,1500);
}
async function restart(){
  if(confirm('¿Reiniciar?')){
    await fetch('/api/restart',{method:'POST'});
    setTimeout(poll,1500);
  }
}

async function sendManual(){
  const asset=document.getElementById('mAsset').value;
  const dir=document.getElementById('mDir').value;
  const pxRaw=document.getElementById('mPx').value;
  const px=pxRaw?parseFloat(pxRaw):null;
  if(px===null||isNaN(px)){
    // usar precio actual del panel
    const cur=asset==='gold'?document.getElementById('pxG').textContent:document.getElementById('pxB').textContent;
    const pxNum=parseFloat(cur.replace(/[^0-9.]/g,''));
    if(!pxNum){document.getElementById('mResult').innerHTML='<span style="color:#ff2d55">Introduce un precio o espera a que cargue</span>';return;}
    await doSend(asset,dir,pxNum);
  } else {
    await doSend(asset,dir,px);
  }
}
async function doSend(asset,dir,px){
  const r=await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset,direction:dir,px})}).then(r=>r.json());
  const el=document.getElementById('mResult');
  if(r.ok){el.innerHTML='<span style="color:#00e87a">✅ Señal enviada a Telegram</span>';}
  else{el.innerHTML=`<span style="color:#ff2d55">❌ ${r.error}</span>`;}
  setTimeout(()=>el.innerHTML='',4000);
  loadLearn();
}

async function loadLearn(){
  try{
    const d=await fetch('/api/learn').then(r=>r.json());
    const names={'tendencia':'TENDENCIA','liquidez':'LIQUIDEZ','confirmacion':'CONFIRM.','volumen':'VOLUMEN','momentum':'MOMENTUM'};
    const grid=document.getElementById('learnGrid');
    grid.innerHTML='';
    for(const[k,v] of Object.entries(d.learn)){
      const pct=v.pct;
      const col=pct===null?'#3a5a72':pct>=60?'#00e87a':pct>=45?'#ffc832':'#ff2d55';
      grid.innerHTML+=`<div style="background:#080e14;border-radius:5px;padding:7px;text-align:center;border:1px solid #162030">
        <div style="font-size:.46rem;color:#3a5a72;letter-spacing:1px">${names[k]||k}</div>
        <div style="font-size:.95rem;font-weight:700;color:${col};margin-top:2px">${pct!==null?pct+'%':'—'}</div>
        <div style="font-size:.46rem;color:#3a5a72">${v.tp}TP ${v.sl}SL</div>
      </div>`;
    }
    const hist=document.getElementById('histList');
    hist.innerHTML=d.history.slice().reverse().map(h=>{
      const ok=h.resultado==='tp';
      return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #0a1520">
        <span style="color:${ok?'#00e87a':'#ff2d55'}">${ok?'✅':'❌'} ${h.resultado.toUpperCase()} #${h.n}</span>
        <span style="color:#3a5a72">${h.asset==='gold'?'🥇':'₿'} ${h.dir.toUpperCase()}</span>
        <span style="color:#3a5a72">${h.hora}</span>
        <span style="color:#ffc832">score:${h.score}</span>
      </div>`;
    }).join('');
  }catch(e){}
}

poll(); setInterval(poll,3000); setInterval(loadLearn,10000); loadLearn();
</script>
</body>
</html>'''

# =============================================
# FLASK
# =============================================

app = Flask(__name__)

@app.route('/')
def index(): return render_template_string(HTML)

@app.route('/api/state')
def api_state():
    with lock:
        return jsonify({
            'status':      state['status'],
            'price':       state['price'],
            'last_scan':   state['last_scan'],
            'last_signal': state['last_signal'],
            'tp_count':    state['tp_count'],
            'sl_count':    state['sl_count'],
            'sig_count':   state['sig_count'],
            'sl_streak':   state['sl_streak'],
            'ops_today':   state['ops_today'],
            'logs':        state['logs'],
        })

@app.route('/api/force', methods=['POST'])
def api_force():
    with lock:
        state['last_alert'] = {'gold': 0, 'btc': 0}
        state['last_key']   = {'gold': None, 'btc': None}
    log('⚡ Análisis forzado')
    return jsonify({'ok': True})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    with lock:
        state['last_alert']  = {'gold': 0, 'btc': 0}
        state['last_key']    = {'gold': None, 'btc': None}
        state['active_ops']  = []
        state['sl_streak']   = {'gold': 0, 'btc': 0}
    log('🔄 Reiniciado por panel web')
    return jsonify({'ok': True})

@app.route('/api/manual', methods=['POST'])
def api_manual():
    from flask import request
    data = request.get_json()
    asset     = data.get('asset','gold')
    direction = data.get('direction','buy')
    try:
        px = float(data.get('px', 0))
        if px <= 0: return jsonify({'ok':False,'error':'Precio inválido'})
    except:
        return jsonify({'ok':False,'error':'Precio inválido'})
    send_op(asset, direction, px, score=0, manual=True)
    log(f'📲 Señal MANUAL: {direction.upper()} {ASSETS[asset]["label"]} {fmt(px,asset)}')
    return jsonify({'ok': True})

@app.route('/api/learn')
def api_learn():
    with lock:
        learn   = dict(state['learn'])
        history = list(state['history'][-20:])
    result = {}
    for fname, r in learn.items():
        tot = r['tp'] + r['sl']
        result[fname] = {
            'tp': r['tp'], 'sl': r['sl'],
            'pct': round(r['tp']/tot*100) if tot else None
        }
    return jsonify({'learn': result, 'history': history})

# =============================================
# LOOP
# =============================================

def scanner_loop():
    log('SNIPER SCANNER v4.1 — score mínimo 80')
    with lock: state['status'] = 'ACTIVO'

    while True:
        try:
            if state['active_ops']:
                check_ops()

            is_weekend = datetime.now().weekday() >= 5
            if not is_weekend:
                scan('gold')
            else:
                log('Fin de semana — ORO cerrado')

            time.sleep(5)
            scan('btc')
            time.sleep(20)

        except Exception as e:
            log(f'Error: {e}')
            time.sleep(20)

if __name__ == '__main__':
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
