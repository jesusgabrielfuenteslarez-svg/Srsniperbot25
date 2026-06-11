import os, time, requests, threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string

# ================================================
# SNIPER SCANNER v5.0
# Escáner ORO y BTC — señales de alta probabilidad
# Panel web en tiempo real
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
SCORE_MIN = 60

state = {
    'status':'INICIANDO','price':{'gold':'—','btc':'—'},
    'last_scan':{'gold':'—','btc':'—'},'last_signal':None,
    'tp':0,'sl':0,'sigs':0,'logs':[],'ops':[],'op_n':0,
    'history':[],'last_alert':{'gold':0,'btc':0},'last_key':{'gold':None,'btc':None},
}
lock = threading.Lock()

# ── Utils ────────────────────────────────────────
def now_str(): return datetime.now().strftime('%H:%M')
def log(m):
    line = f'{datetime.now().strftime("%H:%M:%S")}  {m}'
    print(line, flush=True)
    with lock:
        state['logs'].append(line)
        if len(state['logs'])>80: state['logs'].pop(0)

def send(msg):
    for c in [TG_CHAT_ID, TG_GROUP_ID]:
        try: requests.post(TG_API, json={'chat_id':c,'text':msg}, timeout=10)
        except: pass

def fmt(v,a): return f'{round(v):,}' if a=='btc' else f'{v:.2f}'
def gain(tp,lot): return round(tp*lot*10*EUR_RATE,2)

# ── Datos ────────────────────────────────────────
def candles(sym, iv='5min', n=60):
    try:
        r = requests.get('https://api.twelvedata.com/time_series',
            params={'symbol':sym,'interval':iv,'outputsize':n,'apikey':TWELVE_KEY},timeout=12).json()
        if r.get('status')=='error': return None
        v = r.get('values',[])
        if len(v)<20: return None
        return {'c':[float(x['close']) for x in reversed(v)],
                'h':[float(x['high'])  for x in reversed(v)],
                'l':[float(x['low'])   for x in reversed(v)],
                'v':[float(x.get('volume',0)) for x in reversed(v)]}
    except: return None

def candles_b(sym, iv='5m', n=60):
    try:
        k = requests.get(f'https://api.binance.com/api/v3/klines?symbol={sym}&interval={iv}&limit={n}',timeout=8).json()
        if len(k)<20: return None
        return {'c':[float(x[4]) for x in k],'h':[float(x[2]) for x in k],
                'l':[float(x[3]) for x in k],'v':[float(x[5]) for x in k]}
    except: return None

def get_data(asset, tf='5min'):
    cfg = ASSETS[asset]
    d = candles(cfg['sym12'], tf)
    if d and d['c'][-1]>cfg['min']: return d
    return candles_b(cfg['symB'], {'5min':'5m','15min':'15m','1h':'1h'}.get(tf,'5m'))

def get_spot(asset):
    cfg = ASSETS[asset]
    try:
        p = float(requests.get('https://api.twelvedata.com/price',
            params={'symbol':cfg['sym12'],'apikey':TWELVE_KEY},timeout=6).json().get('price',0))
        if p>cfg['min']: return p
    except: pass
    try:
        t = requests.get(f'https://api.binance.com/api/v3/ticker/bookTicker?symbol={cfg["symB"]}',timeout=5).json()
        if 'bidPrice' in t: return (float(t['bidPrice'])+float(t['askPrice']))/2
    except: pass
    return None

# ── Indicadores ──────────────────────────────────
def ema(c,n):
    if len(c)<n: return c[-1]
    k=2/(n+1); e=c[0]
    for p in c[1:]: e=p*k+e*(1-k)
    return e

def rsi(c,n=14):
    if len(c)<n+1: return 50
    g=l=0
    for i in range(len(c)-n,len(c)):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al=g/n,l/n
    return round(100-100/(1+ag/al),1) if al else 100

def atr(h,l,c,n=14):
    if len(c)<n+1: return 1
    t=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(1,len(c))]
    return sum(t[-n:])/n

# ── Score ────────────────────────────────────────
def score(d5):
    c,h,l,v = d5['c'],d5['h'],d5['l'],d5['v']
    px=c[-1]; r=rsi(c); e20=ema(c,20); e50=ema(c,min(50,len(c))); at=atr(h,l,c)
    b=s=0

    # Tendencia EMA (20pts)
    if px>e20: b+=12
    else: s+=12
    if e20>e50: b+=8
    else: s+=8

    # RSI (25pts)
    if r<30: b+=25
    elif r<40: b+=18
    elif r<48: b+=10
    elif r>70: s+=25
    elif r>60: s+=18
    elif r>52: s+=10

    # Momentum 3 velas (20pts)
    if len(c)>=4:
        if all(c[i]>c[i-1] for i in range(-3,0)): b+=20
        if all(c[i]<c[i-1] for i in range(-3,0)): s+=20
    # 2 velas (10pts extra)
    if len(c)>=3:
        if c[-1]>c[-2]>c[-3]: b+=10
        if c[-1]<c[-2]<c[-3]: s+=10

    # Sweep de liquidez (25pts) — el más importante
    if len(c)>=8:
        for lb in [5,10,20]:
            if len(h)<lb+1: continue
            ph=max(h[-lb:-1]); pl=min(l[-lb:-1]); rng=h[-1]-l[-1]
            if rng==0: continue
            if l[-1]<pl and c[-1]>pl:
                w=(min(c[-2],c[-1])-l[-1])/rng
                if w>=0.25: b+=25 if w>=0.5 else 18; break
            if h[-1]>ph and c[-1]<ph:
                w=(h[-1]-max(c[-2],c[-1]))/rng
                if w>=0.25: s+=25 if w>=0.5 else 18; break

    # Posición en rango (10pts)
    if len(c)>=20:
        mn=min(l[-20:]); mx=max(h[-20:]); rng=mx-mn
        if rng>0:
            pos=(px-mn)/rng
            if pos<0.20: b+=10
            if pos>0.80: s+=10

    # Volumen (10pts)
    if v and len(v)>=6:
        avg=sum(v[-7:-1])/6
        if avg>0 and v[-1]>avg*1.2:
            if b>s: b+=10
            else: s+=10

    # Impulso (10pts)
    if len(c)>=6:
        mv=abs(c[-1]-c[-5]); avg=sum(abs(c[i]-c[i-1]) for i in range(-5,0))/5
        if avg>0 and mv>avg*1.8:
            if c[-1]>c[-5]: b+=10
            else: s+=10

    # Vela de confirmación (5pts)
    if c[-1]>c[-2] and b>s: b+=5
    if c[-1]<c[-2] and s>b: s+=5

    # Penalizar volatilidad extrema
    if at>0 and abs(c[-1]-c[-2])>at*5:
        b=int(b*0.7); s=int(s*0.7)

    b=min(b,100); s=min(s,100)
    log(f'  Score BUY:{b} SELL:{s} RSI:{r:.0f}')

    if b>=SCORE_MIN and b>s+8: return 'buy',b
    if s>=SCORE_MIN and s>b+8: return 'sell',s
    return None,0

# ── Seguimiento ──────────────────────────────────
def check_ops():
    to_rm=[]; now=time.time()
    with lock: ops=list(state['ops'])
    for op in ops:
        cfg=ASSETS[op['a']]
        if now-op['t']>7200: to_rm.append(op); continue
        p=get_spot(op['a'])
        if not p: continue
        hit_tp=(op['d']=='buy' and p>=op['tp']) or (op['d']=='sell' and p<=op['tp'])
        hit_sl=(op['d']=='buy' and p<=op['sl']) or (op['d']=='sell' and p>=op['sl'])
        if hit_tp or hit_sl:
            res='tp' if hit_tp else 'sl'
            send(f'{"✅ TP" if hit_tp else "❌ SL"}\n\n{cfg["icon"]} {cfg["label"]}\n\nHora: {now_str()}')
            log(f'{"✅ TP" if hit_tp else "❌ SL"} #{op["n"]} {cfg["label"]}')
            with lock:
                state['tp' if hit_tp else 'sl']+=1
                state['history'].append({'n':op['n'],'a':op['a'],'d':op['d'],'r':res,'s':op['s'],'h':now_str()})
                if len(state['history'])>50: state['history'].pop(0)
            to_rm.append(op)
    with lock:
        for op in to_rm:
            if op in state['ops']: state['ops'].remove(op)

# ── Enviar señal ─────────────────────────────────
def send_signal(asset, direction, px, sc, manual=False):
    cfg=ASSETS[asset]; isBuy=direction=='buy'

    # Zona de entrada — spread de 4 puntos ORO, 100 puntos BTC
    spread = 4 if asset=='gold' else 100
    if isBuy:
        zona_low  = px
        zona_high = px + spread
    else:
        zona_low  = px - spread
        zona_high = px

    # SL
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    # 3 TPs progresivos
    if isBuy:
        tp1 = px + cfg['tp']
        tp2 = px + cfg['tp'] * 2
        tp3 = px + cfg['tp'] * 4
    else:
        tp1 = px - cfg['tp']
        tp2 = px - cfg['tp'] * 2
        tp3 = px - cfg['tp'] * 4

    tipo = 'BUY' if isBuy else 'SELL'

    with lock:
        state['op_n']+=1; state['sigs']+=1; n=state['op_n']

    msg = (
        f'{cfg["icon"]} {cfg["label"]}\n\n'
        f'{tipo}\n\n'
        f'Confianza: {sc}%\n\n'
        f'Zona entrada: {fmt(zona_low,asset)} - {fmt(zona_high,asset)}\n'
        f'SL: {fmt(sl,asset)}\n\n'
        f'TP1: {fmt(tp1,asset)}\n'
        f'TP2: {fmt(tp2,asset)}\n'
        f'TP3: {fmt(tp3,asset)}\n\n'
        f'Hora: {now_str()}'
    )
    send(msg)
    log(f'{"📲 MANUAL" if manual else "📨"} #{n} {tipo} {cfg["label"]} {fmt(px,asset)} {sc}%')

    with lock:
        state['last_alert'][asset]=time.time()
        state['last_key'][asset]=f'{direction}-{round(px/(cfg["tp"]*3))}'
        state['last_signal']={
            'n':n,'a':asset,'d':direction,
            'px':f'{fmt(zona_low,asset)}-{fmt(zona_high,asset)}',
            'tp':fmt(tp1,asset),'sl':fmt(sl,asset),
            'sc':sc,'h':now_str(),'icon':cfg['icon'],'label':cfg['label']
        }
        state['ops'].append({
            'a':asset,'d':direction,
            'tp':tp1,'sl':sl,   # seguimiento por TP1
            't':time.time(),'n':n,'s':sc
        })

# ── Escanear ─────────────────────────────────────
def scan(asset):
    cfg=ASSETS[asset]
    log(f'{cfg["icon"]} Escaneando {cfg["label"]}...')
    d5=get_data(asset,'5min')
    if not d5: log(f'{cfg["icon"]} Sin datos'); return
    px=d5['c'][-1]
    with lock:
        state['price'][asset]=fmt(px,asset)
        state['last_scan'][asset]=datetime.now().strftime('%H:%M:%S')
    direction,sc=score(d5)
    if not direction: return
    key=f'{direction}-{round(px/(cfg["tp"]*3))}'
    with lock:
        if key==state['last_key'][asset]: log(f'{cfg["icon"]} Misma zona'); return
    send_signal(asset,direction,px,sc)

# ── Panel web ────────────────────────────────────
HTML='''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper v5</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060a0f;color:#b8ccd8;font-family:'Courier New',monospace;font-size:13px}
.hdr{background:#0b1219;border-bottom:1px solid #162030;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{color:#ffc832;font-size:.95rem;font-weight:700;letter-spacing:3px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.g{background:#00e87a;box-shadow:0 0 8px #00e87a}.r{background:#ff2d55}.y{background:#ffc832}
.page{max-width:900px;margin:0 auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#0b1219;border:1px solid #162030;border-radius:8px;padding:14px}
.lbl{font-size:.5rem;letter-spacing:3px;color:#3a5a72;text-transform:uppercase;margin-bottom:8px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.stat{background:#080e14;border-radius:6px;padding:10px;text-align:center;border:1px solid #162030}
.sv{font-size:1.4rem;font-weight:700;color:#ffc832}
.sl2{font-size:.5rem;color:#3a5a72;margin-top:3px}
.px{font-size:1.5rem;font-weight:700}
.logs{background:#040810;border-radius:6px;padding:10px;height:260px;overflow-y:auto;font-size:.68rem;line-height:1.8}
.logs p{border-bottom:1px solid #0a1520;padding:1px 0}
.btn{padding:9px 16px;background:transparent;border:1px solid #ffc83255;border-radius:5px;color:#ffc832;font-family:inherit;font-size:.72rem;letter-spacing:1px;cursor:pointer;transition:all .2s}
.btn:hover{background:#ffc83215}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.buy{color:#00e87a}.sell{color:#ff5fa0}.gold{color:#ffc832}
sel,input{width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#b8ccd8;font-family:inherit;font-size:.8rem;margin-bottom:8px;outline:none}
@media(max-width:500px){.g4{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">⚡ SNIPER v5</div>
  <div style="display:flex;align-items:center;gap:12px">
    <span><span class="dot g" id="sDot"></span><span id="sText" style="font-size:.7rem">—</span></span>
    <span style="color:#3a5a72;font-size:.65rem" id="clk">--:--:--</span>
  </div>
</div>
<div class="page">

  <div class="g2">
    <div class="card"><div class="lbl">🥇 ORO XAU/USD</div><div class="px gold" id="pxG">—</div><div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scG">—</div></div>
    <div class="card"><div class="lbl">₿ BITCOIN</div><div class="px" style="color:#f7931a" id="pxB">—</div><div style="font-size:.6rem;color:#3a5a72;margin-top:4px" id="scB">—</div></div>
  </div>

  <div class="g4">
    <div class="stat"><div class="sv" id="stS">0</div><div class="sl2">SEÑALES</div></div>
    <div class="stat"><div class="sv buy" id="stT">0</div><div class="sl2">✅ TP</div></div>
    <div class="stat"><div class="sv sell" id="stL">0</div><div class="sl2">❌ SL</div></div>
    <div class="stat"><div class="sv gold" id="stE">—</div><div class="sl2">EFECTIVIDAD</div></div>
  </div>

  <div class="card">
    <div class="lbl">Última señal</div>
    <div id="lastSig" style="color:#3a5a72;font-size:.75rem">Sin señales aún</div>
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
    <div class="g2" style="margin-bottom:4px">
      <div>
        <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">ACTIVO</div>
        <select id="mA" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem;margin-bottom:8px">
          <option value="gold">🥇 ORO</option><option value="btc">₿ BITCOIN</option>
        </select>
      </div>
      <div>
        <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">DIRECCIÓN</div>
        <select id="mD" style="width:100%;background:#080e14;border:1px solid #162030;border-radius:5px;padding:8px;color:#ffc832;font-family:inherit;font-size:.8rem;margin-bottom:8px">
          <option value="buy">💚 BUY</option><option value="sell">🩷 SELL</option>
        </select>
      </div>
    </div>
    <div style="font-size:.55rem;color:#3a5a72;margin-bottom:4px">PRECIO (vacío = precio actual)</div>
    <input id="mP" type="number" step="0.01" placeholder="Ej: 3312.50">
    <button class="btn" style="width:100%" onclick="sendManual()">📨 ENVIAR A TELEGRAM</button>
    <div id="mR" style="margin-top:6px;font-size:.65rem;text-align:center"></div>
  </div>

  <div class="card">
    <div class="lbl">🧠 Historial de operaciones</div>
    <div id="hist" style="font-size:.68rem;max-height:200px;overflow-y:auto"></div>
  </div>

  <div class="card">
    <div class="lbl">📊 ORO — XAU/USD M5 EN VIVO</div>
    <iframe src="https://s.tradingview.com/widgetembed/?frameElementId=tv1&symbol=OANDA%3AXAUUSD&interval=5&theme=dark&style=1&locale=es&toolbar_bg=%230b1219&hide_top_toolbar=0&studies=RSI%4014&withdateranges=0&hideideas=1" style="width:100%;height:300px;border:none;border-radius:6px" allowtransparency="true" scrolling="no"></iframe>
  </div>

  <div class="card">
    <div class="lbl">📊 BITCOIN — M5 EN VIVO</div>
    <iframe src="https://s.tradingview.com/widgetembed/?frameElementId=tv2&symbol=BINANCE%3ABTCUSDT&interval=5&theme=dark&style=1&locale=es&toolbar_bg=%230b1219&hide_top_toolbar=0&studies=RSI%4014&withdateranges=0&hideideas=1" style="width:100%;height:300px;border:none;border-radius:6px" allowtransparency="true" scrolling="no"></iframe>
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
    document.getElementById('stS').textContent=d.sigs;
    document.getElementById('stT').textContent=d.tp;
    document.getElementById('stL').textContent=d.sl;
    const tot=d.tp+d.sl;
    const pct=tot>0?Math.round(d.tp/tot*100):null;
    const el=document.getElementById('stE');
    el.textContent=pct!==null?pct+'%':'—';
    el.style.color=pct===null?'#ffc832':pct>=60?'#00e87a':pct>=45?'#ffc832':'#ff2d55';
    const ls=d.last_signal;
    if(ls){
      const b=ls.d==='buy';
      document.getElementById('lastSig').innerHTML=
        `<div style="border-left:3px solid ${b?'#00e87a':'#ff5fa0'};padding:10px;background:#080e14;border-radius:0 6px 6px 0">
          <div style="font-size:.85rem;font-weight:700;color:${b?'#00e87a':'#ff5fa0'}">${ls.icon} ${ls.label} — ${b?'BUY':'SELL'} #${ls.n} <span style="color:#3a5a72;font-size:.6rem">${ls.sc}%</span></div>
          <div style="margin-top:5px;font-size:.75rem">Entrada: <b>${ls.px}</b> · TP: <b style="color:#00e87a">${ls.tp}</b> · SL: <b style="color:#ff2d55">${ls.sl}</b></div>
          <div style="font-size:.6rem;color:#3a5a72;margin-top:2px">${ls.h}</div>
        </div>`;
    }
    const logsEl=document.getElementById('logs');
    logsEl.innerHTML=d.logs.slice().reverse().map(l=>`<p>${l}</p>`).join('');
    const hist=document.getElementById('hist');
    if(d.history.length){
      hist.innerHTML=d.history.slice().reverse().map(h=>{
        const ok=h.r==='tp';
        return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #0a1520">
          <span style="color:${ok?'#00e87a':'#ff2d55'}">${ok?'✅':'❌'} #${h.n}</span>
          <span style="color:#3a5a72">${h.a==='gold'?'🥇':'₿'} ${h.d.toUpperCase()}</span>
          <span style="color:#ffc832">${h.s}%</span>
          <span style="color:#3a5a72">${h.h}</span>
        </div>`;
      }).join('');
    } else {
      hist.innerHTML='<div style="color:#3a5a72;padding:8px">Sin operaciones aún</div>';
    }
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
  const px=pv?parseFloat(pv):0;
  const r=await fetch('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset:a,direction:d,px})}).then(r=>r.json());
  const el=document.getElementById('mR');
  el.innerHTML=r.ok?'<span style="color:#00e87a">✅ Señal enviada</span>':'<span style="color:#ff2d55">❌ '+r.error+'</span>';
  setTimeout(()=>el.innerHTML='',4000);
}
poll(); setInterval(poll,3000);
</script>
</body>
</html>'''

# ── Flask ────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def index(): return render_template_string(HTML)

@app.route('/api/state')
def api_state():
    with lock:
        return jsonify({k:v for k,v in state.items() if k!='ops'})

@app.route('/api/force',methods=['POST'])
def api_force():
    with lock:
        state['last_alert']={'gold':0,'btc':0}
        state['last_key']={'gold':None,'btc':None}
    log('⚡ Análisis forzado')
    return jsonify({'ok':True})

@app.route('/api/restart',methods=['POST'])
def api_restart():
    with lock:
        state['last_alert']={'gold':0,'btc':0}
        state['last_key']={'gold':None,'btc':None}
        state['ops']=[]
    log('🔄 Reiniciado')
    return jsonify({'ok':True})

@app.route('/api/manual',methods=['POST'])
def api_manual():
    from flask import request
    data=request.get_json()
    asset=data.get('asset','gold')
    direction=data.get('direction','buy')
    px=float(data.get('px',0))
    if px<=0:
        p=get_spot(asset)
        if not p: return jsonify({'ok':False,'error':'No hay precio disponible'})
        px=p
    send_signal(asset,direction,px,sc=0,manual=True)
    return jsonify({'ok':True})

# ── Loop ─────────────────────────────────────────
def loop():
    log('SNIPER SCANNER v5.0 — ACTIVO')
    with lock: state['status']='ACTIVO'
    while True:
        try:
            if state['ops']: check_ops()
            if datetime.now().weekday()<5: scan('gold')
            else: log('Fin de semana — ORO cerrado')
            time.sleep(5)
            scan('btc')
            time.sleep(20)
        except Exception as e:
            log(f'Error: {e}')
            time.sleep(20)

if __name__=='__main__':
    threading.Thread(target=loop,daemon=True).start()
    app.run(host='0.0.0.0',port=PORT,debug=False)
