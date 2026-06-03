import os, time, requests, threading, json
from datetime import datetime
from flask import Flask, jsonify, render_template_string

# =============================================
# SNIPER SCANNER v3.0
# Escáner + Panel Web en tiempo real
# Filosofía: detectar lo que un trader vería
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
        'min_px': 2000, 'is_btc': False,
    },
    'btc': {
        'icon': '₿', 'label': 'BITCOIN',
        'tp': 250, 'sl': 125,
        'sym12': 'BTC/USD', 'symB': 'BTCUSDT',
        'min_px': 10000, 'is_btc': True,
    },
}

LOTS      = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]
SCORE_MIN = 48   # más ligero — detectar como trader discrecional
COOLDOWN  = 12 * 60  # 12 minutos entre señales del mismo activo

# ── Estado compartido ───────────────────────
state = {
    'status':      'INICIANDO',
    'last_scan':   {'gold': '—', 'btc': '—'},
    'last_signal': None,
    'price':       {'gold': '—', 'btc': '—'},
    'tp_count':    0,
    'sl_count':    0,
    'sig_count':   0,
    'logs':        [],   # últimas 60 líneas
    'active_ops':  [],
    'last_alert':  {'gold': 0, 'btc': 0},
    'last_key':    {'gold': None, 'btc': None},
    'op_n':        0,
}
lock = threading.Lock()

# =============================================
# LOG — visible en panel y consola
# =============================================

def log(msg):
    ts  = datetime.now().strftime('%H:%M:%S')
    line = f'{ts}  {msg}'
    print(line, flush=True)
    with lock:
        state['logs'].append(line)
        if len(state['logs']) > 60:
            state['logs'].pop(0)

# =============================================
# TELEGRAM
# =============================================

def send(msg):
    for chat in [TG_CHAT_ID, TG_GROUP_ID]:
        try:
            requests.post(TG_API,
                json={'chat_id': chat, 'text': msg},
                timeout=10)
        except:
            pass

def fmt(v, is_btc):
    return f'{round(v):,}' if is_btc else f'{v:.2f}'

def gain_eur(tp_pts, lot):
    return int(tp_pts * lot * 100)

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
        if len(vals) < 20: return None
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
        if not k or len(k) < 20: return None
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

def rsi(c, n=14):
    if len(c) < n+1: return 50
    g = l = 0
    for i in range(len(c)-n, len(c)):
        d = c[i]-c[i-1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g/n, l/n
    return round(100-100/(1+ag/al), 1) if al else 100

def ema(c, n):
    if len(c) < n: return c[-1]
    k = 2/(n+1); e = c[0]
    for p in c[1:]: e = p*k+e*(1-k)
    return e

def atr(h, l, c, n=14):
    if len(c) < n+1: return 1
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-n:]) / n

# =============================================
# DETECCIÓN DE ESTRUCTURA
# =============================================

def detect_sweep(c, h, l):
    if len(c) < 8: return None
    ph = max(h[-8:-1]); pl = min(l[-8:-1])
    rng = h[-1]-l[-1]
    if rng == 0: return None
    if l[-1] < pl and c[-1] > pl:
        if (min(c[-2],c[-1])-l[-1])/rng >= 0.28: return 'buy'
    if h[-1] > ph and c[-1] < ph:
        if (h[-1]-max(c[-2],c[-1]))/rng >= 0.28: return 'sell'
    return None

def detect_choch(c, lb=12):
    if len(c) < lb+3: return None
    seg = c[-(lb+3):]
    highs = [i for i in range(1,len(seg)-1) if seg[i]>seg[i-1] and seg[i]>seg[i+1]]
    lows  = [i for i in range(1,len(seg)-1) if seg[i]<seg[i-1] and seg[i]<seg[i+1]]
    if not highs or not lows: return None
    lh = seg[max(highs)]; ll = seg[min(lows)]
    if c[-1] > lh: return 'buy'
    if c[-1] < ll: return 'sell'
    return None

def detect_bos(c, h, l, lb=15):
    if len(c) < lb: return None
    ph = max(h[-lb:-2]); pl = min(l[-lb:-2])
    if c[-1]>ph and c[-2]>ph: return 'buy'
    if c[-1]<pl and c[-2]<pl: return 'sell'
    return None

def detect_consolidation_break(c, h, l, n=8):
    """Detecta rango estrecho seguido de ruptura — setup clásico."""
    if len(c) < n+3: return None
    rng_h = max(h[-n-1:-1]); rng_l = min(l[-n-1:-1])
    rng   = rng_h - rng_l
    avg_rng = sum(h[i]-l[i] for i in range(-n-1,-1)) / n
    if avg_rng == 0: return None
    # Rango estrecho = consolidación
    if rng < avg_rng * 1.5:
        if c[-1] > rng_h: return 'buy'
        if c[-1] < rng_l: return 'sell'
    return None

# =============================================
# SCORE — calibrado para detectar como trader
# =============================================

def score_op(d5, d1h=None):
    c=d5['c']; h=d5['h']; l=d5['l']; v=d5['v']
    px  = c[-1]
    r   = rsi(c)
    e9  = ema(c, 9)
    e21 = ema(c, 21)
    e50 = ema(c, min(50,len(c)))
    at  = atr(h, l, c)

    buy = sell = 0

    # 1. EMA tendencia (15pts)
    if px > e21: buy  += 10
    else:         sell += 10
    if e21 > e50: buy  += 5
    else:          sell += 5
    if px > e9:   buy  += 3
    else:          sell += 3

    # 2. RSI — zonas favorables (20pts)
    # Más generoso: no exige extremos
    if   r < 32: buy  += 20
    elif r < 42: buy  += 14
    elif r < 50: buy  +=  7
    elif r > 68: sell += 20
    elif r > 58: sell += 14
    elif r > 50: sell +=  7

    # 3. Momentum velas (18pts)
    if len(c) >= 4:
        up3 = all(c[i]>c[i-1] for i in range(-3,0))
        dn3 = all(c[i]<c[i-1] for i in range(-3,0))
        if up3: buy  += 18
        if dn3: sell += 18
    # También 2 velas seguidas (menos exigente)
    if len(c) >= 3:
        if c[-1]>c[-2]>c[-3]: buy  += 8
        if c[-1]<c[-2]<c[-3]: sell += 8

    # 4. Impulso (12pts) — bajado de 2.5x a 1.8x
    if len(c) >= 6:
        move = abs(c[-1]-c[-5])
        avg  = sum(abs(c[i]-c[i-1]) for i in range(-5,0))/5
        if avg > 0 and move > avg*1.8:
            if c[-1]>c[-5]: buy  += 12
            else:            sell += 12

    # 5. Sweep liquidez (20pts) — institucional
    sweep = detect_sweep(c, h, l)
    if sweep=='buy':  buy  += 20
    if sweep=='sell': sell += 20

    # 6. CHoCH (12pts)
    choch = detect_choch(c)
    if choch=='buy':  buy  += 12
    if choch=='sell': sell += 12

    # 7. BOS (10pts)
    bos = detect_bos(c, h, l)
    if bos=='buy':  buy  += 10
    if bos=='sell': sell += 10

    # 8. Ruptura de consolidación (10pts)
    cb = detect_consolidation_break(c, h, l)
    if cb=='buy':  buy  += 10
    if cb=='sell': sell += 10

    # 9. Soporte/Resistencia (10pts)
    if len(c) >= 20:
        mn=min(l[-20:]); mx=max(h[-20:])
        rng=mx-mn
        if rng>0:
            pos=(px-mn)/rng
            if pos<0.22: buy  += 10
            if pos>0.78: sell += 10

    # 10. Volumen (8pts)
    if v and len(v)>=6:
        avg_v=sum(v[-7:-1])/6
        if avg_v>0 and v[-1]>avg_v*1.25:
            if buy>sell: buy  += 8
            else:        sell += 8

    # 11. H1 contexto (10pts)
    if d1h and len(d1h['c'])>=21:
        c1=d1h['c']; e21h=ema(c1,21); r1h=rsi(c1)
        if c1[-1]>e21h: buy  += 6
        else:            sell += 6
        if r1h<45:       buy  += 4
        if r1h>55:       sell += 4

    # 12. Vela de confirmación (5pts)
    if c[-1]>c[-2] and buy>sell:  buy  += 5
    if c[-1]<c[-2] and sell>buy:  sell += 5

    # ATR — solo penalizar explosiones
    if at>0 and abs(c[-1]-c[-2])>at*5:
        buy  = int(buy *0.75)
        sell = int(sell*0.75)

    buy  = min(buy, 100)
    sell = min(sell,100)

    log(f'  📊 buy:{buy} sell:{sell} | RSI:{r:.0f} sweep:{sweep} choch:{choch} bos:{bos}')

    # Ventaja mínima 8pts sobre el contrario (antes era 10)
    if buy  >= SCORE_MIN and buy  > sell+8: return 'buy',  buy
    if sell >= SCORE_MIN and sell > buy +8: return 'sell', sell
    return None, 0

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
            to_rm.append(op); log(f'{cfg["icon"]} Op #{op["n"]} timeout'); continue

        p = fetch_spot(op['asset'])
        if not p: continue

        hit_tp = (op['dir']=='buy'  and p >= op['tp']) or (op['dir']=='sell' and p <= op['tp'])
        hit_sl = (op['dir']=='buy'  and p <= op['sl']) or (op['dir']=='sell' and p >= op['sl'])

        if hit_tp or hit_sl:
            key = 'tp' if hit_tp else 'sl'
            send(f'{"✅ TP" if hit_tp else "❌ SL"} #{op["n"]}\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {datetime.now().strftime("%H:%M")}')
            log(f'{"✅ TP" if hit_tp else "❌ SL"} #{op["n"]} {cfg["label"]}')
            with lock:
                if hit_tp: state['tp_count'] += 1
                else:      state['sl_count'] += 1
            to_rm.append(op)

    with lock:
        for op in to_rm:
            if op in state['active_ops']:
                state['active_ops'].remove(op)

# =============================================
# ENVIAR OPORTUNIDAD
# =============================================

def send_op(asset, direction, px):
    cfg   = ASSETS[asset]
    isBuy = direction == 'buy'
    tp    = px+cfg['tp'] if isBuy else px-cfg['tp']
    sl    = px-cfg['sl'] if isBuy else px+cfg['sl']
    tipo  = 'BUY' if isBuy else 'SELL'

    with lock:
        state['op_n'] += 1
        n = state['op_n']
        state['sig_count'] += 1

    gains = '\n'.join([f'{l:.2f} → +{gain_eur(cfg["tp"],l)}€' for l in LOTS])
    now   = datetime.now().strftime('%H:%M')

    msg = (f'{cfg["icon"]} {cfg["label"]}\n\n'
           f'{tipo}\n\n'
           f'Entrada: {fmt(px,cfg["is_btc"])}\n'
           f'TP: {fmt(tp,cfg["is_btc"])}\n'
           f'SL: {fmt(sl,cfg["is_btc"])}\n\n'
           f'Ganancia estimada:\n{gains}\n\n'
           f'Hora: {now}')
    send(msg)
    log(f'📨 SEÑAL #{n} {tipo} {cfg["label"]} {fmt(px,cfg["is_btc"])}')

    sig_info = {
        'n': n, 'asset': asset, 'dir': direction,
        'px': fmt(px,cfg["is_btc"]),
        'tp': fmt(tp,cfg["is_btc"]),
        'sl': fmt(sl,cfg["is_btc"]),
        'time': now, 'icon': cfg['icon'], 'label': cfg['label'],
    }
    with lock:
        state['last_signal'] = sig_info
        state['last_alert'][asset] = time.time()
        state['last_key'][asset]   = f'{direction}-{round(px/(cfg["tp"]*3))}'
        state['active_ops'].append({
            'asset': asset, 'dir': direction,
            'tp': tp, 'sl': sl,
            't': time.time(), 'n': n,
        })

# =============================================
# ESCANEAR
# =============================================

def scan(asset):
    cfg = ASSETS[asset]
    now = time.time()

    with lock:
        last = state['last_alert'][asset]

    if now - last < COOLDOWN:
        log(f'{cfg["icon"]} Cooldown {((COOLDOWN-(now-last))/60):.0f}min')
        return

    log(f'{cfg["icon"]} Escaneando {cfg["label"]}...')
    d5  = fetch(asset,'5min')
    if not d5: log(f'{cfg["icon"]} Sin datos M5'); return

    d1h = fetch(asset,'1h')
    px  = d5['c'][-1]

    with lock:
        state['price'][asset] = fmt(px, cfg['is_btc'])
        state['last_scan'][asset] = datetime.now().strftime('%H:%M:%S')

    direction, score = score_op(d5, d1h)

    with lock:
        key = state['last_key'][asset]

    if not direction:
        log(f'{cfg["icon"]} Sin oportunidad')
        return

    new_key = f'{direction}-{round(px/(cfg["tp"]*3))}'
    if new_key == key:
        log(f'{cfg["icon"]} Misma zona — skip')
        return

    send_op(asset, direction, px)

# =============================================
# PANEL WEB
# =============================================

HTML = '''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper Scanner</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060a0f;color:#b8ccd8;font-family:'Courier New',monospace;font-size:14px}
.hdr{background:#0b1219;border-bottom:1px solid #162030;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{color:#ffc832;font-size:1rem;font-weight:700;letter-spacing:3px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.g{background:#00e87a;box-shadow:0 0 8px #00e87a}
.r{background:#ff2d55}.y{background:#ffc832}
.page{max-width:860px;margin:0 auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#0b1219;border:1px solid #162030;border-radius:8px;padding:14px}
.label{font-size:.55rem;letter-spacing:3px;color:#3a5a72;text-transform:uppercase;margin-bottom:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.stat{background:#080e14;border-radius:6px;padding:10px;text-align:center;border:1px solid #162030}
.sv{font-size:1.5rem;font-weight:700;color:#ffc832}
.sl2{font-size:.55rem;color:#3a5a72;margin-top:3px}
.sig{background:#080e14;border-radius:6px;padding:12px;border-left:3px solid #ffc832}
.sig.buy{border-left-color:#00e87a}.sig.sell{border-left-color:#ff5fa0}
.sig-t{font-size:1rem;font-weight:700;letter-spacing:2px}
.buy-c{color:#00e87a}.sell-c{color:#ff5fa0}.gold-c{color:#ffc832}
.logs{background:#040810;border-radius:6px;padding:10px;height:280px;overflow-y:auto;font-size:.72rem;line-height:1.7}
.logs p{border-bottom:1px solid #0e1822;padding:2px 0}
.logs p:last-child{color:#00e87a}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:9px 16px;background:transparent;border:1px solid #ffc83255;border-radius:5px;color:#ffc832;font-family:inherit;font-size:.75rem;letter-spacing:1px;cursor:pointer;transition:all .2s}
.btn:hover{background:#ffc83215}
.btn.blue{border-color:#0af5;color:#0af}.btn.blue:hover{background:#0af1}
.btn.red{border-color:#ff2d5555;color:#ff2d55}.btn.red:hover{background:#ff2d5510}
.px-big{font-size:1.6rem;font-weight:700}
@media(max-width:500px){.grid4{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">SNIPER SCANNER v3</div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span><span class="dot g" id="sDot"></span><span id="sText">—</span></span>
    <span style="color:#3a5a72;font-size:.7rem" id="clock">--:--:--</span>
  </div>
</div>

<div class="page">

  <div class="grid2">
    <div class="card">
      <div class="label">🥇 ORO precio</div>
      <div class="px-big gold-c" id="pxGold">—</div>
      <div style="font-size:.65rem;color:#3a5a72;margin-top:4px" id="scanGold">Último scan: —</div>
    </div>
    <div class="card">
      <div class="label">₿ BTC precio</div>
      <div class="px-big" style="color:#f7931a" id="pxBTC">—</div>
      <div style="font-size:.65rem;color:#3a5a72;margin-top:4px" id="scanBTC">Último scan: —</div>
    </div>
  </div>

  <div class="grid4">
    <div class="stat"><div class="sv" id="stSig">0</div><div class="sl2">SEÑALES</div></div>
    <div class="stat"><div class="sv" style="color:#00e87a" id="stTP">0</div><div class="sl2">TP</div></div>
    <div class="stat"><div class="sv" style="color:#ff2d55" id="stSL">0</div><div class="sl2">SL</div></div>
    <div class="stat"><div class="sv" style="color:#0af" id="stEfect">—</div><div class="sl2">EFECTIV.</div></div>
  </div>

  <div class="card">
    <div class="label">Última señal</div>
    <div id="lastSig" style="color:#3a5a72;font-size:.8rem">Sin señales aún</div>
  </div>

  <div class="card">
    <div class="label">Registro en tiempo real</div>
    <div class="logs" id="logs"></div>
  </div>

  <div class="btns">
    <button class="btn" onclick="forceUpdate()">🔄 Forzar análisis</button>
    <button class="btn blue" onclick="restart()">⚡ Reiniciar escáner</button>
  </div>

</div>

<script>
setInterval(()=>document.getElementById('clock').textContent=new Date().toLocaleTimeString('es-ES'),1000);

async function poll(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());
    document.getElementById('sDot').className='dot '+(d.status==='ACTIVO'?'g':'y');
    document.getElementById('sText').textContent=d.status;
    document.getElementById('pxGold').textContent=d.price.gold;
    document.getElementById('pxBTC').textContent=d.price.btc;
    document.getElementById('scanGold').textContent='Último scan: '+d.last_scan.gold;
    document.getElementById('scanBTC').textContent='Último scan: '+d.last_scan.btc;
    document.getElementById('stSig').textContent=d.sig_count;
    document.getElementById('stTP').textContent=d.tp_count;
    document.getElementById('stSL').textContent=d.sl_count;
    const tot=d.tp_count+d.sl_count;
    document.getElementById('stEfect').textContent=tot>0?Math.round(d.tp_count/tot*100)+'%':'—';
    const ls=d.last_signal;
    if(ls){
      const isBuy=ls.dir==='buy';
      document.getElementById('lastSig').innerHTML=
        `<div class="sig ${ls.dir}">
          <div class="sig-t ${isBuy?'buy-c':'sell-c'}">${ls.icon} ${ls.label} — ${isBuy?'BUY':'SELL'} #${ls.n}</div>
          <div style="margin-top:6px;font-size:.75rem;color:#b8ccd8">
            Entrada: <b>${ls.px}</b> · TP: <b>${ls.tp}</b> · SL: <b>${ls.sl}</b>
          </div>
          <div style="font-size:.65rem;color:#3a5a72;margin-top:3px">${ls.time}</div>
        </div>`;
    }
    const logsEl=document.getElementById('logs');
    logsEl.innerHTML=d.logs.slice().reverse().map(l=>`<p>${l}</p>`).join('');
  }catch(e){}
}

async function forceUpdate(){
  await fetch('/api/force',{method:'POST'});
  setTimeout(poll,1500);
}
async function restart(){
  if(confirm('¿Reiniciar el escáner?')){
    await fetch('/api/restart',{method:'POST'});
    setTimeout(poll,1500);
  }
}

poll();
setInterval(poll,3000);
</script>
</body>
</html>'''

# =============================================
# FLASK APP
# =============================================

app = Flask(__name__)

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/state')
def api_state():
    with lock:
        s = dict(state)
        s['active_ops'] = len(state['active_ops'])
    return jsonify(s)

@app.route('/api/force', methods=['POST'])
def api_force():
    with lock:
        state['last_alert']['gold'] = 0
        state['last_alert']['btc']  = 0
        state['last_key']['gold']   = None
        state['last_key']['btc']    = None
    log('⚡ Análisis forzado por panel web')
    return jsonify({'ok': True})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    with lock:
        state['last_alert'] = {'gold': 0, 'btc': 0}
        state['last_key']   = {'gold': None, 'btc': None}
        state['active_ops'] = []
    log('🔄 Escáner reiniciado por panel web')
    return jsonify({'ok': True})

# =============================================
# LOOP PRINCIPAL
# =============================================

def scanner_loop():
    log('SNIPER SCANNER v3.0 iniciando...')
    with lock:
        state['status'] = 'ACTIVO'

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

# =============================================
# ARRANQUE
# =============================================

if __name__ == '__main__':
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
