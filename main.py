import asyncio
import time
import os
import json
import requests
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

# เอาไว้เช็คตรงๆว่า yfinance ส่งข้อมูลราคานอกเวลาตลาดกลับมาให้จริงไหม
# เข้า URL นี้ตรงๆในเบราว์เซอร์ได้เลย เช่น /debug/extended-hours/AAPL
@app.get("/debug/extended-hours/{ticker}")
def debug_extended_hours(ticker: str):
    ticker_upper = ticker.upper()
    result = {"ticker": ticker_upper}

    try:
        stock = yf.Ticker(ticker_upper)
        info = stock.info
        keys_to_check = [
            "marketState", "preMarketPrice", "preMarketChange", "preMarketChangePercent",
            "postMarketPrice", "postMarketChange", "postMarketChangePercent",
            "regularMarketPrice", "regularMarketTime"
        ]
        # ค้นหาฟิลด์ไหนก็ตามที่ชื่อมีคำว่า "overnight"/"extended"/"night" ปนอยู่ (ไม่รู้ชื่อฟิลด์แน่ชัด
        # เลยลองกรองแบบกว้างๆ ดูว่า yfinance เก็บข้อมูลเซสชัน "Overnight" ที่เห็นบนเว็บ Yahoo ไว้ที่ไหนบ้างไหม)
        possible_overnight_keys = {
            k: v for k, v in info.items()
            if any(word in k.lower() for word in ["overnight", "extended", "postmarket", "premarket"])
        }
        result["yfinance"] = {
            "info_fetch_succeeded": True,
            "relevant_fields": {k: info.get(k) for k in keys_to_check},
            "possible_overnight_related_fields": possible_overnight_keys,
            "total_fields_in_info": len(info),
            "all_field_names": sorted(info.keys())
        }
    except Exception as e:
        result["yfinance"] = {"info_fetch_succeeded": False, "error": repr(e)}

    finnhub_result = fetch_finnhub_price(ticker_upper)
    result["finnhub"] = {
        "api_key_configured": bool(FINNHUB_API_KEY),
        "result": finnhub_result
    }

    if finnhub_result and isinstance(finnhub_result.get("quote_timestamp"), (int, float)):
        qt = finnhub_result["quote_timestamp"]
        age = round(time.time() - qt)
        result["finnhub"]["quote_time_readable_utc"] = datetime.utcfromtimestamp(qt).strftime("%Y-%m-%d %H:%M:%S UTC")
        result["finnhub"]["quote_age_seconds"] = age
        result["finnhub"]["note"] = (
            "ถ้า quote_age_seconds มีค่าสูงมาก (หลายนาที/ชั่วโมงขึ้นไป) แปลว่าราคานี้ไม่ใช่ราคาสด "
            "เป็นราคาเทรดล่าสุดที่ Finnhub มีอยู่ ซึ่งอาจเก่ากว่าที่คิด (เช่น ราคาปิดตลาดปกติ ไม่ใช่ราคานอกเวลาจริง)"
        )

    result["calculated_market_state"] = calculate_market_state()
    result["note"] = "แอปตอนนี้ใช้ calculated_market_state (คำนวณเอง ไม่พึ่ง yfinance) เป็นตัวตัดสินจริง ไม่ใช่ yfinance.marketState ด้านบนอีกต่อไป"

    return result

# ============================================================
# คำนวณสถานะตลาดหุ้นสหรัฐฯ (PRE/REGULAR/POST/CLOSED) จากเวลาปัจจุบันโดยตรง
# ไม่ต้องพึ่ง API ไหนเลย (เดิมพึ่ง yfinance.info ซึ่งไม่เสถียร โดน rate limit บ่อย)
# อ้างอิงเวลาทำการมาตรฐานของ NYSE/Nasdaq เท่านั้น ไม่ได้เช็ควันหยุดพิเศษของตลาด
# (ตรงกับหลักการเดียวกับ badge สถานะตลาดฝั่ง frontend)
# ============================================================
from datetime import datetime
from zoneinfo import ZoneInfo


def calculate_market_state():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    weekday = now_et.weekday()  # จันทร์=0 ... อาทิตย์=6
    minutes = now_et.hour * 60 + now_et.minute

    if weekday >= 5:  # เสาร์-อาทิตย์: ตลาดปิดสนิทจริงๆ ไม่มี ECN ให้ราคาต่อเนื่อง ไม่ลองดึงเลย
        return "CLOSED"

    REGULAR_START = 9 * 60 + 30   # 09:30
    REGULAR_END = 16 * 60          # 16:00
    PRE_LABEL_START = 4 * 60       # 04:00 — จุดเปลี่ยน label จาก "หลังตลาดปิด" เป็น "ก่อนตลาดเปิด"

    # หมายเหตุสำคัญ: เดิมมี "ช่วงหลุมดำ" ระหว่าง 20:00-04:00 ET ที่ถือว่า CLOSED ไปเลย
    # ทั้งที่จริงๆราคาอาจยังขยับได้ต่อเนื่อง (เทรดนอกเวลาบางส่วนยังเปิดถึงดึก/ข้ามคืนได้)
    # ตอนนี้แก้ให้ครอบคลุมทั้งวันจันทร์-ศุกร์ที่ไม่ใช่เวลาตลาดหลัก พยายามดึงราคาให้เสมอ
    # แล้วปล่อยให้ Finnhub เป็นคนตัดสินว่ามีราคาจริงให้ไหม (ถ้าไม่มีก็แค่ไม่โชว์แถวนี้เฉยๆ)
    if REGULAR_START <= minutes < REGULAR_END:
        return "REGULAR"
    elif PRE_LABEL_START <= minutes < REGULAR_START:
        return "PRE"
    else:
        return "POST"  # ครอบคลุม 16:00 วันนี้ ถึง 04:00 วันถัดไป (รวมช่วงดึก/ข้ามคืนทั้งหมด)

# ============================================================
# Finnhub: ใช้เป็นแหล่งราคานอกเวลาตลาด (ก่อนเปิด/หลังปิด) แทน yfinance
# เพราะ preMarketPrice/postMarketPrice ของ yfinance เองไม่เสถียร (เป็นปัญหาที่รู้จักกันดี)
# ต้องตั้ง environment variable FINNHUB_API_KEY บน Render ก่อนถึงจะใช้งานได้
# ถ้าไม่ได้ตั้งไว้ ฟีเจอร์นี้จะปิดเงียบๆ ไม่กระทบข้อมูลหลักของแอปเลย
# ============================================================
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"


def fetch_finnhub_price(ticker: str):
    """
    ดึงราคาล่าสุดจาก Finnhub (โดยทั่วไปรวมราคาจากการเทรดนอกเวลาตลาดด้วยสำหรับหุ้นสหรัฐฯ)
    คืนค่า None ถ้ายังไม่ได้ตั้ง FINNHUB_API_KEY ไว้ หรือดึงข้อมูลไม่สำเร็จ
    """
    if not FINNHUB_API_KEY:
        return None
    try:
        resp = requests.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()

        current_price = data.get("c")
        prev_close = data.get("pc")
        quote_timestamp = data.get("t")  # Unix epoch วินาที ของเวลาที่เกิดการเทรดล่าสุดจริงตาม Finnhub

        # Finnhub คืนค่า 0 ทุกฟิลด์เวลาหา ticker ไม่เจอ หรือ token ผิด แทนที่จะ error ตรงๆ
        if not isinstance(current_price, (int, float)) or current_price == 0:
            return None

        change = None
        change_percent = None
        if isinstance(prev_close, (int, float)) and prev_close != 0:
            change = round(current_price - prev_close, 2)
            change_percent = round((change / prev_close) * 100, 2)

        return {
            "price": round(float(current_price), 2),
            "change": change,
            "change_percent": change_percent,
            "quote_timestamp": quote_timestamp
        }
    except Exception as e:
        print(f"[finnhub] {ticker}: ดึงข้อมูลไม่สำเร็จ -> {repr(e)}")
        return None


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
# เก็บผลลัพธ์ไว้ชั่วคราวเป็น key -> (timestamp_ที่เก็บ, ข้อมูล, ttl)
# กัน request ซ้ำๆถี่ๆยิง yfinance รัวจนโดน rate-limit และทำให้ตอบเร็วขึ้นมาก
#
# ยืดเวลา cache ให้นานขึ้นเป็น 2 นาที (จากเดิม 30-90 วิ) เพื่อลดความถี่ในการยิง
# yfinance ลงอีก ช่วยลดความเสี่ยงโดน rate limit ซ้ำในอนาคต
_cache_store = {}

SCREENER_CACHE_TTL = 120  # วินาที (2 นาที) — หน้าจอสแกนทั้งตลาด (80 ตัว)
STOCK_CACHE_TTL = 120     # วินาที (2 นาที) — ดูหุ้นรายตัว


def cache_get(key: str, allow_stale: bool = False):
    """
    allow_stale=False (ปกติ): คืนค่าเฉพาะข้อมูลที่ยังไม่หมดอายุ (ตรงเวลา TTL) เหมือนเดิม
    allow_stale=True: คืนค่าแม้หมดอายุไปแล้วก็ตาม — ใช้เป็น "ทางสำรองฉุกเฉิน" ตอนดึงข้อมูลสดไม่สำเร็จ
    (เช่น yfinance โดน rate limit) ดีกว่าไม่มีอะไรให้แสดงเลย
    """
    entry = _cache_store.get(key)
    if entry is None:
        return None
    saved_at, data, ttl = entry
    is_expired = (time.time() - saved_at) > ttl
    if is_expired and not allow_stale:
        return None
    return data


def cache_set(key: str, data, ttl: int):
    _cache_store[key] = (time.time(), data, ttl)


def cache_age_seconds(key: str):
    """อายุของข้อมูลใน cache ตอนนี้ (วินาที) เอาไว้บอกผู้ใช้ว่าข้อมูลเก่าไปแล้วกี่นาที ถ้าต้องใช้ fallback"""
    entry = _cache_store.get(key)
    if entry is None:
        return None
    saved_at, data, ttl = entry
    return time.time() - saved_at


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
                    "signal": analysis["signal"],
                    "change": analysis.get("change"),
                    "change_percent": analysis.get("change_percent")
                }

            except Exception:
                return None

    tasks = [fetch_stock_price(ticker) for ticker in STOCKS_TO_SCAN]
    results = await asyncio.gather(*tasks)
    stocks = [r for r in results if r is not None]

    # ถ้าดึงสดไม่สำเร็จเลยแทบทั้งหมด (เช่น yfinance โดน rate limit ทั้งชุด)
    # ลองใช้ข้อมูลเก่าที่เคยสำเร็จไว้แทน ดีกว่าปล่อยให้หน้าเว็บว่างเปล่าไม่มีอะไรให้ดูเลย
    if len(stocks) < 5:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and stale.get("stocks"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy

    result = {
        "stocks": stocks,
        "is_stale": False
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


def calculate_true_range(df):
    """True Range มาตรฐาน: ค่าที่มากสุดของ (high-low), |high-prev_close|, |low-prev_close|"""
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def calculate_atr(df, period=14):
    """
    ATR (Average True Range) — วัดความผันผวนจริงของหุ้นตัวนั้นๆ ใช้กำหนดจุด stop-loss/take-profit
    ที่ "เหมาะกับหุ้นตัวนี้จริง" แทนเปอร์เซ็นต์ตายตัว (หุ้นผันผวนสูงควรมีระยะ stop กว้างกว่าหุ้นนิ่ง)
    """
    tr = calculate_true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def calculate_adx(df, period=14):
    """
    ADX (Average Directional Index) — วัด "ความแข็งแกร่งของเทรนด์" ไม่สนใจทิศทาง
    ค่าต่ำ (< 20) = ตลาด sideways ไม่มีทิศทางชัดเจน สัญญาณ BUY/HOLD ช่วงนี้เชื่อถือได้น้อย
    ค่าสูง (>= 20-25) = มีเทรนด์จริง สัญญาณน่าเชื่อถือกว่า
    """
    high = df['High']
    low = df['Low']

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = calculate_true_range(df)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)

    di_sum = (plus_di + minus_di).replace(0, np.nan)  # กัน division by zero ตอนไม่มีการเคลื่อนไหวเลย
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    return adx, plus_di, minus_di


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
            "change": None,
            "change_percent": None,
            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None
        }

    last_close = float(close.iloc[-1])
    max_high = float(high.max()) if not high.empty else last_close
    min_low = float(low.min()) if not low.empty else last_close

    # ===== เปลี่ยนแปลงจากราคาปิดวันก่อนหน้า (เทียบกับแท่งก่อนหน้าล่าสุดใน timeframe ที่ดูอยู่) =====
    if len(close) >= 2:
        prev_close = float(close.iloc[-2])
        change = round(last_close - prev_close, 2)
        change_percent = round((change / prev_close) * 100, 2) if prev_close != 0 else 0.0
    else:
        change = None
        change_percent = None

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
            "change": change,
            "change_percent": change_percent,
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
        "change": change,
        "change_percent": change_percent,
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
                    "resistances": analysis["resistances"],
                    "change": analysis.get("change"),
                    "change_percent": analysis.get("change_percent")
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
                    "resistances": analysis["resistances"],
                    "change": analysis.get("change"),
                    "change_percent": analysis.get("change_percent")
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
            # ไม่มีข้อมูลกลับมาเลย มักเป็นเพราะดึงจาก yfinance ไม่สำเร็จ (เช่นโดน rate limit)
            # ลองใช้ข้อมูลเก่าที่เคยสำเร็จของหุ้นตัวนี้แทน ดีกว่าโชว์ error เฉยๆ
            stale = cache_get(cache_key, allow_stale=True)
            if stale is not None and not stale.get("error"):
                stale_copy = dict(stale)
                stale_copy["is_stale"] = True
                stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
                return stale_copy
            return {"error": f"ไม่พบข้อมูลสำหรับ TF {tf}"}

        analysis = calculate_levels_and_signal(df)
        chart_data = [round(x, 2) for x in df['Close'].tolist()]

        # ===== ราคานอกเวลาตลาด (ก่อนเปิด/หลังปิด) — ดึงเฉพาะตอนดูหุ้นรายตัวเท่านั้น
        # ไม่ใส่ในโหมดสแกนทั้งตลาด (80 ตัว) เพราะยิ่งดึงมากตัวยิ่งช้าและเสี่ยง rate limit สูงขึ้น
        #
        # หมายเหตุ: เดิมใช้ yfinance (.info) บอกว่าตอนนี้อยู่ช่วง PRE/POST หรือเปล่า แต่พบว่า
        # yfinance โดน rate limit บ่อย ทำให้ทั้งฟีเจอร์นี้ใช้งานไม่ได้ไปด้วยทั้งที่ปัญหาจริงๆ
        # อยู่ที่ยืนแค่ "รู้เวลา" เท่านั้น — เปลี่ยนมาคำนวณช่วงเวลาเองจากเวลาปัจจุบันแทน
        # (ไม่ต้องพึ่ง API ไหนเลยสำหรับส่วนนี้) แล้วให้ Finnhub รับผิดชอบเรื่องราคาอย่างเดียว
        # ผลคือฟีเจอร์นี้ไม่ขึ้นกับความเสถียรของ yfinance อีกต่อไป =====
        market_state = calculate_market_state()
        pre_market_price = None
        pre_market_change = None
        pre_market_change_percent = None
        post_market_price = None
        post_market_change = None
        post_market_change_percent = None
        extended_hours_source = None
        extended_hours_quote_timestamp = None
        extended_hours_quote_age_seconds = None

        if market_state == "PRE":
            finnhub_data = fetch_finnhub_price(ticker_upper)
            if finnhub_data:
                pre_market_price = finnhub_data["price"]
                pre_market_change = finnhub_data["change"]
                pre_market_change_percent = finnhub_data["change_percent"]
                extended_hours_source = "finnhub"
                extended_hours_quote_timestamp = finnhub_data.get("quote_timestamp")
                if isinstance(extended_hours_quote_timestamp, (int, float)):
                    extended_hours_quote_age_seconds = round(time.time() - extended_hours_quote_timestamp)
            print(f"[extended-hours] {ticker_upper}: state=PRE (คำนวณเอง), finnhub={finnhub_data}")
        elif market_state == "POST":
            finnhub_data = fetch_finnhub_price(ticker_upper)
            if finnhub_data:
                post_market_price = finnhub_data["price"]
                post_market_change = finnhub_data["change"]
                post_market_change_percent = finnhub_data["change_percent"]
                extended_hours_source = "finnhub"
                extended_hours_quote_timestamp = finnhub_data.get("quote_timestamp")
                if isinstance(extended_hours_quote_timestamp, (int, float)):
                    extended_hours_quote_age_seconds = round(time.time() - extended_hours_quote_timestamp)
            print(f"[extended-hours] {ticker_upper}: state=POST (คำนวณเอง), finnhub={finnhub_data}")

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
            "change": analysis.get("change"),
            "change_percent": analysis.get("change_percent"),
            "signal": f'{analysis["signal"]} (TF: 1D)',
            "supports": analysis["supports"],
            "resistances": analysis["resistances"],
            "supports_is_real": analysis.get("supports_is_real", []),
            "resistances_is_real": analysis.get("resistances_is_real", []),
            "market_state": market_state,
            "pre_market_price": pre_market_price,
            "pre_market_change": pre_market_change,
            "pre_market_change_percent": pre_market_change_percent,
            "post_market_price": post_market_price,
            "post_market_change": post_market_change,
            "post_market_change_percent": post_market_change_percent,
            "extended_hours_source": extended_hours_source,
            "extended_hours_quote_timestamp": extended_hours_quote_timestamp,
            "extended_hours_quote_age_seconds": extended_hours_quote_age_seconds,
            "chart_data": chart_data,
            "chart_dates": chart_dates,
            "rsi": analysis["rsi"],
            "macd": analysis["macd"],
            "macd_signal": analysis["macd_signal"],
            "macd_histogram": analysis["macd_histogram"],
            "rsi_data": series_to_json_list(rsi_full, 2),
            "macd_data": series_to_json_list(macd_full, 4),
            "macd_signal_data": series_to_json_list(macd_signal_full, 4),
            "macd_histogram_data": series_to_json_list(macd_hist_full, 4),
            "is_stale": False
        }
        cache_set(cache_key, result, STOCK_CACHE_TTL)
        return result
    except Exception as e:
        # ดึงข้อมูลไม่สำเร็จเลย (เช่น yfinance rate limit) -> ลองใช้ข้อมูลเก่าที่เคยสำเร็จแทน
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and not stale.get("error"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy
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


# ========= จุดเข้า-ออก (Entry/Exit Analysis) =========
# รวมหลายเงื่อนไขยืนยันกัน (ไม่ใช่แค่ EMA crossover เดี่ยวๆ) เพื่อลดสัญญาณหลอก
# และคำนวณจุด stop-loss/take-profit จาก ATR ให้รู้จุดออกล่วงหน้าชัดเจน ไม่ต้องรอสัญญาณ lag
ENTRY_EXIT_INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d"}
ENTRY_EXIT_PERIOD_MAP = {"1m": "1d", "5m": "5d", "15m": "5d", "30m": "30d", "1h": "30d", "1d": "6mo"}


@app.get("/entry-exit/{ticker}")
def analyze_entry_exit(ticker: str, tf: str = "1d"):
    ticker_upper = ticker.upper()
    if tf not in ENTRY_EXIT_INTERVAL_MAP:
        tf = "1d"

    cache_key = f"entry-exit:{ticker_upper}:{tf}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    target_tf = ENTRY_EXIT_INTERVAL_MAP[tf]
    target_period = ENTRY_EXIT_PERIOD_MAP[tf]

    try:
        stock = yf.Ticker(ticker_upper)
        df = stock.history(period=target_period, interval=target_tf)

        if df.empty or len(df) < 25:
            return {"error": f"ข้อมูลไม่พอสำหรับวิเคราะห์ที่ TF {tf} (ต้องการอย่างน้อย ~25 แท่ง)"}

        close = df['Close'].dropna()
        current_price = float(close.iloc[-1])

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        trend_bullish = bool(ema10.iloc[-1] > ema20.iloc[-1])

        adx, plus_di, minus_di = calculate_adx(df)
        adx_value = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None
        trend_strong = adx_value is not None and adx_value >= 20

        atr_series = calculate_atr(df)
        atr_value = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None

        rsi_series = calculate_rsi(close)
        rsi_value = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else None
        rsi_not_overbought = rsi_value is not None and rsi_value < 70

        _, _, histogram = calculate_macd(close)
        macd_hist_value = float(histogram.iloc[-1]) if pd.notna(histogram.iloc[-1]) else None
        macd_bullish = macd_hist_value is not None and macd_hist_value > 0

        volume_ok = None
        if 'Volume' in df.columns:
            volume = df['Volume'].dropna()
            if len(volume) >= 20:
                avg_volume_20 = float(volume.iloc[-20:].mean())
                current_volume = float(volume.iloc[-1])
                if avg_volume_20 > 0:
                    volume_ok = current_volume > avg_volume_20

        conditions = [
            {"key": "trend", "name": "เทรนด์ขาขึ้น (EMA10 > EMA20)", "met": trend_bullish},
            {"key": "trend_strength", "name": "เทรนด์แข็งแกร่งพอ (ADX ≥ 20)", "met": trend_strong},
            {"key": "rsi", "name": "RSI ยังไม่ overbought (< 70)", "met": rsi_not_overbought},
            {"key": "macd", "name": "โมเมนตัม MACD เป็นบวก", "met": macd_bullish},
        ]
        if volume_ok is not None:
            conditions.append({"key": "volume", "name": "ปริมาณซื้อขายสูงกว่าค่าเฉลี่ย 20 แท่ง", "met": volume_ok})

        met_count = sum(1 for c in conditions if c["met"])
        total_count = len(conditions)

        if met_count == total_count:
            entry_recommendation = "เข้าซื้อได้ - สัญญาณแข็งแกร่งครบทุกเงื่อนไข"
            entry_strength = "strong"
        elif met_count >= total_count - 1:
            entry_recommendation = "พอเข้าซื้อได้ - สัญญาณส่วนใหญ่สนับสนุน"
            entry_strength = "moderate"
        elif met_count >= total_count / 2:
            entry_recommendation = "ยังไม่ชัดเจน - สัญญาณผสมกัน ควรรอดูก่อน"
            entry_strength = "weak"
        else:
            entry_recommendation = "ยังไม่ควรเข้า - สัญญาณส่วนใหญ่ไม่สนับสนุน"
            entry_strength = "none"

        # จุด stop-loss / take-profit อิงจาก ATR (ความผันผวนจริงของหุ้นตัวนี้)
        # ใช้ 2×ATR เป็นระยะตัดขาดทุน, 3×ATR เป็นเป้าทำกำไร (risk:reward ~1:1.5 เป็นอย่างต่ำ)
        # ให้รู้จุดออกล่วงหน้าชัดเจนตั้งแต่ก่อนเข้า ไม่ต้องรอสัญญาณ lag แบบ EMA crossover เพียงอย่างเดียว
        stop_loss = None
        take_profit = None
        risk_reward_ratio = None
        if atr_value is not None and atr_value > 0:
            stop_loss = round(current_price - 2 * atr_value, 2)
            take_profit = round(current_price + 3 * atr_value, 2)
            risk = current_price - stop_loss
            reward = take_profit - current_price
            if risk > 0:
                risk_reward_ratio = round(reward / risk, 2)

        result = {
            "ticker": ticker_upper,
            "timeframe": tf,
            "current_price": round(current_price, 2),
            "entry_recommendation": entry_recommendation,
            "entry_strength": entry_strength,
            "conditions_met": met_count,
            "conditions_total": total_count,
            "conditions": conditions,
            "adx": round(adx_value, 2) if adx_value is not None else None,
            "rsi": round(rsi_value, 2) if rsi_value is not None else None,
            "atr": round(atr_value, 2) if atr_value is not None else None,
            "suggested_stop_loss": stop_loss,
            "suggested_take_profit": take_profit,
            "risk_reward_ratio": risk_reward_ratio,
            "is_stale": False
        }

        cache_set(cache_key, result, STOCK_CACHE_TTL)
        return result

    except Exception as e:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and not stale.get("error"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy
        return {"error": str(e)}


# ========= Backtest: จำลองสัญญาณ BUY/HOLD เดิม (EMA10 ตัด EMA20) กับข้อมูลราคาย้อนหลัง =========
# เพื่อวัดว่าสัญญาณที่แอปใช้อยู่ตอนนี้ "แม่น" แค่ไหนในอดีต ก่อนจะปรับปรุงอะไรเพิ่ม
BACKTEST_CACHE_TTL = 3600  # 1 ชั่วโมง (ข้อมูลย้อนหลังไม่เปลี่ยนบ่อยในระยะสั้น)
VALID_BACKTEST_PERIODS = {"3mo", "6mo", "1y", "2y"}


@app.get("/backtest/{ticker}")
def run_backtest(ticker: str, period: str = "1y"):
    ticker_upper = ticker.upper()
    if period not in VALID_BACKTEST_PERIODS:
        period = "1y"

    cache_key = f"backtest:{ticker_upper}:{period}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        stock = yf.Ticker(ticker_upper)
        df = stock.history(period=period, interval="1d")

        if df.empty or len(df) < 25:
            return {"error": "ข้อมูลไม่พอสำหรับทดสอบย้อนหลัง (ต้องการอย่างน้อย ~25 วันทำการ)"}

        close = df['Close'].dropna()
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        is_buy_signal = ema10 > ema20  # True = BUY, False = HOLD (สูตรเดียวกับที่แอปใช้จริง)

        dates = close.index
        trades = []
        holding = False
        entry_price = None
        entry_date = None

        # เดินไล่ทีละวัน: เข้าซื้อตอนสัญญาณเปลี่ยนเป็น BUY, ขายตอนสัญญาณเปลี่ยนกลับเป็น HOLD
        for i in range(len(close)):
            sig_buy = bool(is_buy_signal.iloc[i])
            price = float(close.iloc[i])
            date_str = dates[i].strftime('%Y-%m-%d')

            if not holding and sig_buy:
                holding = True
                entry_price = price
                entry_date = date_str
            elif holding and not sig_buy:
                exit_price = price
                return_pct = round((exit_price - entry_price) / entry_price * 100, 2)
                trades.append({
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "exit_date": date_str,
                    "exit_price": round(exit_price, 2),
                    "return_percent": return_pct,
                    "is_win": return_pct > 0
                })
                holding = False
                entry_price = None
                entry_date = None

        # ถ้ายังถือ position ค้างอยู่ตอนจบช่วงเวลาที่ทดสอบ ไม่นับเป็น trade ที่จบแล้ว
        # แต่รายงานแยกไว้ต่างหากว่า "ยังเปิดอยู่ กำไร/ขาดทุนล่าสุดเท่าไหร่"
        open_position = None
        if holding:
            last_price = float(close.iloc[-1])
            unrealized_pct = round((last_price - entry_price) / entry_price * 100, 2)
            open_position = {
                "entry_date": entry_date,
                "entry_price": round(entry_price, 2),
                "current_price": round(last_price, 2),
                "unrealized_return_percent": unrealized_pct
            }

        total_trades = len(trades)
        win_count = sum(1 for t in trades if t["is_win"])
        loss_count = total_trades - win_count
        win_rate = round((win_count / total_trades) * 100, 1) if total_trades > 0 else None
        avg_return = round(sum(t["return_percent"] for t in trades) / total_trades, 2) if total_trades > 0 else None

        # ผลตอบแทนรวมแบบทบต้น: เข้า-ออกตามสัญญาณต่อเนื่องกันไปเรื่อยๆ
        strategy_multiplier = 1.0
        for t in trades:
            strategy_multiplier *= (1 + t["return_percent"] / 100)
        strategy_total_return = round((strategy_multiplier - 1) * 100, 2)

        # เทียบกับ "ซื้อแล้วถือยาวเฉยๆ" ในช่วงเวลาเดียวกัน (baseline มาตรฐานที่ใช้เทียบกลยุทธ์เสมอ)
        first_close = float(close.iloc[0])
        last_close_price = float(close.iloc[-1])
        buy_hold_return = round((last_close_price - first_close) / first_close * 100, 2)

        result = {
            "ticker": ticker_upper,
            "period": period,
            "data_points": len(close),
            "total_trades": total_trades,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate_percent": win_rate,
            "avg_return_percent": avg_return,
            "strategy_total_return_percent": strategy_total_return,
            "buy_hold_return_percent": buy_hold_return,
            "open_position": open_position,
            "trades": trades
        }

        cache_set(cache_key, result, BACKTEST_CACHE_TTL)
        return result

    except Exception as e:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and not stale.get("error"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy
        return {"error": str(e)}


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

    # ปกติหุ้น BUY ควรเจอได้บ้าง ถ้าได้ 0 ตัวเป๊ะๆ (ทั้งที่ก่อนหน้านี้เคยเจอ) น่าจะเป็นเพราะดึงข้อมูลไม่สำเร็จมากกว่า
    if len(buy_list) == 0:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and stale.get("buy_list"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy

    result = {"buy_list": buy_list, "is_stale": False}
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

    if len(hold_list) == 0:
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None and stale.get("hold_list"):
            stale_copy = dict(stale)
            stale_copy["is_stale"] = True
            stale_copy["stale_age_seconds"] = round(cache_age_seconds(cache_key) or 0)
            return stale_copy

    result = {"hold_list": hold_list, "is_stale": False}
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
