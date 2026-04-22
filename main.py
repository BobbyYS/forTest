import os
import smtplib
import pandas as pd
import numpy as np
import yfinance as yf
import twstock
import requests
import gspread
import time
from tqdm import tqdm
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta

# ==========================================
# ⚙️ 使用者設定區
# ==========================================
MY_PORTFOLIO = {
    '4939.TW': {'cost': 51.2, 'stop_loss_pct': 0.07},
    '3346.TW': {'cost': 50.8, 'stop_loss_pct': 0.07},
    '2492.TW': {'cost': 133.5, 'stop_loss_pct': 0.07},
    '2317.TW': {'cost': 227.2, 'stop_loss_pct': 0.07}
}

GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

class StockSystem:
    def __init__(self):
        self.bench_ticker = '0050.TW'
        self.min_price = 5
        self.min_volume_chose = 800000
        self.min_volume_drive = 1000000
        self.rs_period_chose = 20
        self.rs_period_drive = 60

    def get_benchmark_roc(self, period):
        try:
            bench = yf.download(self.bench_ticker, period='1y', progress=False, auto_adjust=True)
            close = bench['Close'].iloc[:, 0] if isinstance(bench['Close'], pd.DataFrame) else bench['Close']
            return float(close.pct_change(period).iloc[-1])
        except: return 0

    def health_check_logic(self, ticker, name, data, df):
        try:
            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            curr = float(close.iloc[-1])
            cost, init_risk_pct = data['cost'], data['stop_loss_pct']
            init_risk_amt = cost * init_risk_pct
            pnl_amt = curr - cost
            r_multiple = pnl_amt / init_risk_amt
            ma10 = float(close.rolling(10).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            action, reason = "✅ 續抱", []
            hard_stop = cost * (1 - init_risk_pct)
            if curr < hard_stop:
                action = "🛑 清倉賣出(停損)"; reason.append(f"跌破初始停損 {round(hard_stop, 2)}")
            elif r_multiple >= 2:
                if curr < cost: action = "🛑 清倉賣出(保本)"; reason.append("獲利回吐觸及成本")
                else: reason.append(f"達2R({round(r_multiple,1)}R)啟動保本")
            is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
            check_ma = ma10 if is_super else ma20
            if curr < check_ma:
                action = "⚠️ 警戒/賣出"; reason.append(f"跌破{'10MA' if is_super else '20MA'}")
            
            return {
                "代號": ticker, "名稱": name, "現價": round(curr, 2), 
                "獲利(R)": f"{round(r_multiple, 1)}R", "建議動作": action, 
                "防守價": round(max(hard_stop, check_ma), 2), "原因": " | ".join(reason),
                "created_at": datetime.now()
            }
        except: return None

    def analyze_chose(self, ticker, name, df, bench_roc):
        try:
            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            high = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
            vol = df['Volume'].iloc[:, 0] if isinstance(df['Volume'], pd.DataFrame) else df['Volume']
            open_p = df['Open'].iloc[:, 0] if isinstance(df['Open'], pd.DataFrame) else df['Open']
            curr, avg_vol = float(close.iloc[-1]), float(vol.rolling(20).mean().iloc[-1])
            if curr < self.min_price or avg_vol < self.min_volume_chose: return None
            ma50, ma200 = float(close.rolling(50).mean().iloc[-1]), float(close.rolling(200).mean().iloc[-1])
            if not (curr > ma50 > ma200): return None
            stock_roc = float(close.pct_change(self.rs_period_chose).iloc[-1])
            rs_rating = (stock_roc - bench_roc) * 100
            if rs_rating < 0: return None
            year_high, prev_20_high = float(high.iloc[-250:].max()), float(high.iloc[-21:-1].max())
            is_breakout = (curr > prev_20_high) and (close.iloc[-2] < prev_20_high)
            setup, reason = "", ""
            rally = (high.iloc[-60:].max() - close.iloc[-60:].min())/close.iloc[-60:].min()
            if rally > 0.8 and (year_high-curr)/year_high < 0.25 and is_breakout:
                setup, reason = "🚀 高窄旗型", "飆漲動能突破"
            elif (open_p.iloc[-1] - close.iloc[-2])/close.iloc[-2] > 0.08:
                setup, reason = "🕳️ 買進跳空", "強力消息缺口"
            elif is_breakout and (year_high - curr)/year_high < 0.15:
                setup, reason = "📦 VCP突破", "整理區帶量突破"
            if setup:
                return {
                    "代號": ticker, "名稱": name, "現價": round(curr, 2), "型態": setup, 
                    "RS": round(rs_rating, 1), "建議買價": round(prev_20_high, 2), 
                    "買入原因": reason, "created_at": datetime.now()
                }
            return None
        except: return None

    def analyze_drive(self, item, df, bench_roc):
        try:
            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            high = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
            vol = df['Volume'].iloc[:, 0] if isinstance(df['Volume'], pd.DataFrame) else df['Volume']
            curr, avg_vol = float(close.iloc[-1]), float(vol.rolling(20).mean().iloc[-1])
            if curr < self.min_price or avg_vol < self.min_volume_drive: return None
            ma50, ma200 = float(close.rolling(50).mean().iloc[-1]), float(close.rolling(200).mean().iloc[-1])
            year_high = float(high.iloc[-250:].max())
            if not (curr > ma50 > ma200 and (year_high - curr)/year_high < 0.25): return None
            stock_roc = float(close.pct_change(self.rs_period_drive).iloc[-1])
            rs_rating = (stock_roc - bench_roc) * 100
            if rs_rating < 5: return None
            up_days = (close.iloc[-16:-1].diff() > 0).sum()
            vol_ratio = vol.iloc[-16:-1].mean() / vol.iloc[-31:-16].mean()
            is_mvp = up_days >= 9 and vol_ratio >= 1.2
            score, comments = 0, []
            prev_20_high = float(close.iloc[-21:-1].max())
            if curr > prev_20_high and vol.iloc[-1] > avg_vol * 1.3:
                score += 50; comments.append("樞紐突破")
            if is_mvp: score += 30; comments.append("🔥MVP吸籌")
            if rs_rating > 30: score += 20; comments.append("超強RS")
            if score >= 30:
                return {
                    "代號": item['ticker'], "名稱": item['name'], "產業": item['industry'], 
                    "評分": score, "RS": round(rs_rating, 1), "吸籌特徵": " + ".join(comments),
                    "created_at": datetime.now()
                }
            return None
        except: return None

    def run(self):
        codes = twstock.codes
        all_stocks = [{'ticker': c+('.TW' if r.market=='上市' else '.TWO'), 'name': r.name, 'industry': r.group} for c,r in codes.items() if r.type=='股票']
        bench_c, bench_d = self.get_benchmark_roc(20), self.get_benchmark_roc(60)
        res_h, res_c, res_d = [], [], []
        print(f"🚀 全力掃描 {len(all_stocks)} 檔標的...")
        for item in tqdm(all_stocks):
            try:
                df = yf.download(item['ticker'], period='1y', progress=False, auto_adjust=True)
                if df.empty or len(df) < 200: continue
                if item['ticker'] in MY_PORTFOLIO:
                    h = self.health_check_logic(item['ticker'], item['name'], MY_PORTFOLIO[item['ticker']], df)
                    if h: res_h.append(h)
                c = self.analyze_chose(item['ticker'], item['name'], df, bench_c)
                if c: res_c.append(c)
                d = self.analyze_drive(item, df, bench_d)
                if d: res_d.append(d)
            except: continue
        return res_h, res_c, res_d

# ==========================================
# 📊 輔助引擎與同步邏輯
# ==========================================
def backtest_3y_strategy(ticker, bench_roc_series):
    try:
        df = yf.download(ticker, period='4y', progress=False, auto_adjust=True)
        if df.empty or len(df) < 300: return 0, 0
        c_series = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
        h_series = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
        o_series = df['Open'].iloc[:, 0] if isinstance(df['Open'], pd.DataFrame) else df['Open']
        v_series = df['Volume'].iloc[:, 0] if isinstance(df['Volume'], pd.DataFrame) else df['Volume']
        ma10, ma20 = c_series.rolling(10).mean(), c_series.rolling(20).mean()
        ma50, ma200, avg_vol_20 = c_series.rolling(50).mean(), c_series.rolling(200).mean(), v_series.rolling(20).mean()
        trades, in_pos, entry_p, init_stop_pct = [], False, 0, 0.07 
        start_idx = len(df) - 750
        for i in range(start_idx, len(df)):
            curr_c, dt = float(c_series.iloc[i]), df.index[i]
            if not in_pos:
                if curr_c < 20 or avg_vol_20.iloc[i] < 800000: continue
                if not (curr_c > ma50.iloc[i] > ma200.iloc[i]): continue
                s_roc = float(c_series.iloc[i] / c_series.iloc[i-20] - 1)
                if (s_roc - bench_roc_series.get(dt, 0)) < 0: continue
                y_high, p20_high = float(h_series.iloc[i-250:i].max()), float(h_series.iloc[i-21:i].max())
                is_break = (curr_c > p20_high) and (c_series.iloc[i-1] < p20_high)
                rally = (h_series.iloc[i-60:i].max() - h_series.iloc[i-60:i].min()) / h_series.iloc[i-60:i].min()
                if (rally > 0.8 and (y_high - curr_c)/y_high < 0.25 and is_break) or \
                   ((o_series.iloc[i] - c_series.iloc[i-1])/c_series.iloc[i-1] > 0.08) or \
                   (is_break and (y_high - curr_c)/y_high < 0.15):
                    entry_p, in_pos = curr_c, True
            elif in_pos:
                r_mult = (curr_c - entry_p) / (entry_p * init_stop_pct)
                is_super = (c_series.iloc[i-34:i+1] > ma10.iloc[i-34:i+1]).all()
                check_ma = ma10.iloc[i] if is_super else ma20.iloc[i]
                if curr_c < entry_p * (1 - init_stop_pct) or (r_mult >= 2 and curr_c < entry_p) or curr_c < check_ma:
                    trades.append((curr_c - entry_p) / entry_p); in_pos = False
        if not trades: return 0, 0
        wr, tr = len([t for t in trades if t > 0]) / len(trades) * 100, (np.prod([1 + t for t in trades]) - 1) * 100
        return round(wr, 1), round(tr, 1)
    except: return 0, 0

def generate_ai_diagnostic_html(row_c, row_d, df, bench_series):
    try:
        close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
        ma10 = round(float(close.rolling(10).mean().iloc[-1]), 2)
        ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2)
        wr, tr = backtest_3y_strategy(row_c['代號'], bench_series)
        is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
        def_ma_n, def_ma_v = ("10MA", ma10) if is_super else ("20MA", ma20)
        return (f"<b>【{row_c['名稱']} ({row_c['代號'].split('.')[0]})】</b><br>"
                f"➡️ 診斷：{row_c['型態']} | 評分：{row_d['評分']}<br>"
                f"📊 回測：勝率 {wr}% | 總報 {tr}%<br>"
                f"🛡️ 防守：{def_ma_n} ({def_ma_v})<br><hr style='border:0.5px dashed #ddd;'>")
    except: return ""

def sync_to_gsheets_and_prepare_reports(h, c, d):
    creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    
    now = datetime.now()
    today_date = now.strftime('%Y-%m-%d')
    now_time = now.strftime('%H:%M:%S')
    time_limit = now - timedelta(minutes=15) 

    bench_df = yf.download('0050.TW', period='4y', progress=False, auto_adjust=True)
    bench_series = (bench_df['Close'].iloc[:, 0] if isinstance(bench_df['Close'], pd.DataFrame) else bench_df['Close']).pct_change(20).to_dict()

    ai_html, ai_tg_data = "", []
    if c and d:
        df_c_full, df_d_full = pd.DataFrame(c), pd.DataFrame(d)
        inter_ids = list(set(df_c_full['代號']) & set(df_d_full['代號']))
        ws_ai = sh.worksheet("雙重認證個股深度分析")
        ai_rows = []
        for tid in inter_ids:
            row_c = df_c_full[df_c_full['代號'] == tid].iloc[0]
            row_d = df_d_full[df_d_full['代號'] == tid].iloc[0]
            wr, tr = backtest_3y_strategy(tid, bench_series)
            ai_rows.append([
                today_date, tid, row_c['名稱'], row_d['產業'], row_c['型態'], 
                f"{wr}%/{tr}%", row_d['吸籌特徵'], row_c['建議買價'], 
                f"停損{round(row_c['建議買價']*0.93, 2)}", "MA防禦", now_time
            ])
            if row_c['created_at'] > time_limit:
                df_t = yf.download(tid, period='4y', progress=False, auto_adjust=True)
                ai_html += generate_ai_diagnostic_html(row_c, row_d, df_t, bench_series)
                ai_tg_data.append(f"• <b>{row_c['名稱']} ({tid})</b>\n  勝率:{wr}% | 總報:{tr}%\n  評分:{row_d['評分']} | {row_c['型態']}")
        if ai_rows: ws_ai.append_rows(ai_rows)

    c_filtered_for_notif = []
    if c:
        c_rows = []
        for i in c:
            c_rows.append([today_date, i['代號'], i['名稱'], i['現價'], i['型態'], i['RS'], i['建議買價'], i['買入原因'], now_time])
            if i['created_at'] > time_limit:
                c_filtered_for_notif.append(i)
        sh.worksheet("買入型態掃描").append_rows(c_rows)

    d_filtered_for_notif = []
    if d:
        d_rows = []
        for i in d:
            d_rows.append([today_date, i['代號'], i['名稱'], i['產業'], i['評分'], i['RS'], i['吸籌特徵'], now_time])
            if i['created_at'] > time_limit:
                d_filtered_for_notif.append(i)
        sh.worksheet("大戶動能評分").append_rows(d_rows)

    return ai_html, ai_tg_data, h, c_filtered_for_notif, d_filtered_for_notif

def send_telegram(ai_tg_list):
    """已調整：若無標的則發送『無標的通知』，有標的則發送『深度診斷內容』"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    now_str = datetime.now().strftime('%m/%d %H:%M')
    
    if not ai_tg_list: 
        # 當沒有標的時，發送告知訊息
        print("沒有新診斷標的(15分鐘內)，發送『無標的』通知")
        msg = f"<b>台股深度診斷報告 ({now_str})</b>\n\n☕ 今日無符合雙重認證之標的。"
    else:
        # 有標的時顯示詳細清單
        msg = f"🚀 <b>台股深度診斷報告 ({now_str})</b>\n\n"
        msg += "💎 <b>雙重認證標的 (最新)</b>\n" + "\n".join(ai_tg_list)
    
    # 執行發送
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"})

def send_email_final(h, c, d, ai_html):
    if not ai_html and not c and not d:
        print("沒有新標的(15分鐘內)，跳過 Email 通知")
        return

    df_h = pd.DataFrame(h).drop(columns=['created_at'], errors='ignore') if h else pd.DataFrame()
    df_c = pd.DataFrame(c).drop(columns=['created_at'], errors='ignore') if c else pd.DataFrame()
    df_d = pd.DataFrame(d).drop(columns=['created_at'], errors='ignore') if d else pd.DataFrame()
    
    top_ind = df_d['產業'].value_counts().head(3).index.tolist() if not df_d.empty else []
    
    style = "<style>body{font-family:sans-serif;line-height:1.6;color:#333;}.title{background:#2c3e50;color:white;padding:12px;margin-top:25px;font-weight:bold;border-radius:5px;}.ai-box{background:#fffcf0;border:1px solid #f1c40f;border-left:6px solid #f1c40f;padding:15px;margin:15px 0;font-size:14px;}.table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:20px;}.table th,.table td{border:1px solid #ddd;padding:10px;text-align:left;}.table th{background-color:#f8f9fa;}</style>"
    html = f"<html><head>{style}</head><body><h2>📈 台股策略報告 (15min內更新)</h2><p>💰 主流板塊：{', '.join(top_ind)}</p>"
    html += "<div class='title'>1. 🏥 庫存健檢 (當前所有)</div>" + (df_h.to_html(classes='table', index=False) if not df_h.empty else "<p>無資料</p>")
    html += "<div class='title' style='background:#8e44ad;'>4. 💎 深度診斷 (最新)</div><div class='ai-box'>" + (ai_html if ai_html else "期間內無雙重認證標的") + "</div>"
    html += "<div class='title'>2. 🚀 買入型態 (最新)</div>" + (df_c.to_html(classes='table', index=False) if not df_c.empty else "<p>無資料</p>")
    html += "<div class='title'>3. 👑 大戶評分 (最新)</div>" + (df_d.to_html(classes='table', index=False) if not df_d.empty else "<p>無資料</p>")
    html += "</body></html>"
    
    msg = MIMEMultipart(); msg['Subject'] = f"台股策略報告 (最新) - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg['From'], msg['To'] = GMAIL_USER, RECEIVER_EMAIL
    msg.attach(MIMEText(html, 'html'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD); s.send_message(msg)
    except Exception as e:
        print(f"Email 發送失敗: {e}")

if __name__ == "__main__":
    system = StockSystem()
    h_res, c_res, d_res = system.run()
    
    ai_h, ai_t_list, h_filt, c_filt, d_filt = sync_to_gsheets_and_prepare_reports(h_res, c_res, d_res)
    
    # 傳送 Telegram (僅深度診斷)
    send_telegram(ai_t_list)
    # 傳送 Email (完整報告)
    send_email_final(h_filt, c_filt, d_filt, ai_h)
    
    print("Mission Accomplished!")
