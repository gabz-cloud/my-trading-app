import streamlit as st
import yfinance as yf
import pandas as pd
import streamlit.components.v1 as components

# ==========================================
# 1. ตั้งค่าหน้าเว็บ & โครงสร้างข้อมูล
# ==========================================
st.set_page_config(page_title="AI Pro Stock Tracker ✨", page_icon="✨", layout="wide")

STOCK_CATEGORIES = {
    "🖥️ Technology & AI": {
        "Nvidia (NVDA)": {"yf": "NVDA", "tv": "NASDAQ:NVDA"},
        "Apple (AAPL)": {"yf": "AAPL", "tv": "NASDAQ:AAPL"},
        "Microsoft (MSFT)": {"yf": "MSFT", "tv": "NASDAQ:MSFT"},
        "Intel (INTC)": {"yf": "INTC", "tv": "NASDAQ:INTC"},
        "AMD (AMD)": {"yf": "AMD", "tv": "NASDAQ:AMD"},
        "IBM (IBM)": {"yf": "IBM", "tv": "NYSE:IBM"},
        "ServiceNow (NOW)": {"yf": "NOW", "tv": "NYSE:NOW"},
        "Amazon (AMZN)": {"yf": "AMZN", "tv": "NASDAQ:AMZN"}
    },
    "🛍️ Finance & Consumer": {
        "Visa (V)": {"yf": "V", "tv": "NYSE:V"},
        "Coca-Cola (KO)": {"yf": "KO", "tv": "NYSE:KO"}
    },
    "🏆 Commodities & ETFs": {
        "Gold Spot / ทองคำ (XAUUSD)": {"yf": "GC=F", "tv": "OANDA:XAUUSD"},
        "Barrick Gold Corp (GOLD)": {"yf": "GOLD", "tv": "NYSE:GOLD"},
        "SPCX ETF (SPCX)": {"yf": "SPCX", "tv": "AMEX:SPCX"}
    }
}

# ==========================================
# 2. แถบเมนูด้านซ้าย (Sidebar)
# ==========================================
st.sidebar.markdown("<div style='text-align: center; font-size: 60px; margin-bottom: 15px;'>✨</div>", unsafe_allow_html=True)
st.sidebar.markdown("### ⚙️ ตั้งค่าระบบ")

theme_mode = st.sidebar.radio("🌗 โหมดการแสดงผล:", ["Light Mode ☀️", "Dark Mode 🌙"], horizontal=True)

st.sidebar.markdown("---")

selected_category = st.sidebar.selectbox("📂 หมวดหมู่:", list(STOCK_CATEGORIES.keys()))
selected_stock_name = st.sidebar.selectbox("📊 เลือกสินทรัพย์:", list(STOCK_CATEGORIES[selected_category].keys()))

yf_symbol = STOCK_CATEGORIES[selected_category][selected_stock_name]["yf"]
tv_symbol = STOCK_CATEGORIES[selected_category][selected_stock_name]["tv"]

tf_options = {
    "1 นาที": {"yf": "1m", "tv": "1"},
    "5 นาที": {"yf": "5m", "tv": "5"},
    "15 นาที": {"yf": "15m", "tv": "15"},
    "30 นาที": {"yf": "30m", "tv": "30"},
    "1 ชั่วโมง": {"yf": "1h", "tv": "60"},
    "1 วัน": {"yf": "1d", "tv": "D"}
}

st.sidebar.markdown("**⏱️ Timeframe วิเคราะห์:**")
selected_tf = st.sidebar.radio("", list(tf_options.keys()), index=2, label_visibility="collapsed")
interval_yf = tf_options[selected_tf]["yf"]
interval_tv = tf_options[selected_tf]["tv"]

# จัดการข้อจำกัดการดึงข้อมูลตาม Timeframe ของ yfinance
if interval_yf == "1m":
    period = "5d" 
elif interval_yf in ["5m", "15m", "30m", "1h"]:
    period = "60d" 
else:
    period = "1y" 

st.sidebar.markdown("---")
st.sidebar.markdown("""
**💡 เคล็ดลับ:**
- ขาลง = เทรด SELL (Short)
- ขาขึ้น = เทรด BUY (Long)
- คำนวณ TP/SL อัตโนมัติ
""")

# ==========================================
# 3. Dynamic CSS - Premium Styling
# ==========================================
if "Light" in theme_mode:
    tv_theme = "light"
    bg_gradient = "linear-gradient(135deg, #FFF5F9 0%, #F0F4FF 50%, #FFF0F5 100%)"
    text_color = "#4A4A4A"
    title_gradient = "linear-gradient(135deg, #FF6B9D 0%, #C44569 50%, #FF6B9D 100%)"
    card_bg = "linear-gradient(135deg, rgba(255, 182, 193, 0.25), rgba(173, 216, 230, 0.15))"
    card_hover_bg = "linear-gradient(135deg, rgba(255, 182, 193, 0.35), rgba(173, 216, 230, 0.25))"
    card_border = "rgba(255, 107, 157, 0.25)"
    sidebar_bg = "linear-gradient(135deg, rgba(255, 240, 245, 0.98), rgba(240, 255, 240, 0.98))"
    success_bg = "linear-gradient(135deg, rgba(34, 197, 94, 0.12), rgba(52, 211, 153, 0.08))"
    error_bg = "linear-gradient(135deg, rgba(239, 68, 68, 0.12), rgba(248, 113, 113, 0.08))"
    warning_bg = "linear-gradient(135deg, rgba(251, 146, 60, 0.12), rgba(254, 167, 51, 0.08))"
    info_bg = "linear-gradient(135deg, rgba(59, 130, 246, 0.12), rgba(96, 165, 250, 0.08))"
else:
    tv_theme = "dark"
    bg_gradient = "linear-gradient(135deg, #0F172A 0%, #1A1F35 50%, #0D1B2A 100%)"
    text_color = "#E0E7FF"
    title_gradient = "linear-gradient(135deg, #F5A3FF 0%, #A78BFA 50%, #60A5FA 100%)"
    card_bg = "linear-gradient(135deg, rgba(49, 46, 129, 0.3), rgba(30, 58, 138, 0.25))"
    card_hover_bg = "linear-gradient(135deg, rgba(79, 70, 229, 0.4), rgba(59, 130, 246, 0.3))"
    card_border = "rgba(165, 142, 251, 0.35)"
    sidebar_bg = "linear-gradient(135deg, rgba(15, 23, 42, 0.98), rgba(30, 41, 59, 0.98))"
    success_bg = "linear-gradient(135deg, rgba(16, 185, 129, 0.15), rgba(34, 197, 94, 0.1))"
    error_bg = "linear-gradient(135deg, rgba(239, 68, 68, 0.15), rgba(251, 113, 133, 0.1))"
    warning_bg = "linear-gradient(135deg, rgba(251, 146, 60, 0.15), rgba(253, 186, 116, 0.1))"
    info_bg = "linear-gradient(135deg, rgba(59, 130, 246, 0.15), rgba(147, 197, 253, 0.1))"

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800&family=Quicksand:wght@400;600;700&display=swap');
    
    /* ==================== สีพื้นฐาน ==================== */
    .stApp {{ 
        background: {bg_gradient}; 
        color: {text_color}; 
    }}
    
    html, body, [class*="css"] {{ 
        color: {text_color} !important; 
        font-family: 'Poppins', 'Quicksand', sans-serif !important; 
        line-height: 1.6;
    }}

    /* ==================== หัวข้อหลัก ==================== */
    .big-title {{
        font-size: 42px !important;
        font-weight: 800 !important;
        background: {title_gradient} !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        margin-bottom: 28px !important;
        text-align: center;
        letter-spacing: -1px;
        animation: fadeInDown 0.6s ease-out;
    }}
    
    @keyframes fadeInDown {{
        from {{ opacity: 0; transform: translateY(-20px); }}
        to {{ opacity: 1; transform: translateY(0); }}
    }}
    
    /* ==================== Metric Cards ==================== */
    div[data-testid="metric-container"] {{
        background: {card_bg} !important;
        border: 2px solid {card_border} !important;
        padding: 24px !important;
        border-radius: 20px !important;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08) !important;
        transition: all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1) !important;
        backdrop-filter: blur(10px);
        position: relative;
        overflow: hidden;
    }}
    
    div[data-testid="metric-container"]::before {{
        content: '';
        position: absolute;
        inset: 0;
        background: linear-gradient(135deg, transparent 0%, rgba(255,255,255,0.1) 50%, transparent 100%);
        pointer-events: none;
    }}
    
    div[data-testid="metric-container"]:hover {{
        transform: translateY(-8px) scale(1.02) !important;
        background: {card_hover_bg} !important;
        box-shadow: 0 16px 48px rgba(0, 0, 0, 0.12) !important;
    }}
    
    /* ==================== Alert Boxes ==================== */
    .stAlert {{ 
        border-radius: 18px !important; 
        border: 1px solid transparent !important; 
        padding: 20px 24px !important; 
        backdrop-filter: blur(15px);
        font-weight: 500;
    }}
    
    .stSuccess {{
        background: {success_bg} !important;
        border-left: 6px solid #22C55E !important;
        border-radius: 18px !important;
    }}
    
    .stError {{
        background: {error_bg} !important;
        border-left: 6px solid #EF4444 !important;
        border-radius: 18px !important;
    }}
    
    .stWarning {{
        background: {warning_bg} !important;
        border-left: 6px solid #FB923C !important;
        border-radius: 18px !important;
    }}
    
    .stInfo {{
        background: {info_bg} !important;
        border-left: 6px solid #3B82F6 !important;
        border-radius: 18px !important;
    }}
    
    /* ==================== Headings ==================== */
    h2, h3 {{
        background: {title_gradient} !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        font-weight: 700 !important;
        margin-top: 28px !important;
        margin-bottom: 20px !important;
        letter-spacing: -0.5px;
    }}
    
    h3 {{ font-size: 22px !important; }}
    h2 {{ font-size: 24px !important; }}
    
    /* ==================== Sidebar ==================== */
    .stSidebar {{
        background: {sidebar_bg} !important;
        border-right: 2px solid {card_border} !important;
    }}
    
    .stSidebar h3, .stSidebar h2, .stSidebar label {{
        color: {text_color} !important;
    }}
    
    /* ==================== Radio & Select ==================== */
    .stRadio > label, .stSelectbox > label {{
        color: {text_color} !important;
        font-weight: 600 !important;
        font-size: 14px !important;
    }}
    
    /* ==================== Divider ==================== */
    hr {{ 
        border-color: {card_border} !important; 
        margin: 24px 0 !important;
    }}
    
    /* ==================== Button ==================== */
    .stButton > button {{
        background: {title_gradient} !important;
        color: white !important;
        font-weight: 700 !important;
        border-radius: 16px !important;
        border: none !important;
        padding: 12px 28px !important;
        transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1) !important;
    }}
    
    .stButton > button:hover {{
        transform: translateY(-3px) !important;
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.15) !important;
    }}
    
    /* ==================== Scrollbar ==================== */
    ::-webkit-scrollbar {{ width: 10px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ 
        background: {card_border};
        border-radius: 5px;
    }}
    ::-webkit-scrollbar-thumb:hover {{ opacity: 0.8; }}
    
    /* ==================== Mobile Responsive ==================== */
    @media (max-width: 768px) {{
        .big-title {{
            font-size: 32px !important;
            margin-bottom: 20px !important;
        }}
        
        div[data-testid="metric-container"] {{
            padding: 18px !important;
            border-radius: 16px !important;
            margin-bottom: 10px !important;
        }}
        
        h3 {{ font-size: 18px !important; margin-top: 20px !important; }}
        
        .stTabs [role="tablist"] {{
            gap: 8px !important;
        }}
    }}
    
    @media (max-width: 480px) {{
        .big-title {{
            font-size: 26px !important;
        }}
        
        div[data-testid="metric-container"] {{
            padding: 14px !important;
            border-radius: 14px !important;
        }}
        
        h3 {{ font-size: 16px !important; }}
        
        .stRadio, .stSelectbox {{
            font-size: 13px !important;
        }}
    }}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 4. ฟังก์ชันดึงข้อมูลเบื้องหลัง
# ==========================================
@st.cache_data(ttl=60)
def load_data(ticker, interval, period):
    return yf.Ticker(ticker).history(period=period, interval=interval)

data = load_data(yf_symbol, interval_yf, period)

# ==========================================
# 5. ประมวลผลและแสดงหน้าเว็บ (ลอจิก AI)
# ==========================================
if not data.empty:
    st.markdown(f'<div class="big-title">✨ {selected_stock_name.split(" ")[0]}</div>', unsafe_allow_html=True)
    
    current_price = float(data['Close'].iloc[-1]) 
    
    window = 20
    data['Support'] = data['Low'].rolling(window=window).min()
    data['Resistance'] = data['High'].rolling(window=window).max()
    data['SMA9'] = data['Close'].rolling(window=9).mean()
    data['SMA21'] = data['Close'].rolling(window=21).mean()
    
    current_support = float(data['Support'].iloc[-1])
    current_resistance = float(data['Resistance'].iloc[-1])
    sma9 = float(data['SMA9'].iloc[-1])
    sma21 = float(data['SMA21'].iloc[-1])

    # คำนวณความผันผวน
    volatility = current_resistance - current_support
    if volatility <= 0:
        volatility = current_price * 0.005

    # ลอจิก AI ตัดสินใจเทรด
    if sma9 > sma21 and current_price >= sma21:
        signal = "🟢 แนะนำ BUY (Uptrend)"
        trend = "📈 ขาขึ้น"
        status_color = "success"
        action = "BUY"
        
        entry_price = sma9 if current_price > sma9 else current_price
        take_profit = entry_price + (volatility * 0.7)
        stop_loss = entry_price - (volatility * 0.35)

    elif sma9 < sma21 and current_price <= sma21:
        signal = "🔴 แนะนำ SELL (Downtrend)"
        trend = "📉 ขาลง"
        status_color = "error"
        action = "SELL"
        
        entry_price = sma9 if current_price < sma9 else current_price
        take_profit = entry_price - (volatility * 0.7)
        stop_loss = entry_price + (volatility * 0.35)

    else:
        signal = "🟡 รอชาญฉลาด (Sideways)"
        trend = "↔️ ออกข้าง"
        status_color = "warning"
        action = "HOLD"
        
        entry_price = current_price
        take_profit = current_resistance
        stop_loss = current_support

    # ==========================================
    # ส่วนที่ 1: ราคา REAL-TIME & แนวโน้ม
    # ==========================================
    ticker_widget_html = f"""
    <div class="tradingview-widget-container">
      <div class="tradingview-widget-container__widget"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-single-quote.js" async>
      {{
      "symbol": "{tv_symbol}",
      "width": "100%",
      "colorTheme": "{tv_theme}",
      "isTransparent": true,
      "locale": "th_TH"
    }}
      </script>
    </div>
    """
    
    col_price, col_trend = st.columns([3, 1])
    with col_price:
        components.html(ticker_widget_html, height=120)
    with col_trend:
        st.metric(label="สถานะ", value=trend.split(" ")[0], delta=trend.split(" ")[1], delta_color="normal" if "ขึ้น" in trend else "inverse")

    st.markdown("")

    # ==========================================
    # ส่วนที่ 2: AI วิเคราะห์แผนการเทรด 
    # ==========================================
    st.markdown(f"### 🤖 แผนการเทรด ({selected_tf})")
    
    if status_color == "success":
        st.success(f"✅ {signal}")
    elif status_color == "error":
        st.error(f"❌ {signal}")
    else:
        st.warning(f"⚠️ {signal}")

    plan_col1, plan_col2, plan_col3 = st.columns(3)
    
    if action == "BUY":
        plan_col1.info(f"**🛒 Entry (BUY)**\n\n${entry_price:.2f}")
        plan_col2.success(f"**💰 TP (กำไร)**\n\n${take_profit:.2f}")
        plan_col3.error(f"**🛑 SL (ขาดทุน)**\n\n${stop_loss:.2f}")
    elif action == "SELL":
        plan_col1.info(f"**🔻 Entry (SELL)**\n\n${entry_price:.2f}")
        plan_col2.success(f"**💰 TP (กำไร)**\n\n${take_profit:.2f}")
        plan_col3.error(f"**🛑 SL (ขาดทุน)**\n\n${stop_loss:.2f}")
    else:
        plan_col1.info(f"**📉 Support**\n\n${stop_loss:.2f}")
        plan_col2.success(f"**ราคาปัจจุบัน**\n\n${entry_price:.2f}")
        plan_col3.error(f"**📈 Resistance**\n\n${take_profit:.2f}")

    st.markdown("")

    # ==========================================
    # ส่วนที่ 3: ข้อมูลทางเทคนิค
    # ==========================================
    st.markdown("### 📊 ข้อมูลสรุป")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(label="ราคาปัจจุบัน", value=f"${current_price:.2f}")
    col2.metric(label="Support", value=f"${current_support:.2f}")
    col3.metric(label="Resistance", value=f"${current_resistance:.2f}")
    col4.metric(label="SMA 9", value=f"${sma9:.2f}")

    st.markdown("")

    # ==========================================
    # ส่วนที่ 4: กราฟ TRADINGVIEW
    # ==========================================
    st.markdown("### 📈 วิเคราะห์เชิงลึก")
    
    tradingview_html = f"""
    <div class="tradingview-widget-container" style="height:100%;width:100%">
      <div id="tradingview_{tv_symbol.replace(':', '_')}" style="height:500px;width:100%"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
      <script type="text/javascript">
      new TradingView.widget(
      {{
      "autosize": true,
      "symbol": "{tv_symbol}",
      "interval": "{interval_tv}",
      "timezone": "Asia/Bangkok",
      "theme": "{tv_theme}",
      "style": "1",
      "locale": "th_TH",
      "enable_publishing": false,
      "allow_symbol_change": true,
      "studies": [
        "STD;MACD",
        "STD;SMA"
      ],
      "container_id": "tradingview_{tv_symbol.replace(':', '_')}"
    }}
      );
      </script>
    </div>
    """
    
    components.html(tradingview_html, height=500)

else:
    st.error("❌ ไม่สามารถดึงข้อมูลได้ โปรดตรวจสอบการเชื่อมต่อ")