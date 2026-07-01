import os, time, requests, threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request

# ================================================
# SNIPER SCANNER v18.5
# Escáner de Confluencia Institucional
# Winrate objetivo > 60%
# ================================================

TG_TOKEN    = os.environ.get('TG_TOKEN',    '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ')
TG_CHAT_ID  = os.environ.get('TG_CHAT_ID',  '6467338067')
TG_GROUP_ID = os.environ.get('TG_GROUP_ID', '-5123266724')
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'
PORT        = int(os.environ.get('PORT', 5000))

ASSETS = {
    'gold': {'icon':'🥇','label':'ORO',     'tp':10,  'sl':5,   'sym12':'XAU/USD','symB':'XAUUSDT','min':2000},
    'btc':  {'icon':'₿', 'label':'BITCOIN', 'tp':250, 'sl':125, 'sym12':'BTC/USD','symB':'BTCUSDT','min':10000},
}
LOTS     = [0.01, 0.02, 0.05, 0.10, 0.15, 0.50]
EUR_RATE = 0.92

# ── Umbrales ─────────────────────────────────────
SCORE_MIN        = 80   # mínimo normal
SCORE_SURVIVAL   = 90   # tras 3 SL consecutivos
MAX_OPS_DAY      = 5    # máximo por activo por día

# ── Cooldowns tras SL ────────────────────────────
COOLDOWN_SL1 = 30 * 60   # 30 min tras 1 SL
COOLDOWN_SL2 = 60 * 60   # 1h tras 2 SL consecutivos

# ── Estado ───────────────────────────────────────
state = {
    'status': 'INICIANDO',
    'price':  {'gold':'—','btc':'—'},
    'last_scan': {'gold':'—','btc':'—'},
    'last_signal': None,
    'tp': 0, 'sl': 0, 'sigs': 0,
    'logs': [], 'ops': [], 'op_n': 0, 'history': [],
    'last_alert':  {'gold': 0,    'btc': 0},
    'last_key':    {'gold': None, 'btc': None},
    'sl_streak':   {'gold': 0,    'btc': 0},
    'ops_today':   {'gold': 0,    'btc': 0},
    'day_date':    None,
}
lock = threading.Lock()

# ── Utils ─────────────────────────────────────────
def now_str(): return datetime.now().strftime('%H:%M')

def log(m):
    line = f'{datetime.now().strftime("%H:%M:%S")}  {m}'
    print(line, flush=True)
    with lock:
        state['logs'].append(line)
        if len(state['logs']) > 100: state['logs'].pop(0)

def send(msg):
    for c in [TG_CHAT_ID, TG_GROUP_ID]:
        try: requests.post(TG_API, json={'chat_id':c,'text':msg}, timeout=10)
        except: pass

def fmt(v, a): return f'{round(v):,}' if a=='btc' else f'{v:.2f}'
def gain(tp, lot): return round(tp * lot * 10 * EUR_RATE, 2)

def reset_daily():
    today = datetime.now().strftime('%Y-%m-%d')
    with lock:
        if state['day_date'] != today:
            state['day_date']  = today
            state['ops_today'] = {'gold':0,'btc':0}
            state['sl_streak'] = {'gold':0,'btc':0}
            log('📅 Reset diario')

# ── Datos ─────────────────────────────────────────
def candles(sym, iv='5min', n=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series',
            params={'symbol':sym,'interval':iv,'outputsize':n,'apikey':TWELVE_KEY},
            timeout=12).json()
        if r.get('status') == 'error': return None
        v = r.get('values', [])
        if len(v) < 20: return None
        return {
            'c': [float(x['close'])  for x in reversed(v)],
            'h': [float(x['high'])   for x in reversed(v)],
            'l': [float(x['low'])    for x in reversed(v)],
            'v': [float(x.get('volume',0)) for x in reversed(v)],
        }
    except: return None

def candles_b(sym, iv='5m', n=60):
    try:
        k = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval={iv}&limit={n}',
            timeout=8).json()
        if len(k) < 20: return None
        return {
            'c': [float(x[4]) for x in k],
            'h': [float(x[2]) for x in k],
            'l': [float(x[3]) for x in k],
            'v': [float(x[5]) for x in k],
        }
    except: return None

def get_data(asset, tf='5min'):
    cfg = ASSETS[asset]
    d = candles(cfg['sym12'], tf)
    if d and d['c'][-1] > cfg['min']: return d
    iv = {'5min':'5m','15min':'15m','1h':'1h'}.get(tf,'5m')
    return candles_b(cfg['symB'], iv)

def get_spot(asset):
    cfg = ASSETS[asset]
    try:
        p = float(requests.get('https://api.twelvedata.com/price',
            params={'symbol':cfg['sym12'],'apikey':TWELVE_KEY},timeout=6).json().get('price',0))
        if p > cfg['min']: return p
    except: pass
    try:
        t = requests.get(
            f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={cfg["symB"]}',
            timeout=5).json()
        if 'bidPrice' in t:
            return (float(t['bidPrice'])+float(t['askPrice']))/2
    except: pass
    return None

# ── Indicadores ───────────────────────────────────
def ema(c, n):
    if len(c) < n: return c[-1]
    k = 2/(n+1); e = c[0]
    for p in c[1:]: e = p*k + e*(1-k)
    return e

def atr(h, l, c, n=14):
    if len(c) < n+1: return 1
    t = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1,len(c))]
    return sum(t[-n:]) / n

# ================================================
# MATRIZ DE CONFLUENCIA INSTITUCIONAL
# Máximo teórico: 100 puntos
# ================================================

def matriz_confluencia(d5, d15):
    c5=d5['c']; h5=d5['h']; l5=d5['l']; v5=d5['v']
    c15=d15['c'] if d15 else c5
    h15=d15['h'] if d15 else h5
    l15=d15['l'] if d15 else l5

    score_buy = score_sell = 0
    bloqueado_buy = bloqueado_sell = False

    # ─────────────────────────────────────────────
    # 1. TENDENCIA MACRO ALINEADA M5+M15 (20pts) — OBLIGATORIO
    # ─────────────────────────────────────────────
    px5  = c5[-1]
    e20_5 = ema(c5, 20); e50_5 = ema(c5, min(50,len(c5)))
    e20_15= ema(c15,20); e50_15= ema(c15,min(50,len(c15)))
    px15  = c15[-1]

    tend_buy_m5  = px5  > e20_5  and e20_5  > e50_5
    tend_sell_m5 = px5  < e20_5  and e20_5  < e50_5
    tend_buy_m15 = px15 > e20_15 and e20_15 > e50_15
    tend_sell_m15= px15 < e20_15 and e20_15 < e50_15

    if tend_buy_m5 and tend_buy_m15:
        score_buy += 20
    elif not (tend_buy_m5 and tend_buy_m15):
        bloqueado_buy = True   # tendencias no alineadas → bloqueado

    if tend_sell_m5 and tend_sell_m15:
        score_sell += 20
    elif not (tend_sell_m5 and tend_sell_m15):
        bloqueado_sell = True

    # ─────────────────────────────────────────────
    # 2. SWEEP DE LIQUIDEZ (25pts) — OBLIGATORIO
    # ─────────────────────────────────────────────
    sweep_buy = sweep_sell = False
    if len(c5) >= 6:
        prev_h = max(h5[-6:-1])
        prev_l = min(l5[-6:-1])
        rng    = h5[-1] - l5[-1]
        if rng > 0:
            # Sweep de mínimos → potencial BUY
            if l5[-1] < prev_l and c5[-1] > prev_l:
                mecha = (min(c5[-2],c5[-1]) - l5[-1]) / rng
                if mecha >= 0.35:
                    sweep_buy = True
                    score_buy += 25
            # Sweep de máximos → potencial SELL
            if h5[-1] > prev_h and c5[-1] < prev_h:
                mecha = (h5[-1] - max(c5[-2],c5[-1])) / rng
                if mecha >= 0.35:
                    sweep_sell = True
                    score_sell += 25

    if not sweep_buy:  bloqueado_buy  = True
    if not sweep_sell: bloqueado_sell = True

    # ─────────────────────────────────────────────
    # 3. CHoCH — CONFIRMACIÓN DE ESTRUCTURA (20pts) — OBLIGATORIO
    # ─────────────────────────────────────────────
    choch_buy = choch_sell = False
    if len(c5) >= 4:
        # CHoCH alcista: cierra por encima del cuerpo de la vela anterior
        if c5[-1] > max(c5[-3], c5[-2]) and c5[-1] > c5[-2]:
            choch_buy = True
            score_buy += 20
        # CHoCH bajista: cierra por debajo del cuerpo de la vela anterior
        if c5[-1] < min(c5[-3], c5[-2]) and c5[-1] < c5[-2]:
            choch_sell = True
            score_sell += 20

    if not choch_buy:  bloqueado_buy  = True
    if not choch_sell: bloqueado_sell = True

    # ─────────────────────────────────────────────
    # 4. VOLUMEN DE ABSORCIÓN (15pts) — SUMADOR
    # No bloquea, solo suma si el volumen acompaña
    # ─────────────────────────────────────────────
    if v5 and len(v5) >= 11:
        avg_v = sum(v5[-11:-1]) / 10
        if avg_v > 0 and v5[-1] >= avg_v * 1.15:
            score_buy  += 15
            score_sell += 15

    # ─────────────────────────────────────────────
    # 5. MOMENTUM DE DESPLAZAMIENTO (20pts) — SUMADOR
    # Expansión real vs ruido lateral
    # ─────────────────────────────────────────────
    if len(h5) >= 6:
        at = atr(h5, l5, c5)
        rng3 = max(h5[-3:]) - min(l5[-3:])
        rng_prev = max(h5[-6:-3]) - min(l5[-6:-3])
        if at > 0 and rng_prev > 0:
            expansion = rng3 / rng_prev
            if expansion >= 1.5:
                pts_mom = 20
            elif expansion >= 1.2:
                pts_mom = 12
            elif expansion >= 1.0:
                pts_mom = 6
            else:
                pts_mom = 0
            score_buy  += pts_mom
            score_sell += pts_mom

    # ─────────────────────────────────────────────
    # Aplicar bloqueos obligatorios
    # ─────────────────────────────────────────────
    if bloqueado_buy:  score_buy  = 0
    if bloqueado_sell: score_sell = 0

    score_buy  = min(score_buy,  100)
    score_sell = min(score_sell, 100)

    log(f'  Matriz → BUY:{score_buy} SELL:{score_sell} | T:{tend_buy_m5&tend_buy_m15}/{tend_sell_m5&tend_sell_m15} S:{sweep_buy}/{sweep_sell} C:{choch_buy}/{choch_sell}')

    if score_buy >= SCORE_MIN and score_buy > score_sell:
        return 'buy', score_buy
    if score_sell >= SCORE_MIN and score_sell > score_buy:
        return 'sell', score_sell
    return None, 0

# ── Cooldown adaptativo ───────────────────────────
def get_cooldown(asset):
    with lock: streak = state['sl_streak'][asset]
    if streak >= 2: return COOLDOWN_SL2
    if streak >= 1: return COOLDOWN_SL1
    return 0   # sin cooldown si no hay racha

def get_score_min(asset):
    with lock: streak = state['sl_streak'][asset]
    return SCORE_SURVIVAL if streak >= 3 else SCORE_MIN

# ── Seguimiento TP/SL ─────────────────────────────
def check_ops():
    to_rm = []; now = time.time()
    with lock: ops = list(state['ops'])
    for op in ops:
        cfg = ASSETS[op['a']]
        if now - op['t'] > 7200:
            to_rm.append(op)
            log(f'{cfg["icon"]} Op #{op["n"]} timeout')
            continue
        p = get_spot(op['a'])
        if not p: continue
        hit_tp = (op['d']=='buy'  and p >= op['tp']) or (op['d']=='sell' and p <= op['tp'])
        hit_sl = (op['d']=='buy'  and p <= op['sl']) or (op['d']=='sell' and p >= op['sl'])
        log(f'{cfg["icon"]} #{op["n"]} precio:{fmt(p,op["a"])} TP:{fmt(op["tp"],op["a"])} SL:{fmt(op["sl"],op["a"])}')
        if hit_tp or hit_sl:
            res = 'tp' if hit_tp else 'sl'
            send(f'{"✅ TP" if hit_tp else "❌ SL"}\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'{"✅ TP" if hit_tp else "❌ SL"} #{op["n"]} {cfg["label"]}')
            with lock:
                state['tp' if hit_tp else 'sl'] += 1
                if res == 'sl':
                    state['sl_streak'][op['a']] += 1
                else:
                    state['sl_streak'][op['a']] = 0
                state['history'].append({'n':op['n'],'a':op['a'],'d':op['d'],'r':res,'s':op['s'],'h':now_str()})
                if len(state['history']) > 50: state['history'].pop(0)
            to_rm.append(op)
    with lock:
        for op in to_rm:
            if op in state['ops']: state['ops'].remove(op)

# ── Enviar señal ──────────────────────────────────
def send_signal(asset, direction, px, sc, manual=False):
    cfg = ASSETS[asset]; isBuy = direction == 'buy'
    tp  = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl  = px - cfg['sl'] if isBuy else px + cfg['sl']
    tipo = 'BUY' if isBuy else 'SELL'

    with lock:
        state['op_n'] += 1; state['sigs'] += 1
        state['ops_today'][asset] += 1
        n = state['op_n']

    msg = (
        f'{cfg["icon"]} {cfg["label"]}\n\n'
        f'{tipo}\n\n'
        f'Entrada: {fmt(px,asset)}\n'
        f'TP: {fmt(tp,asset)}\n'
        f'SL: {fmt(sl,asset)}\n\n'
        f'Hora: {now_str()}'
    )
    send(msg)
    log(f'{"📲 MANUAL" if manual else "📨 AUTO"} #{n} {tipo} {cfg["label"]} {fmt(px,asset)} score:{sc}')

    with lock:
        state['last_alert'][asset] = time.time()
        state['last_key'][asset]   = f'{direction}-{round(px/(cfg["tp"]*3))}'
        state['last_signal'] = {
            'n':n,'a':asset,'d':direction,
            'px':fmt(px,asset),'tp':fmt(tp,asset),'sl':fmt(sl,asset),
            'sc':sc,'h':now_str(),'icon':cfg['icon'],'label':cfg['label']
        }
        state['ops'].append({
            'a':asset,'d':direction,'tp':tp,'sl':sl,
            't':time.time(),'n':n,'s':sc
        })

# ── Escanear ──────────────────────────────────────
def scan(asset):
    cfg = ASSETS[asset]
    reset_daily()

    with lock:
        ops_hoy = state['ops_today'][asset]
        last    = state['last_alert'][asset]
        streak  = state['sl_streak'][asset]

    if ops_hoy >= MAX_OPS_DAY:
        log(f'{cfg["icon"]} Máx {MAX_OPS_DAY} ops alcanzado hoy')
        return

    cd = get_cooldown(asset)
    if cd > 0 and time.time() - last < cd:
        mins = (cd-(time.time()-last))/60
        log(f'{cfg["icon"]} Cooldown {mins:.0f}min (streak:{streak})')
        return

    log(f'{cfg["icon"]} Escaneando {cfg["label"]}...')
    d5  = get_data(asset, '5min')
    if not d5: log(f'{cfg["icon"]} Sin datos M5'); return
    d15 = get_data(asset, '15min')

    px = d5['c'][-1]
    with lock:
        state['price'][asset]     = fmt(px, asset)
        state['last_scan'][asset] = datetime.now().strftime('%H:%M:%S')

    score_min = get_score_min(asset)
    direction, sc = matriz_confluencia(d5, d15)

    if not direction or sc < score_min:
        log(f'{cfg["icon"]} Sin confluencia (score:{sc} min:{score_min})')
        return

    key = f'{direction}-{round(px/(cfg["tp"]*3))}'
    with lock:
        if key == state['last_key'][asset]:
            log(f'{cfg["icon"]} Misma zona — skip')
            return

    send_signal(asset, direction, px, sc)

# ── Panel web ─────────────────────────────────────
HTML = '''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper v18.5</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060a0f;color:#b8ccd8;font-family:'Courier New',monospace;font-size:13px}
.hdr{background:#0b1219;border-bottom:1px solid #162030;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{color:#ffc832;font-size:.95rem;font-weight:700;letter-spacing:3px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.g{background:#00e87a;box-shadow:0 0 8px #00e87a}.y{background:#ffc832}.r{background:#ff2d55}
.page{max-width:900px;margin:0 auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#0b1219;border:1px solid #162030;border-radius:8px;padding:14px}
.lbl{font-size:.5rem;letter-spacing:3px;color:#3a5a72;text-transform:uppercase;margin-bottom:8px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.stat{background:#080e14;border-radius:6px;padding:10px;text-align:center;border:1px solid #162030}
.sv{font-size:1.4rem;font-weight:700;color:#ffc832}
.sl2{font-size:.5rem;color:#3a5a72;margin-top:3px}
.px{font-size:1.5rem;font-weight:700}
.logs{background:#040810;border-radius:6px;padding:10px;height:280px;overflow-y:auto;font-size:.68rem;line-height:1.8}
.logs p{border-bottom:1px solid #0a1520;padding:1px 0}
.btn{padding:9px 16px;background:transparent;border:1px solid #ffc83255;border-radius:5px;color:#ffc832;font-family:inherit;font-size:.72rem;letter-spacing:1px;cursor:pointer;transition:all .2s}
.btn:hover{background:#ffc83215}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.buy{color:#00e87a}.sell{color:#ff5fa0}.gold{color:#ffc832}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">⚡ SNIPER v18.5</div>
  <div style="display:flex;align-items:center;gap:12px">
    <span><span class="dot g" id="sDot"></span><span id="sText" style="font-size:.7rem">—</span></span>
    <span style="color:#3a5a72;font-size:.65rem" id="clk">--:--:--</span>
  </div>
</div>
<div class="page">

  <div class="g2">
    <div class="card">
      <div class="lbl">🥇 ORO XAU/USD</div>
      <div class="px gold" id="pxG">—</div>
      <div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scG">—</div>
      <div style="font-size:.6rem;margin-top:3px" id="slG"></div>
    </div>
    <div class="card">
      <div class="lbl">₿ BITCOIN</div>
      <div class="px" style="color:#f7931a" id="pxB">—</div>
      <div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scB">—</div>
      <div style="font-size:.6rem;margin-top:3px" id="slB"></div>
    </div>
  </div>

  <div class="g4">
    <div class="stat"><div class="sv" id="stS">0</div><div class="sl2">SEÑALES</div></div>
    <div class="stat"><div class="sv buy" id="stT">0</div><div class="sl2">✅ TP</div></div>
    <div class="stat"><div class="sv sell" id="stL">0</div><div class="sl2">❌ SL</div></div>
    <div class="stat"><div class="sv gold" id="stE">—</div><div class="sl2">WINRATE</div></div>
  </div>

  <div class="card">
    <div class="lbl">Última señal</div>
    <div id="lastSig" style="color:#3a5a72;font-size:.75rem">Sin señales aún — Score mínimo: 80pts</div>
  </div>

  <div class="card">
    <div class="lbl">Registro en tiempo real</div>
    <div class="logs" id="logs"></div>
  </div>

  <div class="btns">
    <button class="btn" onclick="force()">🔄 Forzar análisis</button>
    <button class="btn" onclick="restart()">⚡ Reiniciar</button>
  </div>

  <div class="card">
    <div class="lbl">📲 Señal manual</div>
    <div class="g2" style="margin-bottom:6px">
      <div>
        <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">ACTIVO</div>
        <select id="mA" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem">
          <option value="gold">🥇 ORO</option><option value="btc">₿ BITCOIN</option>
        </select>
      </div>
      <div>
        <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">DIRECCIÓN</div>
        <select id="mD" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem">
          <option value="buy">💚 BUY</option><option value="sell">🩷 SELL</option>
        </select>
      </div>
    </div>
    <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">PRECIO (vacío = precio actual)</div>
    <input id="mP" type="number" step="0.01" placeholder="Ej: 3312.50" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#b8ccd8;font-family:inherit;font-size:.85rem;margin-bottom:8px;outline:none">
    <button class="btn" style="width:100%" onclick="sendManual()">📨 ENVIAR A TELEGRAM</button>
    <div id="mR" style="margin-top:6px;font-size:.65rem;text-align:center"></div>
  </div>

  <div class="card">
    <div class="lbl">🧠 Historial</div>
    <div id="hist" style="font-size:.68rem;max-height:200px;overflow-y:auto"></div>
  </div>

  <div class="card">
    <div class="lbl">📊 ORO XAU/USD — M5 EN VIVO</div>
    <iframe src="https://s.tradingview.com/widgetembed/?frameElementId=tv1&symbol=OANDA%3AXAUUSD&interval=5&theme=dark&style=1&locale=es&toolbar_bg=%230b1219&hide_top_toolbar=0&studies=RSI%4014&withdateranges=0&hideideas=1"
      style="width:100%;height:300px;border:none;border-radius:6px" allowtransparency="true" scrolling="no"></iframe>
  </div>

  <div class="card">
    <div class="lbl">📊 BITCOIN — M5 EN VIVO</div>
    <iframe src="https://s.tradingview.com/widgetembed/?frameElementId=tv2&symbol=BINANCE%3ABTCUSDT&interval=5&theme=dark&style=1&locale=es&toolbar_bg=%230b1219&hide_top_toolbar=0&studies=RSI%4014&withdateranges=0&hideideas=1"
      style="width:100%;height:300px;border:none;border-radius:6px" allowtransparency="true" scrolling="no"></iframe>
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
    const sg=d.sl_streak.gold,sb=d.sl_streak.btc;
    document.getElementById('slG').innerHTML=sg>0?`<span style="color:#ff2d55">⚠️ ${sg} SL seguidos</span>`:'<span style="color:#00e87a">✓ OK</span>';
    document.getElementById('slB').innerHTML=sb>0?`<span style="color:#ff2d55">⚠️ ${sb} SL seguidos</span>`:'<span style="color:#00e87a">✓ OK</span>';
    document.getElementById('stS').textContent=d.sigs;
    document.getElementById('stT').textContent=d.tp;
    document.getElementById('stL').textContent=d.sl;
    const tot=d.tp+d.sl,pct=tot>0?Math.round(d.tp/tot*100):null;
    const el=document.getElementById('stE');
    el.textContent=pct!==null?pct+'%':'—';
    el.style.color=pct===null?'#ffc832':pct>=60?'#00e87a':pct>=45?'#ffc832':'#ff2d55';
    const ls=d.last_signal;
    if(ls){
      const b=ls.d==='buy';
      document.getElementById('lastSig').innerHTML=
        `<div style="border-left:3px solid ${b?'#00e87a':'#ff5fa0'};padding:10px;background:#080e14;border-radius:0 6px 6px 0">
          <div style="font-weight:700;color:${b?'#00e87a':'#ff5fa0'}">${ls.icon} ${ls.label} — ${b?'BUY':'SELL'} #${ls.n} <span style="color:#3a5a72;font-size:.6rem">score:${ls.sc}</span></div>
          <div style="margin-top:5px;font-size:.75rem">Entrada:<b>${ls.px}</b> TP:<b style="color:#00e87a">${ls.tp}</b> SL:<b style="color:#ff2d55">${ls.sl}</b></div>
          <div style="font-size:.6rem;color:#3a5a72;margin-top:2px">${ls.h}</div>
        </div>`;
    }
    document.getElementById('logs').innerHTML=d.logs.slice().reverse().map(l=>`<p>${l}</p>`).join('');
    const hist=document.getElementById('hist');
    hist.innerHTML=d.history.length
      ? d.history.slice().reverse().map(h=>`<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #0a1520">
          <span style="color:${h.r==='tp'?'#00e87a':'#ff2d55'}">${h.r==='tp'?'✅':'❌'} #${h.n}</span>
          <span style="color:#3a5a72">${h.a==='gold'?'🥇':'₿'} ${h.d.toUpperCase()}</span>
          <span style="color:#ffc832">score:${h.s}</span>
          <span style="color:#3a5a72">${h.h}</span>
        </div>`).join('')
      : '<div style="color:#3a5a72;padding:8px">Sin operaciones aún</div>';
  }catch(e){}
}
async function force(){
  await fetch('/api/force',{method:'POST'});
  setTimeout(poll,1000);
}
async function restart(){
  if(confirm('¿Reiniciar escáner?')){
    await fetch('/api/restart',{method:'POST'});
    setTimeout(poll,1000);
  }
}
async function sendManual(){
  const a=document.getElementById('mA').value;
  const d=document.getElementById('mD').value;
  const pv=document.getElementById('mP').value;
  const r=await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({asset:a,direction:d,px:pv?parseFloat(pv):0})}).then(r=>r.json());
  const el=document.getElementById('mR');
  el.innerHTML=r.ok?'<span style="color:#00e87a">✅ Señal enviada</span>':'<span style="color:#ff2d55">❌ '+r.error+'</span>';
  setTimeout(()=>el.innerHTML='',4000);
  poll();
}
poll(); setInterval(poll,3000);
</script>
</body>
</html>'''

# ── Flask ─────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def index(): return render_template_string(HTML)

@app.route('/api/state')
def api_state():
    with lock:
        return jsonify({k:v for k,v in state.items() if k!='ops'})

@app.route('/api/force', methods=['POST'])
def api_force():
    with lock:
        state['last_alert'] = {'gold':0,'btc':0}
        state['last_key']   = {'gold':None,'btc':None}
    log('⚡ Análisis forzado')
    return jsonify({'ok':True})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    with lock:
        state['last_alert']  = {'gold':0,'btc':0}
        state['last_key']    = {'gold':None,'btc':None}
        state['ops']         = []
        state['sl_streak']   = {'gold':0,'btc':0}
        state['ops_today']   = {'gold':0,'btc':0}
    log('🔄 Escáner reiniciado')
    return jsonify({'ok':True})

@app.route('/api/manual', methods=['POST'])
def api_manual():
    data = request.get_json()
    asset = data.get('asset','gold')
    direction = data.get('direction','buy')
    try: px = float(data.get('px',0))
    except: px = 0
    if px <= 0:
        px = get_spot(asset)
        if not px:
            with lock: p = state['price'][asset]
            try: px = float(p.replace(',',''))
            except: return jsonify({'ok':False,'error':'Sin precio disponible'})
    send_signal(asset, direction, px, sc=99, manual=True)
    return jsonify({'ok':True})

@app.route('/señal/<asset>/<direction>')
def senal_rapida(asset, direction):
    if asset not in ASSETS: return 'Activo inválido',400
    if direction not in ['buy','sell']: return 'Dirección inválida',400
    px = get_spot(asset)
    if not px:
        with lock: p = state['price'][asset]
        try: px = float(p.replace(',',''))
        except: return 'Sin precio',400
    send_signal(asset, direction, px, sc=99, manual=True)
    return f'✅ {direction.upper()} {ASSETS[asset]["label"]} {fmt(px,asset)} → Telegram', 200

# ── Loop principal ────────────────────────────────
def loop():
    log('SNIPER SCANNER v18.5 — Score mín:80 | Filtros: Tendencia+Sweep+CHoCH obligatorios')
    with lock: state['status'] = 'ACTIVO'
    cycle = 0
    while True:
        try:
            check_ops()
            if cycle % 2 == 0:
                if datetime.now().weekday() < 5:
                    scan('gold')
                else:
                    log('Fin de semana — ORO cerrado')
                time.sleep(5)
                scan('btc')
            cycle += 1
            time.sleep(10)
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(10)

if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
