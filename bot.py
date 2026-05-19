import time
import requests
import json
from datetime import datetime

# =============================================
# SNIPER BOT v11 — SERVIDOR 24/7
# Analiza mercado cada 15 segundos
# Manda alertas a Telegram sin necesitar navegador
# =============================================

TG_TOKEN    = '8499195812:AAGRoj18KGtKJAJLHRpijCA2V5xvg-pJKVQ'
TG_CHAT_ID  = '6467338067'    # Jesús
TG_GROUP_ID = '-5123266724'   # Grupo: Jesús, Negro, Alejandro
TG_API      = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'

# Parámetros por activo
ASSETS = {
    'gold': {
        'name': 'ORO XAU/USD',
        'icon': '🥇',
        'symbol_yahoo': 'GC=F',
        'factor': 2,
        'desfase': 6.21,
        'tp': 20,
        'sl': 10,
        'val_pto': 1.0,  # € por punto con 0.01 lote
    },
    'btc': {
        'name': 'BITCOIN BTC/USD',
        'icon': '₿',
        'symbol_binance': 'BTCUSDT',
        'tp': 500,
        'sl': 200,
        'val_pto': 0.01,
    }
}

last_signal = {'gold': None, 'btc': None}
last_signal_time = {'gold': 0, 'btc': 0}
SIGNAL_COOLDOWN = 300  # 5 minutos entre señales del mismo tipo

def now_str():
    return datetime.now().strftime('%H:%M:%S')

# ---- TELEGRAM ----
def send_telegram(chat_id, msg):
    try:
        r = requests.post(TG_API, json={
            'chat_id': chat_id,
            'text': msg,
            'parse_mode': 'HTML'
        }, timeout=10)
        return r.json().get('ok', False)
    except Exception as e:
        print(f'TG Error: {e}')
        return False

def send_alert(msg):
    ok1 = send_telegram(TG_CHAT_ID, msg)
    ok2 = send_telegram(TG_GROUP_ID, msg)
    print(f'[{now_str()}] Telegram enviado: Jesús={ok1} Grupo={ok2}')
    return ok1 or ok2

# ---- OBTENER PRECIOS ----
def get_gold_prices():
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=5m&range=1d'
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        data = r.json()
        raw = data['chart']['result'][0]['indicators']['quote'][0]['close']
        prices = [(p + ASSETS['gold']['desfase']) * ASSETS['gold']['factor']
                  for p in raw if p is not None]
        return prices if len(prices) >= 15 else None
    except Exception as e:
        print(f'[{now_str()}] Gold fetch error: {e}')
        return None

def get_btc_prices():
    try:
        # Historial de velas
        url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=60'
        r = requests.get(url, timeout=10)
        prices = [float(k[4]) for k in r.json()]  # close prices

        # Precio actual en tiempo real (bid+ask)/2
        ticker_url = 'https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT'
        t = requests.get(ticker_url, timeout=5).json()
        if 'bidPrice' in t and 'askPrice' in t:
            real_price = (float(t['bidPrice']) + float(t['askPrice'])) / 2
            prices[-1] = real_price

        return prices if len(prices) >= 15 else None
    except Exception as e:
        print(f'[{now_str()}] BTC fetch error: {e}')
        return None

# ---- INDICADORES ----

def calc_ac(prices, fast=5, slow=34, signal=5):
    """Accelerator Oscillator (AC) con suavizado SMO
    AC = AO - SMA(AO, signal)
    AO = SMA(mid, fast) - SMA(mid, slow)
    Positivo y subiendo = aceleración alcista
    Negativo y bajando = aceleración bajista
    """
    if len(prices) < slow + signal + 2:
        return 0, 0
    
    # Calcular AO (Awesome Oscillator)
    def sma(data, n):
        if len(data) < n:
            return 0
        return sum(data[-n:]) / n
    
    ao_values = []
    for i in range(slow, len(prices) + 1):
        slice_p = prices[:i]
        ao = sma(slice_p, fast) - sma(slice_p, slow)
        ao_values.append(ao)
    
    if len(ao_values) < signal + 1:
        return 0, 0
    
    # AC = AO - SMA(AO, signal)
    ac_now = ao_values[-1] - sma(ao_values, signal)
    ac_prev = ao_values[-2] - sma(ao_values[:-1], signal) if len(ao_values) > signal else 0
    
    return round(ac_now, 4), round(ac_prev, 4)

def interpret_ac(ac_now, ac_prev):
    """Interpretación del AC para scalping"""
    if ac_now > 0 and ac_now > ac_prev:
        return 'BUY_STRONG', '🟢 AC positivo y subiendo — aceleración alcista fuerte'
    elif ac_now > 0 and ac_now < ac_prev:
        return 'BUY_WEAK', '🟡 AC positivo pero bajando — fuerza alcista débil'
    elif ac_now < 0 and ac_now < ac_prev:
        return 'SELL_STRONG', '🔴 AC negativo y bajando — aceleración bajista fuerte'
    elif ac_now < 0 and ac_now > ac_prev:
        return 'SELL_WEAK', '🟡 AC negativo pero subiendo — fuerza bajista débil'
    else:
        return 'NEUTRAL', '⚪ AC neutral — sin señal clara'

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains = losses = 0
    for i in range(len(prices) - period, len(prices)):
        d = prices[i] - prices[i-1]
        if d > 0:
            gains += d
        else:
            losses += abs(d)
    ag = gains / period
    al = losses / period
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag / al))

def calc_ma(prices, n):
    s = prices[-min(n, len(prices)):]
    return sum(s) / len(s)

def calc_mom(prices, n=5):
    if len(prices) < n + 1:
        return 0
    return prices[-1] - prices[-1-n]

def detect_range(prices):
    if len(prices) < 20:
        return {'is_range': False, 'high': 0, 'low': 0, 'size': 0}
    recent = prices[-20:]
    high = max(recent)
    low = min(recent)
    size = high - low
    ma5 = calc_ma(prices, 5)
    ma20 = calc_ma(prices, 20)
    trend_str = abs(ma5 - ma20)
    is_range = trend_str < size * 0.3 and size > 0
    return {'is_range': is_range, 'high': high, 'low': low, 'size': size}

def calc_prob(prices, direction, range_info):
    prob = 40
    px = prices[-1]
    rsi = calc_rsi(prices)
    mom = calc_mom(prices)
    size = range_info['size'] or 1
    high = range_info['high']
    low = range_info['low']

    if range_info['is_range']:
        prob += 15

    if direction == 'buy':
        dist_to_low = (px - low) / size
        if dist_to_low < 0.2:
            prob += 20
        elif dist_to_low < 0.35:
            prob += 12
        if rsi < 35:
            prob += 15
        elif rsi < 45:
            prob += 8
        if 0 < mom < size * 0.1:
            prob += 8
        if len(prices) >= 3 and prices[-1] > prices[-3]:
            prob += 5
    else:
        dist_to_high = (high - px) / size
        if dist_to_high < 0.2:
            prob += 20
        elif dist_to_high < 0.35:
            prob += 12
        if rsi > 65:
            prob += 15
        elif rsi > 55:
            prob += 8
        if -size * 0.1 < mom < 0:
            prob += 8
        if len(prices) >= 3 and prices[-1] < prices[-3]:
            prob += 5

    return min(max(round(prob), 35), 97)

def get_motivos(prices, direction, range_info):
    motivos = []
    px = prices[-1]
    rsi = calc_rsi(prices)
    mom = calc_mom(prices)
    size = range_info['size'] or 1

    if direction == 'buy':
        dist = (px - range_info['low']) / size
        if dist < 0.2:
            motivos.append('💚 Precio en zona BAJA del rango — rebote esperado')
        if rsi < 35:
            motivos.append(f'📊 RSI en sobreventa ({rsi:.1f}) — compradores entrando')
        elif rsi < 45:
            motivos.append(f'📊 RSI bajando ({rsi:.1f}) — zona de compra')
        if range_info['is_range']:
            motivos.append(f'🔄 Mercado en RANGO de {size:.1f} pts — scalping activo')
    else:
        dist = (range_info['high'] - px) / size
        if dist < 0.2:
            motivos.append('🩷 Precio en zona ALTA del rango — caída esperada')
        if rsi > 65:
            motivos.append(f'📊 RSI en sobrecompra ({rsi:.1f}) — vendedores entrando')
        elif rsi > 55:
            motivos.append(f'📊 RSI subiendo ({rsi:.1f}) — zona de venta')
        if range_info['is_range']:
            motivos.append(f'🔄 Mercado en RANGO de {size:.1f} pts — scalping activo')

    return motivos[:3]

# ---- ANALIZAR Y ALERTAR ----
def analyze_asset(asset_key, prices):
    cfg = ASSETS[asset_key]
    px = prices[-1]
    rsi = calc_rsi(prices)
    range_info = detect_range(prices)
    ma20 = calc_ma(prices, 20)

    # Calcular AC SMO (indicador de Negro)
    ac_now, ac_prev = calc_ac(prices)
    ac_signal, ac_desc = interpret_ac(ac_now, ac_prev)

    # Detectar señal combinando RANGO + RSI + AC
    sig_type = None
    if range_info['is_range'] and range_info['size'] > 0:
        size = range_info['size']
        dist_low = (px - range_info['low']) / size
        dist_high = (range_info['high'] - px) / size

        if dist_low < 0.25 and ac_signal in ('BUY_STRONG', 'BUY_WEAK'):
            sig_type = 'buy'
        elif dist_high < 0.25 and ac_signal in ('SELL_STRONG', 'SELL_WEAK'):
            sig_type = 'sell'

    # RSI extremo + AC confirma
    if not sig_type:
        if rsi < 30 and ac_signal in ('BUY_STRONG', 'BUY_WEAK'):
            sig_type = 'buy'
        elif rsi > 70 and ac_signal in ('SELL_STRONG', 'SELL_WEAK'):
            sig_type = 'sell'

    if not sig_type:
        fmt = lambda v: f'{round(v):,}' if asset_key == 'btc' else f'{v:.2f}'
        print(f'[{now_str()}] {cfg["icon"]} {cfg["name"]}: {fmt(px)} | RSI: {rsi:.1f} | Rango: {range_info["is_range"]} | Sin señal')
        return

    # Calcular probabilidad
    prob = calc_prob(prices, sig_type, range_info)
    fmt = lambda v: f'{round(v):,}' if asset_key == 'btc' else f'{v:.2f}'

    print(f'[{now_str()}] {cfg["icon"]} {cfg["name"]}: {fmt(px)} | RSI: {rsi:.1f} | {sig_type.upper()} | Prob: {prob}%')

    # Solo enviar si prob >= 75%
    if prob < 75:
        print(f'  → Prob {prob}% < 75%, ignorando')
        return

    # Evitar spam — mismo tipo de señal en menos de 5 minutos
    sig_key = f'{sig_type}-{round(px / (cfg["sl"] * 2))}'
    elapsed = time.time() - last_signal_time[asset_key]
    if last_signal[asset_key] == sig_key and elapsed < SIGNAL_COOLDOWN:
        print(f'  → Señal repetida, esperando cooldown ({int(SIGNAL_COOLDOWN-elapsed)}s)')
        return

    last_signal[asset_key] = sig_key
    last_signal_time[asset_key] = time.time()

    # Calcular niveles
    isBuy = sig_type == 'buy'
    tp = px + cfg['tp'] if isBuy else px - cfg['tp']
    sl = px - cfg['sl'] if isBuy else px + cfg['sl']
    gain_001 = round(cfg['tp'] * cfg['val_pto'], 2)
    loss_001 = round(cfg['sl'] * cfg['val_pto'], 2)
    motivos = get_motivos(prices, sig_type, range_info)

    emoji = '💚' if isBuy else '🩷'
    tipo = 'BUY — COMPRA AQUÍ' if isBuy else 'SELL — VENDE AQUÍ'

    msg = f'''{emoji} <b>SCALPING {tipo}</b>
{cfg['icon']} <b>{cfg['name']}</b>
🕐 {now_str()}

📍 <b>ENTRADA:</b> {fmt(px)}
🎯 <b>TAKE PROFIT:</b> {fmt(tp)} (+{cfg['tp']} pts)
🛑 <b>STOP LOSS:</b> {fmt(sl)} (-{cfg['sl']} pts)
📊 <b>Probabilidad:</b> {prob}%
📈 <b>RSI:</b> {rsi:.1f}
⚡ <b>AC SMO:</b> {ac_desc}

{chr(10).join(f'• {m}' for m in motivos)}

💰 <b>Con 0.01 lote:</b> +{gain_001}€ / -{loss_001}€
⚠️ Verifica en MT5 antes de entrar'''

    if send_alert(msg):
        print(f'  ✅ Alerta enviada: {sig_type.upper()} {fmt(px)} prob={prob}%')
    else:
        print(f'  ❌ Error enviando alerta')

# ---- LOOP PRINCIPAL ----
def main():
    print('=' * 50)
    print('SNIPER BOT v11 — SERVIDOR 24/7')
    print('Telegram: Jesús + Grupo (3 personas)')
    print('Escaneo cada 15 segundos')
    print('Solo señales +75% probabilidad')
    print('=' * 50)

    # Mensaje de inicio
    send_alert('''🤖 <b>SNIPER BOT v11 — SERVIDOR ACTIVO 24/7</b>

Hola Jesús 👋 y al grupo!
El servidor está corriendo y vigilando el mercado.

🥇 ORO: TP +20pts / SL -10pts
₿ BTC: TP +500pts / SL -200pts

✅ Solo alertas +75% probabilidad
⏱ Escaneo cada 15 segundos
🔄 Funciona sin tener el móvil abierto

¡Listo para operar! 🚀''')

    scan_count = 0
    while True:
        try:
            scan_count += 1
            print(f'\n[{now_str()}] Escaneo #{scan_count}')

            # Analizar Oro
            gold_prices = get_gold_prices()
            if gold_prices:
                analyze_asset('gold', gold_prices)
            else:
                print(f'[{now_str()}] ⚠️ Sin datos de Oro')

            time.sleep(2)  # pausa entre requests

            # Analizar BTC
            btc_prices = get_btc_prices()
            if btc_prices:
                analyze_asset('btc', btc_prices)
            else:
                print(f'[{now_str()}] ⚠️ Sin datos de BTC')

            # Esperar 15 segundos para el siguiente escaneo
            time.sleep(13)

        except KeyboardInterrupt:
            print('\n⛔ Bot detenido manualmente')
            send_alert('⛔ <b>SNIPER BOT</b> — Servidor detenido')
            break
        except Exception as e:
            print(f'[{now_str()}] Error general: {e}')
            time.sleep(15)

if __name__ == '__main__':
    main()
