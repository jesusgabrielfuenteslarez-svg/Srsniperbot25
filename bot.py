import time
import requests
from datetime import datetime

# =============================================
# SNIPER BOT v12.0 - VERSION FINAL
# =============================================
# ANTES DE SUBIR:
# 1. Ve a twelvedata.com, registrate gratis
# 2. Copia tu API Key y pegala abajo
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

# ── Umbrales ──────────────────────────────────
BASE_THRESHOLD    = 85    # minimo para cualquier senal
STRONG_THRESHOLD  = 90    # senales fuertes
OUT_SESSION_THR   = 95    # fuera de sesion Londres/NY
MAX_PER_HOUR      = 10    # maximo señales por hora
MIN_STRONG        = 5     # minimo de 90%+ por hora

# ── Estado por hora ───────────────────────────
hour_signals      = []    # lista de senales esta hora
current_hour      = datetime.now().hour
last_hour_summary = -1    # para no repetir resumen

# ── Adaptativo ───────────────────────────────
current_threshold = BASE_THRESHOLD
consecutive_sl    = 0
paused_until      = 0

# ── Señales activas (seguimiento TP/SL) ───────
active_signals    = []
last_signal       = {'gold': None, 'btc': None}
last_signal_time  = {'gold': 0,    'btc': 0}
COOLDOWN          = 300
MAX_SIGNAL_AGE    = 7200

# ── Stats diarias ─────────────────────────────
stats = {'gold': {'tp': 0, 'sl': 0}, 'btc': {'tp': 0, 'sl': 0}}
last_summary_date = None
SUMMARY_HOUR      = 23

# ── Sesiones (UTC) ────────────────────────────
SESSIONS = [(7, 16), (12, 21)]  # Londres, New York

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
    if 7 <= h < 12:  return 'Londres'
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
        log(f'Twelve excepcion [{symbol}]: {e}')
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
# RESUMEN POR HORA
# =============================================

def check_hourly_summary():
    global current_hour, last_hour_summary, hour_signals, current_threshold, consecutive_sl

    now = datetime.now()
    if now.hour == last_hour_summary:
        return
    if now.minute != 0:
        return

    last_hour_summary = now.hour

    if not hour_signals:
        log('Sin senales esta hora, no hay resumen')
        hour_signals = []
        current_hour = now.hour
        return

    tp_count   = sum(1 for s in hour_signals if s['result'] == 'tp')
    sl_count   = sum(1 for s in hour_signals if s['result'] == 'sl')
    open_count = sum(1 for s in hour_signals if s['result'] == 'open')
    total      = len(hour_signals)
    closed     = tp_count + sl_count
    pct = round((tp_count / closed) * 100) if closed > 0 else 0

    if   pct == 100: stars = '🌟🌟🌟'
    elif pct >= 80:  stars = '🌟🌟'
    elif pct >= 60:  stars = '🌟'
    else:            stars = ''

    hora_pasada = f"{(now.hour - 1) % 24:02d}:00"

    send_alert(
        f'<b>RESUMEN {hora_pasada} - {now.hour:02d}:00</b>\n'
        f'TP: {tp_count}  SL: {sl_count}  En curso: {open_count}\n'
        f'Efectividad: <b>{pct}%</b> {stars}\n'
        f'Total señales: {total}'
    )
    log(f'Resumen hora: {tp_count}TP {sl_count}SL {open_count}abiertos {pct}%')

    # Ajuste adaptativo basado en efectividad
    if closed >= 3:
        if pct < 80:
            current_threshold = min(current_threshold + 5, 95)
            log(f'Efectividad {pct}% < 80% — umbral sube a {current_threshold}%')
        elif pct >= 80 and current_threshold > BASE_THRESHOLD:
            current_threshold = max(current_threshold - 5, BASE_THRESHOLD)
            log(f'Efectividad {pct}% OK — umbral baja a {current_threshold}%')

    # Reset para la siguiente hora
    hour_signals = []
    current_hour = now.hour
    consecutive_sl = 0

# =============================================
# RESUMEN DIARIO — 23:00
# =============================================

def check_daily_summary():
    global last_summary_date
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if now.hour == SUMMARY_HOUR and now.minute == 0:
        if last_summary_date != today:
            last_summary_date = today
            tp_g = stats['gold']['tp']
            sl_g = stats['gold']['sl']
            tp_b = stats['btc']['tp']
            sl_b = stats['btc']['sl']
            total_tp = tp_g + tp_b
            total_sl = sl_g + sl_b
            total    = total_tp + total_sl
            pct = round((total_tp / total) * 100) if total > 0 else 0
            if   pct == 100: stars = '🌟🌟🌟'
            elif pct >= 80:  stars = '🌟🌟'
            elif pct >= 60:  stars = '🌟'
            else:            stars = ''
            send_alert(
                f'<b>RESUMEN DIA {now.strftime("%d/%m")}</b>\n'
                f'TP: {total_tp}   SL: {total_sl}\n'
                f'Efectividad: <b>{pct}%</b> {stars}\n'
                f'Total: {total} senales\n'
                f'ORO  {tp_g}TP {sl_g}SL\n'
                f'BTC  {tp_b}TP {sl_b}SL\n'
                f'Umbral activo: {current_threshold}%'
            )
            for k in stats:
                stats[k]['tp'] = 0
                stats[k]['sl'] = 0

# =============================================
# INDICADORES
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

def calc_mom(prices, n=5):
    return prices[-1] - prices[-1-n] if len(prices) >= n+1 else 0

def calc_ac(prices, fast=5, slow=34, sig=5):
    if len(prices) < slow+sig+2: return 0, 0
    def sma(d, n): return sum(d[-n:])/n if len(d) >= n else 0
    ao = [sma(prices[:i], fast) - sma(prices[:i], slow)
          for i in range(slow, len(prices)+1)]
    if len(ao) < sig+1: return 0, 0
    return round(ao[-1]-sma(ao,sig),4), round(ao[-2]-sma(ao[:-1],sig),4)

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

def calc_prob(prices, direction, range_info):
    prob, px = 40, prices[-1]
    rsi = calc_rsi(prices)
    ac_now, ac_prev = calc_ac(prices)
    size = range_info['size'] or 1
    if range_info['is_range']: prob += 12
    if direction == 'buy':
        dist = (px - range_info['low']) / size
        if dist < 0.20: prob += 20
        elif dist < 0.35: prob += 10
        if rsi < 30: prob += 18
        elif rsi < 40: prob += 10
        elif rsi < 50: prob += 5
        if ac_now > 0 and ac_now > ac_prev: prob += 10
        elif ac_now > 0: prob += 5
        if calc_mom(prices) > 0: prob += 5
    else:
        dist = (range_info['high'] - px) / size
        if dist < 0.20: prob += 20
        elif dist < 0.35: prob += 10
        if rsi > 70: prob += 18
        elif rsi > 60: prob += 10
        elif rsi > 50: prob += 5
        if ac_now < 0 and ac_now < ac_prev: prob += 10
        elif ac_now < 0: prob += 5
        if calc_mom(prices) < 0: prob += 5
    return min(max(round(prob), 35), 97)

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
            consecutive_sl = 0
            # bajar umbral si efectividad buena
            total_tp = stats['gold']['tp'] + stats['btc']['tp']
            total_sl = stats['gold']['sl'] + stats['btc']['sl']
            total = total_tp + total_sl
            if total >= 5 and (total_tp/total*100) >= 80 and current_threshold > BASE_THRESHOLD:
                current_threshold = max(current_threshold - 5, BASE_THRESHOLD)
            send_alert(
                f'<b>TP {cfg["icon"]} +{cfg["tp"]}</b>\n'
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)
            log(f'TP {cfg["name"]} {fmt(price)}')

        elif hit_sl:
            sig['result'] = 'sl'
            stats[sig['asset']]['sl'] += 1
            consecutive_sl += 1
            total_tp = stats['gold']['tp'] + stats['btc']['tp']
            total_sl = stats['gold']['sl'] + stats['btc']['sl']
            total = total_tp + total_sl
            if total >= 3 and (total_tp/total*100) < 80:
                current_threshold = min(current_threshold + 5, 95)
                log(f'Efectividad baja — umbral sube a {current_threshold}%')
            if consecutive_sl >= 3:
                paused_until = time.time() + 1800
                current_threshold = min(current_threshold + 5, 95)
                send_alert(
                    f'<b>PAUSA 30 MIN</b>\n'
                    f'3 SL consecutivos\n'
                    f'Nuevo umbral: {current_threshold}%\n'
                    f'{now_str()}'
                )
                consecutive_sl = 0
            send_alert(
                f'<b>SL {cfg["icon"]} -{cfg["sl"]}</b>\n'
                f'{fmt(sig["entry"])} a {fmt(price)}\n'
                f'{now_str()} (+{age_min}min)'
            )
            to_remove.append(sig)
            log(f'SL {cfg["name"]} {fmt(price)}')

    for s in to_remove:
        # actualizar resultado en hour_signals
        for hs in hour_signals:
            if hs.get('id') == s.get('id'):
                hs['result'] = s.get('result', 'open')
        if s in active_signals:
            active_signals.remove(s)

# =============================================
# SEÑAL — ANALISIS Y ENVIO
# =============================================

def analyze_and_alert(asset_key, prices):
    global hour_signals, current_threshold, paused_until

    cfg = ASSETS[asset_key]
    px  = prices[-1]
    rsi = calc_rsi(prices)
    range_info = detect_range(prices)
    ac_now, ac_prev = calc_ac(prices)
    is_btc = asset_key == 'btc'
    fmt = lambda v: f'{round(v):,}' if is_btc else f'{v:.2f}'

    # Pausa activa
    if time.time() < paused_until:
        mins = round((paused_until - time.time()) / 60)
        log(f'  Pausa activa — {mins} min restantes')
        return

    # Detectar direccion
    sig = None
    if range_info['is_range'] and range_info['size'] > 0:
        sz = range_info['size']
        if (px - range_info['low']) / sz < 0.25: sig = 'buy'
        elif (range_info['high'] - px) / sz < 0.25: sig = 'sell'
    if not sig:
        if rsi < 30: sig = 'buy'
        elif rsi > 70: sig = 'sell'

    log(f'{cfg["icon"]} {fmt(px)} RSI:{rsi} AC:{ac_now:.3f} -> {sig or "sin senal"}')
    if not sig: return

    prob = calc_prob(prices, sig, range_info)

    # Umbral segun sesion
    umbral = OUT_SESSION_THR if not is_market_open() else max(current_threshold, BASE_THRESHOLD)

    if prob < umbral:
        log(f'  {prob}% < {umbral}%, skip')
        return

    # Control por hora
    hora_actual = datetime.now().hour
    senales_hora = [s for s in hour_signals if s['hour'] == hora_actual]
    fuertes_hora = [s for s in senales_hora if s['prob'] >= STRONG_THRESHOLD]

    if len(senales_hora) >= MAX_PER_HOUR:
        log(f'  Limite {MAX_PER_HOUR} senales/hora alcanzado')
        return

    # Si ya tenemos 5 fuertes, solo aceptar 85%+
    # Si no tenemos 5 fuertes, priorizar 90%+
    if len(fuertes_hora) < MIN_STRONG and prob < STRONG_THRESHOLD:
        log(f'  {prob}% — esperando senales 90%+ ({len(fuertes_hora)}/{MIN_STRONG} fuertes)')
        return

    # Cooldown por activo
    sig_key = f'{sig}-{round(px / (cfg["sl"] * 2))}'
    if (last_signal[asset_key] == sig_key and
            time.time() - last_signal_time[asset_key] < COOLDOWN):
        log('  Cooldown activo')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    isBuy = sig == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']

    if   ac_now > 0 and ac_now > ac_prev: ac_txt = 'AC+'
    elif ac_now < 0 and ac_now < ac_prev: ac_txt = 'AC-'
    else:                                  ac_txt = 'AC~'

    # Lotaje recomendado por probabilidad
    if prob >= 92:
        lote_rec = '0.10 - 0.15'
        lote_ico = '💪'
    elif prob >= 88:
        lote_rec = '0.05 - 0.10'
        lote_ico = '👍'
    else:
        lote_rec = '0.01 - 0.05'
        lote_ico = '👌'

    lots_txt = '\n'.join([
        f'{lot:.2f} +{round(cfg["tp"] * cfg["val_pto"] * lot / 0.01, 2):.2f}EUR'
        for lot in LOTAJES
    ])

    tipo = 'BUY' if isBuy else 'SELL'
    sig_id = f'{asset_key}-{int(time.time())}'
    num_hora = len(senales_hora) + 1

    send_alert(
        f'<b>{tipo} {cfg["icon"]} {cfg["name"]} {prob}%</b>\n'
        f'RSI {rsi} | {ac_txt} | {session_name()}\n'
        f'Entrada: {fmt(px)}\n'
        f'TP: {fmt(tp)} (+{cfg["tp"]})\n'
        f'SL: {fmt(sl)} (-{cfg["sl"]})\n'
        f'---\n'
        f'{lote_ico} Lote: {lote_rec}\n'
        f'{lots_txt}\n'
        f'Senal {num_hora}/{MAX_PER_HOUR} | {now_str()}'
    )
    log(f'{tipo} {fmt(px)} {prob}% — {num_hora}/{MAX_PER_HOUR} esta hora')

    # Registrar en seguimiento
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
    log('SNIPER BOT v12.0 INICIANDO')
    send_alert(
        '<b>SNIPER BOT v12.0 ACTIVO</b>\n'
        'ORO TP+20 SL-10 | BTC TP+500 SL-200\n'
        'Max 10 senales/hora | Min 5 al 90%+\n'
        'Resumen cada hora | Resumen diario 23:00'
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
