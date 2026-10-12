#!/usr/bin/env python3
"""
بوت تداول ورقي لفريم 15 دقيقة
فحص كل 60 ثانية
يعمل على Oracle Cloud أو أي استضافة
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# إعداد التسجيل
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("15m-bot")

# إعدادات المنصة
BASE_URL = "https://api.binance.com"
session = requests.Session()
session.headers.update({"User-Agent": "TradingBot/1.0"})

# إعدادات التداول (من Environment Variables أو .env)
INITIAL_CASH = float(os.getenv("INITIAL_CASH", "100.0"))
FEE_RATE = 0.001  # 0.1% رسوم التداول
SLIPPAGE_RATE = 0.0005  # 0.05% انزلاق سعري
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))  # 0.5% مخاطرة لكل صفقة
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0.015"))  # 1.5% حد خسارة يومي
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "50"))
MIN_QUOTE_VOLUME_USDT = 1_000_000
HTTP_TIMEOUT = 12

STATE_FILE = Path("paper_state.json")

# ==================== دوال المساعدة ====================

def utc_now():
    return datetime.now(timezone.utc)

def utc_day():
    return utc_now().strftime("%Y-%m-%d")

# ==================== دوال جلب البيانات ====================

def get_klines(symbol, interval, limit=250):
    """جلب شموع مباشرة من Binance"""
    try:
        response = session.get(
            f"{BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()
        raw = response.json()
        
        candles = []
        now_ms = int(time.time() * 1000)
        for k in raw:
            close_time = int(k[6])
            if close_time >= now_ms:
                continue
            
            candles.append({
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "quote_volume": float(k[7]),
                "taker_buy_quote_volume": float(k[10]),
            })
        return candles
    except Exception as e:
        log.warning(f"خطأ في جلب البيانات لـ {symbol}: {e}")
        return []

# ==================== دوال المؤشرات الفنية ====================

def ema(values, period):
    """حساب المتوسط المتحرك الأسي"""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * multiplier + current * (1 - multiplier)
    return current

def rsi(values, period=14):
    """حساب مؤشر القوة النسبية"""
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(candles, period=14):
    """حساب متوسط المدى الحقيقي"""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))
    
    if len(true_ranges) < period:
        return None
    
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value

# ==================== دوال إدارة الحالة ====================

def default_state():
    return {
        "cash": INITIAL_CASH,
        "positions": {},
        "trades": [],
        "day": utc_day(),
        "day_start_equity": INITIAL_CASH,
        "last_scan": None,
    }

def load_state():
    if not STATE_FILE.exists():
        return default_state()
    
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("cash", INITIAL_CASH)
        state.setdefault("positions", {})
        state.setdefault("trades", [])
        state.setdefault("day", utc_day())
        state.setdefault("day_start_equity", state["cash"])
        state.setdefault("last_scan", None)
        
        if state["day"] != utc_day():
            state["day"] = utc_day()
            state["day_start_equity"] = equity(state)
        
        return state
    except Exception as exc:
        log.exception(f"تعذّرت قراءة ملف الحالة: {exc}")
        raise RuntimeError("ملف paper_state.json تالف") from exc

def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE_FILE)

# ==================== دوال الاستراتيجية ====================

def get_15m_signal(symbol, candles_15m, candles_1h):
    """تحليل السوق وإرجاع إشارة شراء إذا تحققت الشروط"""
    if len(candles_15m) < 50 or len(candles_1h) < 210:
        return None

    closes_15m = [c["close"] for c in candles_15m]
    closes_1h = [c["close"] for c in candles_1h]

    current_rsi = rsi(closes_15m, 14)
    current_atr = atr(candles_15m, 14)
    trend_ema200_1h = ema(closes_1h, 200)

    if not all([current_rsi, current_atr, trend_ema200_1h]) or current_atr <= 0:
        return None

    last = candles_15m[-1]
    previous = candles_15m[-2]

    # حساب الانخفاض (1 ساعة = 4 شموع، 4 ساعات = 16 شمعة)
    drop_1h = (last["close"] / candles_15m[-5]["close"]) - 1
    drop_4h = (last["close"] / candles_15m[-17]["close"]) - 1

    # حساب متوسط الحجم
    recent_volumes = [c["quote_volume"] for c in candles_15m[-21:-1]]
    avg_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
    
    # نسبة ضغط الشراء
    buy_ratio = last["taker_buy_quote_volume"] / last["quote_volume"] if last["quote_volume"] > 0 else 0

    # شروط الدخول
    conditions = {
        "dip_detected": drop_1h <= -0.02 or drop_4h <= -0.03,
        "bullish_reclaim": last["close"] > last["open"] and last["close"] > previous["high"],
        "volume_confirmed": avg_volume > 0 and last["quote_volume"] >= 1.5 * avg_volume,
        "trend_confirmed": closes_1h[-1] > trend_ema200_1h,
        "rsi_ok": 40 <= current_rsi <= 68,
        "buy_pressure": buy_ratio >= 0.52
    }

    if all(conditions.values()):
        return {
            "symbol": symbol,
            "price": last["close"],
            "atr": current_atr,
            "rsi": current_rsi,
            "volume_ratio": last["quote_volume"] / avg_volume,
            "buy_ratio_pct": buy_ratio * 100,
            "drop_1h_pct": drop_1h * 100,
            "drop_4h_pct": drop_4h * 100
        }
    return None

# ==================== دوال إدارة الصفقات ====================

def get_top_usdt_pairs():
    """جلب أفضل أزواج USDT من حيث الحجم"""
    try:
        tickers = session.get(f"{BASE_URL}/api/v3/ticker/24hr", timeout=HTTP_TIMEOUT).json()
        exchange = session.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=HTTP_TIMEOUT).json()

        allowed = {
            item["symbol"]
            for item in exchange.get("symbols", [])
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("isSpotTradingAllowed", True)
        }

        filtered = []
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if symbol not in allowed or not symbol.endswith("USDT"):
                continue

            base = symbol[:-4]
            if any(base.endswith(suffix) for suffix in ("UP", "DOWN", "BULL", "BEAR")):
                continue

            try:
                quote_volume = float(ticker.get("quoteVolume", 0))
                price = float(ticker.get("lastPrice", 0))
            except (TypeError, ValueError):
                continue

            if quote_volume >= MIN_QUOTE_VOLUME_USDT and price > 0:
                filtered.append((symbol, quote_volume))

        filtered.sort(key=lambda item: item[1], reverse=True)
        return [symbol for symbol, _ in filtered[:MAX_PAIRS]]
    except Exception as e:
        log.error(f"خطأ في جلب الأزواج: {e}")
        return []

def equity(state):
    """حساب القيمة الإجمالية للحساب"""
    position_value = sum(
        pos["qty"] * pos.get("last_price", pos["entry_price"])
        for pos in state["positions"].values()
    )
    return state["cash"] + position_value

def mark_positions(state):
    """تحديث آخر سعر للصفقات المفتوحة"""
    for symbol, position in list(state["positions"].items()):
        try:
            candles = get_klines(symbol, "5m", 3)
            if candles:
                position["last_price"] = candles[-1]["close"]
        except Exception as exc:
            log.warning(f"{symbol}: تعذّر تحديث السعر: {exc}")

def telegram(message):
    """إرسال رسالة إلى Telegram"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        log.info("Telegram غير مهيأ؛ تم تخطي الإرسال.")
        return
    
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning(f"فشل إرسال Telegram: {exc}")

def open_position(state, signal):
    """فتح صفقة جديدة"""
    symbol = signal["symbol"]
    
    if symbol in state["positions"]:
        return False
    
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False

    current_equity = equity(state)
    maximum_loss = current_equity * RISK_PER_TRADE

    # حساب سعر الدخول مع الانزلاق السعري
    entry = signal["price"] * (1 + SLIPPAGE_RATE)
    
    # حساب وقف الخسارة ديناميكياً بناءً على ATR
    stop = entry - 1.5 * signal["atr"]

    if stop <= 0 or stop >= entry:
        return False

    risk_per_unit = entry - stop
    quantity = maximum_loss / risk_per_unit

    max_notional = min(current_equity * 0.20, state["cash"] / (1 + FEE_RATE))
    quantity = min(quantity, max_notional / entry)

    if quantity <= 0:
        return False

    notional = quantity * entry
    fee = notional * FEE_RATE
    cost_basis = notional + fee

    if cost_basis > state["cash"]:
        return False

    state["cash"] -= cost_basis
    
    # حساب هدف الربح (نسبة 1:2 مكافأة إلى مخاطرة)
    target = entry + 2 * risk_per_unit

    state["positions"][symbol] = {
        "entry_price": entry,
        "qty": quantity,
        "stop_price": stop,
        "target_price": target,
        "cost_basis": cost_basis,
        "last_price": signal["price"],
        "opened_at": utc_now().isoformat(),
        "atr": signal["atr"],
    }

    # حساب النسب المئوية
    stop_loss_pct = ((stop - entry) / entry) * 100
    target_pct = ((target - entry) / entry) * 100

    message = (
        f"🟢 <b>فتح صفقة شراء</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 الرمز: <b>{symbol}</b>\n"
        f"💰 سعر السوق: ${signal['price']:.6f}\n"
        f"📈 سعر الدخول (مع الانزلاق): ${entry:.6f}\n"
        f"🔢 الكمية: {quantity:.6f}\n"
        f"💵 القيمة الإجمالية: ${notional:.2f}\n"
        f"💸 الرسوم (0.1%): ${fee:.4f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛑 وقف الخسارة: ${stop:.6f} ({stop_loss_pct:.2f}%)\n"
        f"🎯 هدف الربح: ${target:.6f} (+{target_pct:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 RSI: {signal['rsi']:.1f}\n"
        f"📊 ضغط الشراء: {signal['buy_ratio_pct']:.1f}%\n"
        f"📈 نسبة الحجم: {signal['volume_ratio']:.2f}x\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 الرصيد المتبقي: ${state['cash']:.2f}\n"
        f"📊 القيمة الإجمالية: ${equity(state):.2f}"
    )
    
    log.info(f"فتح صفقة {symbol} | الدخول: {entry:.6f} | وقف: {stop:.6f} | هدف: {target:.6f}")
    telegram(message)
    return True

def close_position(state, symbol, price, reason):
    """إغلاق صفقة"""
    position = state["positions"].pop(symbol)
    
    # حساب سعر الخروج مع الانزلاق السعري
    exit_price = price * (1 - SLIPPAGE_RATE)
    gross = position["qty"] * exit_price
    fee = gross * FEE_RATE
    net_proceeds = gross - fee

    state["cash"] += net_proceeds
    pnl = net_proceeds - position["cost_basis"]
    pnl_pct = (pnl / position["cost_basis"]) * 100

    state["trades"].append({
        "symbol": symbol,
        "entry_price": position["entry_price"],
        "exit_price": exit_price,
        "qty": position["qty"],
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "closed_at": utc_now().isoformat(),
    })

    # أيقونة حسب الربح أو الخسارة
    icon = "✅" if pnl > 0 else "❌"
    pnl_emoji = "💰" if pnl > 0 else "💸"

    message = (
        f"{icon} <b>إغلاق صفقة</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 الرمز: <b>{symbol}</b>\n"
        f"📝 السبب: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 سعر الدخول: ${position['entry_price']:.6f}\n"
        f"📉 سعر الخروج (مع الانزلاق): ${exit_price:.6f}\n"
        f"🔢 الكمية: {position['qty']:.6f}\n"
        f"💵 القيمة: ${gross:.2f}\n"
        f"💸 الرسوم (0.1%): ${fee:.4f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} الربح/الخسارة: ${pnl:.4f} ({pnl_pct:+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 الرصيد النقدي: ${state['cash']:.2f}\n"
        f"📊 القيمة الإجمالية: ${equity(state):.2f}"
    )
    
    log.info(f"إغلاق {symbol} | السبب: {reason} | الربح: {pnl:.4f}")
    telegram(message)

def manage_positions(state):
    """إدارة الصفقات المفتوحة"""
    for symbol in list(state["positions"]):
        position = state["positions"][symbol]
        
        try:
            candles = get_klines(symbol, "15m", 60)
            if len(candles) < 50:
                continue

            price = candles[-1]["close"]
            position["last_price"] = price

            closes = [c["close"] for c in candles]
            ema20 = ema(closes, 20)
            ema50 = ema(closes, 50)

            if price <= position["stop_price"]:
                close_position(state, symbol, price, "🛑 وقف الخسارة")
            elif price >= position["target_price"]:
                close_position(state, symbol, price, "🎯 هدف الربح")
            elif ema20 is not None and ema50 is not None and ema20 < ema50:
                close_position(state, symbol, price, "📉 تقاطع EMA20 تحت EMA50")
        except Exception as exc:
            log.warning(f"{symbol}: تعذّرت إدارة الصفقة: {exc}")

def send_daily_report(state):
    """إرسال تقرير يومي شامل"""
    current_equity = equity(state)
    total_pnl = current_equity - INITIAL_CASH
    total_pnl_pct = (total_pnl / INITIAL_CASH) * 100
    
    total_trades = len(state["trades"])
    winning_trades = len([t for t in state["trades"] if t["pnl"] > 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    message = (
        f"📊 <b>التقرير اليومي</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 رأس المال الابتدائي: ${INITIAL_CASH:.2f}\n"
        f"💰 القيمة الحالية: ${current_equity:.2f}\n"
        f"{'💰' if total_pnl >= 0 else '💸'} الربح/الخسارة الإجمالي: ${total_pnl:.2f} ({total_pnl_pct:+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 عدد الصفقات: {total_trades}\n"
        f"✅ صفقات رابحة: {winning_trades}\n"
        f"❌ صفقات خاسرة: {total_trades - winning_trades}\n"
        f"📊 نسبة النجاح: {win_rate:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔓 صفقات مفتوحة: {len(state['positions'])}\n"
        f"💵 الرصيد النقدي: ${state['cash']:.2f}"
    )
    
    telegram(message)

# ==================== الدالة الرئيسية ====================

def run_bot_cycle():
    """تشغيل دورة فحص واحدة"""
    state = load_state()
    
    if state["day"] != utc_day():
        state["day"] = utc_day()
        state["day_start_equity"] = equity(state)
        send_daily_report(state)

    log.info("بدء الفحص: تداول ورقي فقط، دون أوامر حقيقية.")

    try:
        mark_positions(state)
        manage_positions(state)

        current_equity = equity(state)
        day_start = max(float(state.get("day_start_equity", INITIAL_CASH)), 0.01)
        daily_loss = (day_start - current_equity) / day_start

        if daily_loss >= MAX_DAILY_LOSS:
            message = (
                f"⛔ <b>توقف فتح صفقات جديدة</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"السبب: الوصول إلى حد الخسارة اليومية\n"
                f"الخسارة اليومية: {daily_loss * 100:.2f}%\n"
                f"الحد المسموح: {MAX_DAILY_LOSS * 100:.2f}%"
            )
            log.warning(f"حد الخسارة اليومية: {daily_loss * 100:.2f}%")
            telegram(message)
        else:
            symbols = get_top_usdt_pairs()
            log.info(f"تم اختيار {len(symbols)} زوج USDT.")

            for index, symbol in enumerate(symbols, 1):
                if len(state["positions"]) >= MAX_OPEN_POSITIONS:
                    break
                
                if symbol in state["positions"]:
                    continue

                try:
                    candles_15m = get_klines(symbol, "15m", 250)
                    candles_1h = get_klines(symbol, "1h", 250)
                    
                    if not candles_15m or not candles_1h:
                        continue

                    signal = get_15m_signal(symbol, candles_15m, candles_1h)
                    
                    if signal:
                        log.info(
                            f"🚨 إشارة {symbol}: RSI={signal['rsi']:.1f} volume={signal['volume_ratio']:.2f}x"
                        )
                        open_position(state, signal)
                except Exception as exc:
                    log.warning(f"{symbol}: فشل تحليل الزوج: {exc}")

                if index % 10 == 0:
                    log.info(f"تم فحص {index}/{len(symbols)} زوج.")
                    time.sleep(0.5)  # تأخير قصير لتجنب الحظر

        state["last_scan"] = utc_now().isoformat()
        mark_positions(state)
        save_state(state)

        log.info(
            f"✅ انتهى الفحص | النقد={state['cash']:.2f} | "
            f"الحساب={equity(state):.2f} | صفقات={len(state['positions'])}"
        )
    except Exception:
        save_state(state)
        log.exception("فشل التشغيل")
        raise

def main():
    """تشغيل البوت بشكل مستمر - فحص كل 60 ثانية"""
    log.info("🚀 بدء تشغيل البوت - فحص كل 60 ثانية")
    
    while True:
        try:
            run_bot_cycle()
        except Exception as e:
            log.error(f"خطأ غير متوقع: {e}")
        
        # الانتظار 60 ثانية قبل الدورة التالية
        log.info("⏳ الانتظار 60 ثانية قبل الفحص التالي...")
        time.sleep(60)

if __name__ == "__main__":
    main()
