import time
import requests
from datetime import datetime

# =============================================
# BOT
# Objetivo: 5 señales/hora garantizadas
# =============================================

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'
TG_GROUP_ID = '-5123266724'
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
TWELVE_KEY  = '4faa588607814607a01ff11d31e86830'

ASSETS = {
    'gold': {'name': 'XAU/USD', 'icon': '🥇', 'tp': 20,  'sl': 10},
    'btc':  {'name': 'BTC/USD', 'icon': '₿',  'tp': 500, 'sl': 200},
}
LOTAJES = [0.01, 0.02, 0.05, 0.08, 0.10, 0.15]

# Ganancias por lotaje (pip value)
# XAU: 0.01 lot * 20pip = ~2 EUR
# BTC: 0.01 lot * 500pip = ~5 EUR
GAINS = {
    'gold': {lot: round(20  * lot * 10, 1) for lot in LOTAJES},
    'btc':  {lot: round(500 * lot * 0.1, 1) for lot in LOTAJES},
}

MAX_PER_HOUR = 5
MIN_PROB     = 70
COOLDOWN     = 180   # 3 min entre señales del mismo activo

# Estado
hour_count   = {}    # {hora: {asset: count}}
last_sig_time= {'gold': 0, 'btc': 0}
last_sig_key = {'gold': '', 'btc': ''}
active_sigs  = []
stats        = {'gold': {'tp':0,'sl':0}, 'btc': {'tp':0,'sl':0}}
last_h_report= -1
last_d_report= None

def now_str(): return datetime.now().strftime('%H:%M')
def log(m):   print(f'[{now_str()}] {m}')

def send(msg):
    for chat in [TG_CHAT_ID, TG_GROUP_ID]:
        try:
            requests.post(TG_API, json={
                'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'
            }, timeout=10)
        except: pass

# ── PRECIOS ───────────────────────────────────

def get_candles(symbol):
    try:
        r = requests.get('https://api.twelvedata.com/time_series', params={
            'symbol': symbol, 'interval': '1min',
            'outputsize': 30, 'apikey': TWELVE_KEY,
        }, timeout=10)
        data = r.json()
        if data.get('status') == 'error':
            log(f'API error: {data.get("message")}')
            return None
        vals = data.get('values', [])
        if not vals: return None
        closes = [float(v['close']) for v in reversed(vals)]
        log(f'{symbol} OK — precio actual: {closes[-1]:.2f}')
        return closes
    except Exception as e:
        log(f'Error precios {symbol}: {e}')
        return None

def get_binance(sym, minp):
    try:
        r = requests.get(
            f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m&limit=30',
            timeout=8)
        p = [float(k[4]) for k in r.json()]
        return p if p and p[-1] > minp else None
    except: return None

def gold_prices():
    p = get_candles('XAU/USD')
    if p and p[-1] > 2000: return p
    return get_binance('XAUUSDT', 2000)

def btc_prices():
    p = get_candles('BTC/USD')
    if p and p[-1] > 10000: return p
    return get_binance('BTCUSDT', 10000)

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

# ── INDICADORES ───────────────────────────────

def rsi(prices, n=14):
    if len(prices) < n+1: return 50
    g = l = 0
    for i in range(len(prices)-n, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l -= d
    ag, al = g/n, l/n
    return round(100 - 100/(1 + ag/al), 1) if al else 100

def ma(prices, n):
    p = prices[-n:] if len(prices) >= n else prices
    return sum(p)/len(p)

def prob(prices, direction):
    score = 40
    r = rsi(prices)
    ma5, ma20 = ma(prices,5), ma(prices,20)
    px = prices[-1]

    # RSI
    if direction == 'buy':
        if r < 30:   score += 22
        elif r < 40: score += 14
        elif r < 50: score += 7
        elif r > 65: score -= 10
    else:
        if r > 70:   score += 22
        elif r > 60: score += 14
        elif r > 50: score += 7
        elif r < 35: score -= 10

    # MA tendencia
    if direction == 'buy'  and ma5 > ma20: score += 12
    if direction == 'sell' and ma5 < ma20: score += 12

    # Momentum ultimas 3 velas
    if len(prices) >= 4:
        up = all(prices[i] > prices[i-1] for i in range(-3,0))
        dn = all(prices[i] < prices[i-1] for i in range(-3,0))
        if direction == 'buy'  and up: score += 14
        if direction == 'sell' and dn: score += 14
        if direction == 'buy'  and dn: score -= 8
        if direction == 'sell' and up: score -= 8

    # Rebote desde extremo local (ultimas 10 velas)
    if len(prices) >= 10:
        mn = min(prices[-10:-1])
        mx = max(prices[-10:-1])
        rng = mx - mn
        if rng > 0:
            if direction == 'buy'  and (px - mn)/rng < 0.2: score += 10
            if direction == 'sell' and (mx - px)/rng < 0.2: score += 10

    return min(max(round(score), 35), 97)

def signal(prices):
    r = rsi(prices)
    ma5, ma20 = ma(prices, 5), ma(prices, 20)
    px = prices[-1]
    votes_buy = votes_sell = 0

    # RSI
    if r < 35: votes_buy  += 3
    if r > 65: votes_sell += 3
    if r < 45: votes_buy  += 1
    if r > 55: votes_sell += 1

    # MA
    if ma5 > ma20: votes_buy  += 2
    if ma5 < ma20: votes_sell += 2

    # Momentum 3 velas
    if len(prices) >= 4:
        up3 = all(prices[i] > prices[i-1] for i in range(-3,0))
        dn3 = all(prices[i] < prices[i-1] for i in range(-3,0))
        if up3: votes_buy  += 3
        if dn3: votes_sell += 3

    # Rebote local
    if len(prices) >= 8:
        mn = min(prices[-8:-1])
        mx = max(prices[-8:-1])
        rng = mx - mn if mx > mn else 1
        if (px - mn)/rng < 0.15: votes_buy  += 2
        if (mx - px)/rng < 0.15: votes_sell += 2

    if votes_buy >= 4 and votes_buy > votes_sell:   return 'buy'
    if votes_sell >= 4 and votes_sell > votes_buy:  return 'sell'
    return None

# ── TP/SL TRACKING ───────────────────────────

def check_signals():
    to_rm = []
    now = time.time()
    for s in active_sigs:
        cfg = ASSETS[s['asset']]
        btc = s['asset'] == 'btc'
        fmt = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')
        age = round((now - s['t'])/60)
        if now - s['t'] > 7200:
            to_rm.append(s); continue
        p = spot(s['asset'])
        if not p: continue
        hit_tp = (s['d']=='buy' and p>=s['tp']) or (s['d']=='sell' and p<=s['tp'])
        hit_sl = (s['d']=='buy' and p<=s['sl']) or (s['d']=='sell' and p>=s['sl'])
        if hit_tp:
            stats[s['asset']]['tp'] += 1
            tp_t = stats['gold']['tp']+stats['btc']['tp']
            sl_t = stats['gold']['sl']+stats['btc']['sl']
            tot  = tp_t+sl_t
            pct  = round(tp_t/tot*100) if tot else 0
            send(f'<b>✅ TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                 f'{fmt(s["e"])} → {fmt(p)} (+{age}min)\n'
                 f'📊 Hoy: {tp_t}✅ {sl_t}❌ | {pct}%')
            to_rm.append(s)
        elif hit_sl:
            stats[s['asset']]['sl'] += 1
            tp_t = stats['gold']['tp']+stats['btc']['tp']
            sl_t = stats['gold']['sl']+stats['btc']['sl']
            tot  = tp_t+sl_t
            pct  = round(tp_t/tot*100) if tot else 0
            send(f'<b>❌ SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                 f'{fmt(s["e"])} → {fmt(p)} (+{age}min)\n'
                 f'📊 Hoy: {tp_t}✅ {sl_t}❌ | {pct}%')
            to_rm.append(s)
    for s in to_rm:
        if s in active_sigs: active_sigs.remove(s)

# ── RESÚMENES ─────────────────────────────────

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

# ── SEÑAL PRINCIPAL ───────────────────────────

def analyze(asset, prices):
    cfg  = ASSETS[asset]
    px   = prices[-1]
    hora = datetime.now().hour
    btc  = asset == 'btc'
    fmt  = (lambda v: f'{round(v):,}') if btc else (lambda v: f'{v:.2f}')

    # Control señales por hora
    hk = hour_count.get(hora, {})
    if hk.get(asset, 0) >= MAX_PER_HOUR:
        log(f'  {cfg["icon"]} {MAX_PER_HOUR}/hora alcanzado')
        return

    # Detectar dirección
    sig = signal(prices)
    if not sig:
        log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi(prices)} → sin señal')
        return

    # Probabilidad
    p = prob(prices, sig)
    log(f'{cfg["icon"]} {fmt(px)} → {sig.upper()} {p}%')
    if p < MIN_PROB:
        log(f'  {p}% < {MIN_PROB}%, skip')
        return

    # Cooldown 3 min
    key = f'{sig}-{round(px/50)*50}'
    if key == last_sig_key[asset] and time.time()-last_sig_time[asset] < COOLDOWN:
        log(f'  Cooldown {round((COOLDOWN-(time.time()-last_sig_time[asset]))/60)}min')
        return

    last_sig_key[asset]  = key
    last_sig_time[asset] = time.time()
    hour_count[hora] = hk
    hour_count[hora][asset] = hk.get(asset, 0) + 1
    total_hora = sum(hour_count[hora].values())

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    # Lotaje recomendado
    if p >= 88:   rec, ico = '0.10-0.15', '💪'
    elif p >= 78: rec, ico = '0.05-0.10', '👍'
    else:         rec, ico = '0.01-0.05', '👌'

    gains = '\n'.join([f'  {l:.2f} → +{GAINS[asset][l]:.0f}€' for l in LOTAJES])
    tipo  = 'BUY' if isBuy else 'SELL'

    send(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {p}%</b>\n'
        f'📍 {fmt(px)}\n'
        f'🎯 {fmt(tp)}  (+{cfg["tp"]})\n'
        f'🛑 {fmt(sl)}  (-{cfg["sl"]})\n'
        f'━━━━━━━━━━━━\n'
        f'{ico} Lote rec: {rec}\n'
        f'{gains}\n'
        f'━━━━━━━━━━━━\n'
        f'Señal {total_hora}/{MAX_PER_HOUR*2} | {now_str()}'
    )
    log(f'✅ ENVIADA {tipo} {fmt(px)} {p}%')

    active_sigs.append({
        'asset': asset, 'd': sig,
        'e': px, 'tp': tp, 'sl': sl, 't': time.time()
    })

# ── MAIN ──────────────────────────────────────

def main():
    log('BOT INICIANDO')
    send('<b>🤖 BOT ACTIVO</b>\n'
         '🥇 ORO 🎯+20 🛑-10\n'
         '₿  BTC 🎯+500 🛑-200\n'
         '5 señales/hora | Min 70%\n'
         '🔔 TP/SL | Resumen horario | 23:00')

    while True:
        try:
            daily()
            check_signals()

            gp = gold_prices()
            if gp: analyze('gold', gp)
            else:  log('Sin datos ORO')

            time.sleep(3)

            bp = btc_prices()
            if bp: analyze('btc', bp)
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
