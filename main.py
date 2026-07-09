import asyncio
import time
import os
import json
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import yfinance as yf
import pandas as pd
import numpy as np
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_credentials=False เพราะแอปนี้ไม่ได้ใช้ cookie/session-based auth เลย
    # (ตาม CORS spec เบราว์เซอร์ไม่ยอมให้ allow_origins="*" คู่กับ allow_credentials=True พร้อมกัน)
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== เสิร์ฟหน้าเว็บ (index.html) จาก backend ตัวเดียวกันเลย =====
# ไม่ต้องแยกไป deploy frontend ที่ Netlify/Vercel อีกเว็บหนึ่ง — เข้า URL ของ Render
# ตรงๆก็เห็นหน้าตาแอปได้เลย (index.html ต้องอยู่โฟลเดอร์เดียวกับ main.py)
@app.get("/")
def serve_frontend():
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    return FileResponse(index_path)

# Service Worker ต้องเสิร์ฟจาก root scope ("/sw.js") ถึงจะควบคุมทั้งเว็บได้
# (ถ้าอยู่ใต้โฟลเดอร์ย่อยจะควบคุมได้แค่โฟลเดอร์นั้น ไม่ครอบคลุมทั้งแอป)
@app.get("/sw.js")
def serve_service_worker():
    sw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sw.js")
    return FileResponse(sw_path, media_type="application/javascript")

STOCKS_TO_SCAN = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AMD", "INTC",
    "AVGO", "QCOM", "TXN", "MU", "SMCI", "ARM", "ASML", "NFLX", "ADBE", "ORCL",
    "CRM", "CSCO", "ACN", "IBM", "LRCX", "AMAT", "ADI", "PANW", "SNPS", "CDNS",
    "KLAC", "APH", "FTNT", "PLTR", "NOW", "WDAY", "ROP", "ANSS", "TEAM", "MDB",
    "DDOG", "NET", "OKTA", "ZS", "CRWD", "SE", "SHOP", "PINS", "SNAP", "TWLO",
    "DOCU", "ZM", "U", "STNE", "AFRM", "HOOD", "BRK-B", "JPM", "BAC", "WFC",
    "MS", "GS", "V", "MA", "PYPL", "COIN", "AXP", "BLK", "C", "SCHW",
    "SPGI", "MMC", "CB", "PGR", "MDLZ", "AON", "TRV", "ALL", "MET", "PRU"
]

# ========= In-memory TTL cache =========
# เก็บผลลัพธ์ไว้ชั่วคราวเป็น key -> (timestamp_ที่เก็บ, ข้อมูล)
# กัน request ซ้ำๆถี่ๆยิง yfinance รัวจนโดน rate-limit และทำให้ตอบเร็วขึ้นมาก
_cache_store = {}

# หน้าจอสแกนทั้งตลาด (80 ตัว) คำนวณหนัก -> cache นานกว่า
SCREENER_CACHE_TTL = 90   # วินาที
# ดูหุ้นรายตัว เบากว่า -> cache สั้นกว่านิดหน่อยเพื่อความสด
STOCK_CACHE_TTL = 30      # วินาที


def cache_get(key: str):
    entry = _cache_store.get(key)
    if entry is None:
        return None
    saved_at, data, ttl = entry
    if time.time() - saved_at > ttl:
        # หมดอายุแล้ว ลบทิ้งกันหน่วยความจำบวม
        _cache_store.pop(key, None)
        return None
    return data


def cache_set(key: str, data, ttl: int):
    _cache_store[key] = (time.time(), data, ttl)


# ========================================

@app.get("/get-all-stocks")
async def get_all_stocks():
    cache_key = "get-all-stocks"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    semaphore = asyncio.Semaphore(30)

    async def fetch_stock_price(ticker):
        async with semaphore:
            try:
                loop = asyncio.get_event_loop()
                stock = yf.Ticker(ticker)

                df = await loop.run_in_executor(
                    None,
                    lambda: stock.history(period="3mo", interval="1d")
                )

                if df.empty:
                    return None

                analysis = calculate_levels_and_signal(df)

                return {
                    "ticker": ticker,
                    "current_price": analysis["current_price"],
                    "supports": analysis["supports"],
                    "signal": analysis["signal"]
                }

            except Exception:
                return None

    tasks = [fetch_stock_price(ticker) for ticker in STOCKS_TO_SCAN]
    results = await asyncio.gather(*tasks)
    stocks = [r for r in results if r is not None]

    result = {
        "stocks": stocks
    }
    cache_set(cache_key, result, SCREENER_CACHE_TTL)
    return result

def calculate_rsi(close, period=14):
    """RSI (Relative Strength Index) มาตรฐาน ใช้ Wilder's smoothing (ewm alpha=1/period)"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # เคสขอบที่ทำให้เกิดหารด้วยศูนย์ (ไม่ใช่ "ข้อมูลไม่พอ" แต่เป็นเคสจริงที่ค่าสูตรพื้นฐานหารด้วย 0):
    # 1) ไม่มีวันขาดทุนเลยในช่วงคำนวณ (avg_loss = 0) -> ตามธรรมเนียม RSI มาตรฐาน = 100 (overbought สุดขั้ว)
    # 2) ราคาไม่ขยับเลยทั้งช่วง (avg_gain = 0 และ avg_loss = 0) -> ถือเป็นกลาง (neutral) = 50
    rsi = rsi.where(avg_loss != 0, 100.0)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    rsi = rsi.where(~both_zero, 50.0)

    return rsi


def calculate_macd(close, fast=12, slow=26, signal=9):
    """MACD มาตรฐาน: เส้น MACD (EMA fast - EMA slow), เส้น Signal (EMA ของ MACD), และ Histogram"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def series_to_json_list(series, ndigits=2):
    """แปลง pandas Series เป็น list ที่ปลอดภัยสำหรับ JSON (NaN -> None)"""
    return [round(float(x), ndigits) if pd.notna(x) else None for x in series]


def calculate_swing_levels(df, current_price, max_levels=3):
    """
    หา Swing High / Swing Low แบบ fractal: จุดที่ high/low เป็นจุดสูงสุด/ต่ำสุดจริง
    เมื่อเทียบกับแท่งก่อนหน้า-หลังในช่วง window ที่กำหนด (นี่คือนิยามมาตรฐานของ
    แนวรับ-แนวต้านในทางเทคนิคคอลจริงๆ ต่างจากสูตร ±1%/2% แบบเดิมที่เป็นแค่ค่าประมาณการ)

    คืนค่า (supports, resistances) เป็นราคาที่ "เคยเกิดขึ้นจริง" เท่านั้น
    เรียงจากใกล้ราคาปัจจุบันไปไกลสุด ถ้าหาไม่เจอเลยจะคืน list ว่างเปล่า (ให้ผู้เรียก fallback เอง)
    """
    n = len(df)
    window = max(2, min(5, n // 20))  # ปรับขนาดหน้าต่างตามจำนวนแท่งที่มี กันสัญญาณรบกวนตอนข้อมูลเยอะ/น้อยเกินไป
    if n < window * 2 + 1:
        return [], []

    highs = df['High'].tolist()
    lows = df['Low'].tolist()

    swing_highs = set()
    swing_lows = set()
    for i in range(window, n - window):
        segment_high = highs[i - window:i + window + 1]
        segment_low = lows[i - window:i + window + 1]
        if highs[i] == max(segment_high):
            swing_highs.add(round(highs[i], 2))
        if lows[i] == min(segment_low):
            swing_lows.add(round(lows[i], 2))

    # แนวต้าน: เอา swing high ที่อยู่ "เหนือ" ราคาปัจจุบัน เรียงจากใกล้สุดก่อน
    resistances = sorted([h for h in swing_highs if h > current_price])[:max_levels]
    # แนวรับ: เอา swing low ที่อยู่ "ใต้" ราคาปัจจุบัน เรียงจากใกล้สุดก่อน แล้วค่อยเรียงใหม่จากน้อยไปมาก
    supports = sorted([l for l in swing_lows if l < current_price], reverse=True)[:max_levels]
    supports = sorted(supports)

    return supports, resistances


def build_support_resistance(df, last_close, max_high, min_low, max_levels=3):
    """
    รวม swing-based levels (ของจริง) เข้ากับ fallback แบบ ±% เดิม (ใช้เฉพาะตอนหา swing
    point จริงไม่ครบ 3 ระดับ เช่น หุ้นทำจุดสูงสุด/ต่ำสุดใหม่ต่อเนื่องจนไม่มี pullback ให้เห็น)
    รับประกันว่าจะได้ 3 ระดับเสมอเพื่อให้หน้าเว็บแสดงผลได้เหมือนเดิม

    คืนค่าเพิ่ม supports_is_real / resistances_is_real (list ของ True/False ตำแหน่งตรงกับ
    supports/resistances) บอกว่าค่าไหนเป็นราคาที่เคยเกิดขึ้นจริง ค่าไหนเป็นแค่ค่าประมาณการ ±%
    """
    supports, resistances = calculate_swing_levels(df, last_close, max_levels)

    # เติมแนวต้านให้ครบ 3 ถ้า swing high จริงไม่พอ (fallback: ขยายจากราคาสูงสุดจริง +2% ต่อระดับ)
    if not resistances:
        resistances = [round(max_high, 2)]  # max_high เองก็เป็นราคาจริง เลยยังนับว่า "จริง" อยู่
    resistances_real_count = len(resistances)
    while len(resistances) < max_levels:
        resistances.append(round(resistances[-1] * 1.02, 2))

    # เติมแนวรับให้ครบ 3 ถ้า swing low จริงไม่พอ (fallback: ขยายจากราคาต่ำสุดจริง -2% ต่อระดับ)
    if not supports:
        supports = [round(min_low, 2)]  # min_low เองก็เป็นราคาจริง เลยยังนับว่า "จริง" อยู่
    supports_real_count = len(supports)
    while len(supports) < max_levels:
        supports.insert(0, round(supports[0] * 0.98, 2))

    resistances_is_real = [True] * resistances_real_count + [False] * (len(resistances) - resistances_real_count)
    # แนวรับ: ของจริงถูกเก็บไว้ตอนท้าย list เพราะค่า fallback จะถูก insert ไว้ข้างหน้าเสมอ
    supports_is_real = [False] * (len(supports) - supports_real_count) + [True] * supports_real_count

    return supports, resistances, supports_is_real, resistances_is_real


def calculate_levels_and_signal(df):
    close = df['Close'].dropna()
    high = df['High'].dropna()
    low = df['Low'].dropna()

    # เคสเดียวที่ไม่มีราคาจริงให้แสดงเลย: ไม่มีข้อมูลราคาปิดหลงเหลืออยู่สักแถวเดียว
    if close.empty:
        return {
            "supports": ["-"],
            "resistances": ["-"],
            "supports_is_real": [],
            "resistances_is_real": [],
            "signal": "HOLD",
            "current_price": 0,
            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None
        }

    last_close = float(close.iloc[-1])
    max_high = float(high.max()) if not high.empty else last_close
    min_low = float(low.min()) if not low.empty else last_close

    # ===== แนวรับ-แนวต้าน: ใช้ Swing High/Low จริงเป็นหลัก (ราคาที่เคยเกิดขึ้นจริง)
    # เติมด้วยสูตร ±% เดิมเฉพาะตอนหา swing point จริงไม่ครบ 3 ระดับ =====
    supports, resistances, supports_is_real, resistances_is_real = build_support_resistance(
        df, last_close, max_high, min_low
    )

    # หุ้นที่เพิ่งเข้าตลาดใหม่ (เช่น เพิ่ง IPO ไม่ถึง 20 วันทำการ) จะมีประวัติราคาไม่พอ
    # คำนวณ EMA/RSI/MACD ให้น่าเชื่อถือได้ -> แสดงราคาจริง + แนวรับ/แนวต้านตามที่มีข้อมูล
    # แต่ยอมรับตรงๆว่ายังฟันธงสัญญาณซื้อ/ขายหรือ indicator ไม่ได้ แทนที่จะเซ็ตราคาเป็น 0 ทั้งหมด
    if len(close) < 20:
        return {
            "supports": supports,
            "resistances": resistances,
            "supports_is_real": supports_is_real,
            "resistances_is_real": resistances_is_real,
            "signal": "HOLD",
            "current_price": round(last_close, 2),
            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None
        }

    # ===== ใช้ EMA TF 1D ตัดสิน BUY/HOLD =====
    ema10 = close.ewm(span=10, adjust=False).mean().iloc[-1]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]

    # BUY เมื่อ EMA10 > EMA20
    if ema10 > ema20:
        signal = "BUY"
    else:
        signal = "HOLD"

    # ===== RSI / MACD (ตัวชี้วัดเสริม แสดงผลอย่างเดียว ไม่กระทบสัญญาณ BUY/HOLD เดิม) =====
    rsi_series = calculate_rsi(close)
    macd_line, signal_line, histogram = calculate_macd(close)

    rsi_latest = rsi_series.iloc[-1] if len(rsi_series) else None
    macd_latest = macd_line.iloc[-1] if len(macd_line) else None
    macd_signal_latest = signal_line.iloc[-1] if len(signal_line) else None
    macd_hist_latest = histogram.iloc[-1] if len(histogram) else None

    return {
        "supports": supports,
        "resistances": resistances,
        "supports_is_real": supports_is_real,
        "resistances_is_real": resistances_is_real,
        "signal": signal,
        "current_price": round(last_close, 2),
        "rsi": round(float(rsi_latest), 2) if pd.notna(rsi_latest) else None,
        "macd": round(float(macd_latest), 4) if pd.notna(macd_latest) else None,
        "macd_signal": round(float(macd_signal_latest), 4) if pd.notna(macd_signal_latest) else None,
        "macd_histogram": round(float(macd_hist_latest), 4) if pd.notna(macd_hist_latest) else None
    }

# --- Helper สำหรับดึงหุ้นสาย BUY ---
async def fetch_single_stock_buy(ticker, semaphore):
    async with semaphore:
        try:
            loop = asyncio.get_event_loop()
            stock = yf.Ticker(ticker)
            df = await loop.run_in_executor(None, lambda: stock.history(period="3mo", interval="1d"))

            if df.empty or len(df) < 20:
                return None

            analysis = calculate_levels_and_signal(df)

            # คัดเฉพาะตัวที่เป็นสัญญาณ BUY (EMA10 > EMA20) เท่านั้น
            if analysis["signal"] == "BUY":
                return {
                    "ticker": ticker,
                    "current_price": analysis["current_price"],
                    "supports": analysis["supports"],
                    "resistances": analysis["resistances"]
                }
        except Exception:
            pass
        return None

# --- Helper ตัวใหม่สำหรับดึงหุ้นสาย HOLD (ออมยาว / รับโซนล่าง) ---
async def fetch_single_stock_hold(ticker, semaphore):
    async with semaphore:
        try:
            loop = asyncio.get_event_loop()
            stock = yf.Ticker(ticker)
            # ดึงข้อมูลรายวัน D1 ย้อนหลัง 3 เดือน เพื่อความแม่นยำในการคำนวณแนวรับ
            df = await loop.run_in_executor(None, lambda: stock.history(period="3mo", interval="1d"))

            if df.empty or len(df) < 20:
                return None

            analysis = calculate_levels_and_signal(df)

            # คัดเฉพาะตัวที่มีสัญญาณเป็น HOLD (EMA10 <= EMA20) ซึ่งเป็นช่วงพักฐานหรือหุ้นราคาถูกโซนแนวรับ
            if analysis["signal"] == "HOLD":
                return {
                    "ticker": ticker,
                    "current_price": analysis["current_price"],
                    "supports": analysis["supports"],
                    "resistances": analysis["resistances"]
                }
        except Exception:
            pass
        return None

@app.get("/stock/{ticker}")
def get_stock_data(ticker: str, tf: str = "1d"):
    ticker_upper = ticker.upper()
    cache_key = f"stock:{ticker_upper}:{tf}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        stock = yf.Ticker(ticker_upper)

        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "1d": "1d"
        }

        period_map = {
            "1m": "1d", "5m": "5d", "15m": "5d", "30m": "30d",
            "1h": "30d", "1d": "3mo"
        }

        target_tf = interval_map.get(tf, "1d")
        target_period = period_map.get(tf, "3mo")

        df = stock.history(period=target_period, interval=target_tf)

        if df.empty:
            return {"error": f"ไม่พบข้อมูลสำหรับ TF {tf}"}

        analysis = calculate_levels_and_signal(df)
        chart_data = [round(x, 2) for x in df['Close'].tolist()]

        # ===== ข้อมูลเต็มช่วงเวลา สำหรับวาดกราฟ RSI/MACD ใต้กราฟราคาหลัก =====
        close_full = df['Close'].dropna()
        rsi_full = calculate_rsi(close_full)
        macd_full, macd_signal_full, macd_hist_full = calculate_macd(close_full)

        if "m" in target_tf or "h" in target_tf:
            chart_dates = [date.strftime('%m-%d %H:%M') for date in df.index]
        else:
            chart_dates = [date.strftime('%Y-%m-%d') for date in df.index]

        result = {
            "ticker": ticker_upper,
            "current_price": analysis["current_price"],
            "signal": f'{analysis["signal"]} (TF: 1D)',
            "supports": analysis["supports"],
            "resistances": analysis["resistances"],
            "supports_is_real": analysis.get("supports_is_real", []),
            "resistances_is_real": analysis.get("resistances_is_real", []),
            "chart_data": chart_data,
            "chart_dates": chart_dates,
            "rsi": analysis["rsi"],
            "macd": analysis["macd"],
            "macd_signal": analysis["macd_signal"],
            "macd_histogram": analysis["macd_histogram"],
            "rsi_data": series_to_json_list(rsi_full, 2),
            "macd_data": series_to_json_list(macd_full, 4),
            "macd_signal_data": series_to_json_list(macd_signal_full, 4),
            "macd_histogram_data": series_to_json_list(macd_hist_full, 4)
        }
        cache_set(cache_key, result, STOCK_CACHE_TTL)
        return result
    except Exception as e:
        return {"error": str(e)}

# ========= ข่าวหุ้นรายตัว =========
NEWS_CACHE_TTL = 300  # วินาที (ข่าวไม่ได้เปลี่ยนถี่เท่าราคา เลย cache ได้นานกว่า)

@app.get("/stock/{ticker}/news")
def get_stock_news(ticker: str):
    ticker_upper = ticker.upper()
    cache_key = f"news:{ticker_upper}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        stock = yf.Ticker(ticker_upper)
        raw_news = stock.news or []

        news_list = []
        for item in raw_news[:10]:
            try:
                # โครงสร้างข้อมูลข่าวจาก yfinance เปลี่ยนไปมาตามเวอร์ชัน
                # บางเวอร์ชันข้อมูลอยู่ตรงๆ บางเวอร์ชันซ้อนอยู่ใต้ key "content"
                content = item.get("content", item)

                title = content.get("title") or item.get("title")
                if not title:
                    continue

                publisher = (
                    content.get("provider", {}).get("displayName")
                    if isinstance(content.get("provider"), dict)
                    else item.get("publisher")
                ) or "ไม่ทราบสำนักข่าว"

                link = (
                    content.get("canonicalUrl", {}).get("url")
                    if isinstance(content.get("canonicalUrl"), dict)
                    else item.get("link")
                ) or ""

                pub_date = content.get("pubDate") or item.get("providerPublishTime")

                news_list.append({
                    "title": title,
                    "publisher": publisher,
                    "link": link,
                    "published_at": str(pub_date) if pub_date else None
                })
            except Exception:
                continue

        result = {"news": news_list}
        cache_set(cache_key, result, NEWS_CACHE_TTL)
        return result
    except Exception as e:
        return {"error": str(e), "news": []}


# 🔵 โหมดเดิม: สแกนหาหุ้นสัญญาณ BUY เท่านั้น
@app.get("/screener")
async def run_screener():
    cache_key = "screener:buy"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    semaphore = asyncio.Semaphore(40)
    tasks = [fetch_single_stock_buy(ticker, semaphore) for ticker in STOCKS_TO_SCAN]
    results = await asyncio.gather(*tasks)
    buy_list = [r for r in results if r is not None]

    result = {"buy_list": buy_list}
    cache_set(cache_key, result, SCREENER_CACHE_TTL)
    return result

# 🟢 โหมดใหม่: แก้ปัญหา 404 สแกนหาหุ้นสัญญาณ HOLD (ออมระยะยาวบน D1)
@app.get("/screener-hold")
async def run_screener_hold():
    cache_key = "screener:hold"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    semaphore = asyncio.Semaphore(40)
    tasks = [fetch_single_stock_hold(ticker, semaphore) for ticker in STOCKS_TO_SCAN]
    results = await asyncio.gather(*tasks)
    hold_list = [r for r in results if r is not None]

    result = {"hold_list": hold_list}
    cache_set(cache_key, result, SCREENER_CACHE_TTL)
    return result

# ============================================================
# ระบบแจ้งเตือนแบบ real-time (ทำงานได้แม้ไม่ได้เปิดแอปอยู่)
# ใช้ Web Push Notification + ตัวเช็คราคาอัตโนมัติที่รันอยู่บน server ตลอดเวลา
# ============================================================

# VAPID keys สำหรับ Web Push (คู่กุญแจเข้ารหัสเฉพาะของแอปนี้ ไม่ใช่ความลับส่วนตัวของผู้ใช้)
# หมายเหตุ: private key ควรเก็บเป็น environment variable ตอน deploy จริงจัง
# แต่สำหรับแอปผู้ใช้คนเดียวแบบนี้ hardcode ไว้ก็ใช้งานได้ปลอดภัยเพียงพอ
VAPID_PUBLIC_KEY = os.environ.get(
    "VAPID_PUBLIC_KEY",
    "BLd29jExwUWZ037xjzPjuosgO6zAgZh6tAb-A44jD931IrpNleKpbsy-zwji6fPav_chEKOFRUNMsbS4R5lYIKA"
)
VAPID_PRIVATE_KEY = os.environ.get(
    "VAPID_PRIVATE_KEY",
    "1jIvdGM6ajukgVoTN8t8Y1P8NIvOoqC3uFdCQ1cUnNI"
)
VAPID_CLAIMS = {"sub": "mailto:notify@tickr-app.local"}

# เก็บ alert / push subscription ไว้ในหน่วยความจำ (เรียบง่ายสำหรับแอปผู้ใช้คนเดียว)
# ⚠️ ข้อจำกัดสำคัญ: ข้อมูลจะหายไปถ้า Render restart/redeploy เพราะยังไม่ได้ต่อ database จริง
# ถ้าอยากให้อยู่ถาวรข้าม restart ต้องเปลี่ยนไปเก็บใน database (เช่น Render Postgres ฟรี)
server_alerts = []          # [{id, kind, ticker, condition, price, targetSignal}, ...]
push_subscriptions = []     # [subscription_dict, ...] จาก browser PushManager


@app.get("/push/vapid-public-key")
def get_vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/push/subscribe")
async def subscribe_push(request: Request):
    sub = await request.json()
    # กันเพิ่มซ้ำถ้าเบราว์เซอร์เดิม subscribe มาหลายรอบ (เทียบจาก endpoint ซึ่ง unique ต่ออุปกรณ์/เบราว์เซอร์)
    existing_endpoints = [s.get("endpoint") for s in push_subscriptions]
    if sub.get("endpoint") not in existing_endpoints:
        push_subscriptions.append(sub)
    return {"status": "subscribed", "total_subscriptions": len(push_subscriptions)}


@app.post("/alerts")
async def create_server_alert(request: Request):
    data = await request.json()
    if not data.get("id"):
        data["id"] = f"{data.get('ticker', 'UNKNOWN')}-{int(time.time() * 1000)}"
    # กันตั้งซ้ำถ้า id เดิมมีอยู่แล้ว (เผื่อ frontend ยิงซ้ำ)
    server_alerts[:] = [a for a in server_alerts if a.get("id") != data["id"]]
    server_alerts.append(data)
    return {"status": "created", "alert": data}


@app.get("/alerts")
def list_server_alerts():
    return {"alerts": server_alerts}


@app.delete("/alerts/{alert_id}")
def delete_server_alert(alert_id: str):
    server_alerts[:] = [a for a in server_alerts if a.get("id") != alert_id]
    return {"status": "deleted"}


def send_push_to_all(title: str, body: str):
    """ส่ง Web Push ไปทุกอุปกรณ์ที่เคย subscribe ไว้ ถ้า subscription ไหนหมดอายุ/ถูกยกเลิกจะลบทิ้งอัตโนมัติ"""
    still_valid = []
    for sub in push_subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=dict(VAPID_CLAIMS)
            )
            still_valid.append(sub)
        except WebPushException:
            # subscription หมดอายุ หรือผู้ใช้ปิดสิทธิ์แจ้งเตือนไปแล้ว -> เอาออกจากลิสต์เงียบๆ
            continue
    push_subscriptions[:] = still_valid


def check_server_alerts_job():
    """ทำงานเป็นระยะๆบน server เอง (ไม่ต้องพึ่งเบราว์เซอร์เปิดอยู่) เช็ค alert ทุกตัวที่ตั้งไว้"""
    if not server_alerts or not push_subscriptions:
        return

    try:
        tickers_needed = list({a["ticker"] for a in server_alerts if a.get("ticker")})
        analysis_map = {}

        for ticker in tickers_needed:
            try:
                stock = yf.Ticker(ticker)
                df = stock.history(period="3mo", interval="1d")
                if df.empty:
                    continue
                analysis_map[ticker] = (calculate_levels_and_signal(df), df)
            except Exception:
                continue

        remaining = []
        for alert in server_alerts:
            ticker = alert.get("ticker")
            entry = analysis_map.get(ticker)
            if not entry:
                remaining.append(alert)
                continue
            analysis, df = entry

            triggered = False
            message = ""
            price = analysis["current_price"]
            kind = alert.get("kind")

            if kind == "price":
                condition = alert.get("condition")
                target_price = alert.get("price")
                cond_text = "ต่ำกว่า" if condition == "below" else "สูงกว่า"
                if condition == "below" and price <= target_price:
                    triggered = True
                elif condition == "above" and price >= target_price:
                    triggered = True
                if triggered:
                    message = f"{ticker} ถึงเงื่อนไขแล้ว: ราคาปัจจุบัน ${price} (ตั้งไว้ {cond_text} ${target_price})"

            elif kind == "signal":
                target_signal = alert.get("targetSignal")
                if analysis["signal"] == target_signal:
                    triggered = True
                    message = f"{ticker} เปลี่ยนสัญญาณเป็น {analysis['signal']} แล้ว!"

            elif kind == "breakout":
                # ใช้นิยาม breakout ที่ตรงไปตรงมาที่สุด: ราคาปิดวันนี้ทะลุจุดสูงสุด/ต่ำสุด
                # ของ "ทุกวันก่อนหน้า" ในช่วงที่ดูอยู่ (ไม่รวมวันนี้เอง) แทนที่จะอิงจาก index
                # ของ array แนวรับ-แนวต้าน ซึ่งอาจเป็นค่าจริงหรือค่าประมาณการปนกันไม่แน่นอน
                if len(df) > 1:
                    prior_high = float(df['High'].iloc[:-1].max())
                    prior_low = float(df['Low'].iloc[:-1].min())
                    if price > prior_high:
                        triggered = True
                        message = f"{ticker} ทะลุจุดสูงสุดเดิมแล้ว! ราคา ${price} (จุดสูงสุดก่อนหน้า ${round(prior_high, 2)})"
                    elif price < prior_low:
                        triggered = True
                        message = f"{ticker} หลุดจุดต่ำสุดเดิมแล้ว! ราคา ${price} (จุดต่ำสุดก่อนหน้า ${round(prior_low, 2)})"

            if triggered:
                send_push_to_all(f"🔔 {ticker}", message)
            else:
                remaining.append(alert)

        server_alerts[:] = remaining
    except Exception as e:
        print("check_server_alerts_job error:", repr(e))


# ตัวเช็คอัตโนมัติทำงานทุก 5 นาที บน server เอง ไม่ต้องพึ่งเบราว์เซอร์เปิดค้างไว้เลย
scheduler = BackgroundScheduler()
scheduler.add_job(check_server_alerts_job, "interval", minutes=5)
scheduler.start()


if __name__ == "__main__":
    import uvicorn
    import os
    # Render (และ hosting อื่นๆส่วนใหญ่) จะกำหนด PORT มาทาง environment variable
    # ถ้ารันบนเครื่องตัวเอง (ไม่มี PORT ตั้งไว้) จะ fallback ไปที่ 8000 เหมือนเดิม
    port = int(os.environ.get("PORT", 8000))
    # host="0.0.0.0" จำเป็นตอน deploy จริง (ต่างจาก "127.0.0.1" ที่รับได้แค่จากเครื่องตัวเอง)
    uvicorn.run(app, host="0.0.0.0", port=port)
