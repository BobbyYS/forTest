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
# ⚙️ 環境變數設定 (GitHub Secrets)
# ==========================================
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

class StockSystem:
    def __init__(self, portfolio):
        self.portfolio = portfolio
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

    def health_check_logic(self, ticker, name, cost_data, df):
        """史考特賣出法則邏輯"""
        try:
            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            curr = float(close.iloc[-1])
            cost = float(cost_data['cost'])
            init_risk_pct = 0.07 
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
                return {"代號": ticker, "名稱": name, "現價": round(curr, 2), "型態": setup, "RS": round(rs_rating, 1), "建議買價": round(prev_20_high, 2), "買入原因": reason, "created_at": datetime.now()}
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
                return {"代號": item['ticker'], "名稱": item['name'], "產業": item['industry'], "評分": score, "RS": round(rs_rating, 1), "吸籌特徵": " + ".join(comments), "created_at": datetime.now()}
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
                if item['ticker'] in self.portfolio:
                    h = self.health_check_logic(item['ticker'], item['name'], self.portfolio[item['ticker']], df)
                    if h: res_h.append(h)
                c = self.analyze_chose(item['ticker'], item['name'], df, bench_c)
                if c: res_c.append(c)
                d = self.analyze_drive(item, df, bench_d)
                if d: res_d.append(d)
            except: continue
        return res_h, res_c, res_d

# ==========================================
# 📊 輔助邏輯與同步
# ==========================================
def backtest_3y_strategy(ticker, bench_roc_series):
    try:
        df = yf.download(ticker, period='4y', progress=False, auto_adjust=True)
        if df.empty or len(df) < 300: return 0, 0
        c_series = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
        ma10, ma20 = c_series.rolling(10).mean(), c_series.rolling(20).mean()
        trades, in_pos, entry_p, init_stop_pct = [], False, 0, 0.07 
        for i in range(len(df)-750, len(df)):
            curr_c, dt = float(c_series.iloc[i]), df.index[i]
            if not in_pos: # 簡化回測邏輯以滿足速度
                if curr_c > ma10.iloc[i]: entry_p, in_pos = curr_c, True
            elif in_pos:
                if curr_c < entry_p * 0.93 or curr_c < ma20.iloc[i]:
                    trades.append((curr_c - entry_p)/entry_p); in_pos = False
        if not trades: return 0, 0
        wr, tr = len([t for t in trades if t > 0])/len(trades)*100, (np.prod([1+t for t in trades])-1)*100
        return round(wr, 1), round(tr, 1)
    except: return 0, 0

def sync_and_prepare(h, c, d):
    creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    
    now = datetime.now()
    today_date, now_time = now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S')
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
            ai_rows.append([today_date, tid, row_c['名稱'], row_d['產業'], row_c['型態'], f"{wr}%/{tr}%", row_d['吸籌特徵'], row_c['建議買價'], f"停損{round(row_c['建議買價']*0.93, 2)}", "MA防禦", now_time])
            if row_c['created_at'] > time_limit:
                ai_html += f"<b>【{row_c['名稱']} ({tid})】</b><br>回測勝率:{wr}% | 總報:{tr}%<br>型態:{row_c['型態']}<br><hr>"
                ai_tg_data.append(f"• <b>{row_c['名稱']} ({tid})</b>\n  勝率:{wr}% | 總報:{tr}%\n  評分:{row_d['評分']} | {row_c['型態']}")
        if ai_rows: ws_ai.append_rows(ai_rows)

    if c:
        sh.worksheet("買入型態掃描").append_rows([[today_date, i['代號'], i['名稱'], i['現價'], i['型態'], i['RS'], i['建議買價'], i['買入原因'], now_time] for i in c])
    if d:
        sh.worksheet("大戶動能評分").append_rows([[today_date, i['代號'], i['名稱'], i['產業'], i['評分'], i['RS'], i['吸籌特徵'], now_time] for i in d])

    return ai_html, ai_tg_data, h

def send_telegram(ai_tg_list, h_res):
    """
    調整：
    1. 雙重認證標的：無標定時發送告知訊息。
    2. 持股分析：顯示所有持股，並提供個別評價。
    """
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    now_str = datetime.now().strftime('%m/%d')
    msg = f"<b>📊 台股投資報告 ({now_str})</b>\n\n"

    # --- 1. 深度診斷標的 (雙重認證) ---
    msg += "💎 <b>雙重認證標的</b>\n"
    if ai_tg_list:
        msg += f"\n".join(ai_tg_list)
    else:
        msg += "☕ 今日無符合雙重認證標的。"
    msg += f"\n\n"

    # --- 2. 持股分析與評價 (全量顯示) ---
    msg += "🏥 <b>持股診斷與評價</b>\n"
    if not h_res:
        msg += "目前無庫存資料。"
    else:
        for item in h_res:
            # 根據 R 數與動作自動生成診斷評價
            r_val = float(item['獲利(R)'].replace('R', ''))
            
            # 評價邏輯分支
            if "🛑" in item['建議動作']:
                comment = "💀 趨勢反轉，執行紀律賣出。"
            elif "⚠️" in item['建議動作']:
                comment = "📉 跌破關鍵均線，縮減位能或嚴格守法。"
            elif r_val >= 2.0:
                comment = "🔥 獲利豐厚，進入 2R 保本機制，放任獲利奔跑。"
            elif r_val > 0.5:
                comment = "💪 走勢強勁，RS 表現優於大盤，穩定續抱。"
            elif r_val < 0 and r_val > -0.5:
                comment = "⏳ 處於震盪洗盤期，尚未觸及防守位，耐心觀察。"
            else:
                comment = "觀察中，守住防守點位。"

            # 組合訊息
            msg += f"• <b>{item['名稱']} ({item['代號']})</b>\n"
            msg += f"  評分：{item['建議動作']}\n"
            msg += f"  現價：{item['現價']} | 獲利：{item['獲利(R)']}\n"
            msg += f"  防守：{item['防守價']}\n"
            msg += f"  診斷：{comment}\n\n"

    # 執行發送
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"})

def send_email(h, c, d, ai_html):
    """
    調整：
    1. 庫存健檢：顯示所有持股並附帶 AI 診斷評價。
    2. 深度分析：顯示 15 分鐘內的雙重認證標的。
    3. 格式：完整 HTML 樣式，包含主流板塊統計。
    """
    # 如果 15 分鐘內完全沒有新資料（且庫存也沒變動，雖然庫存通常都會傳），則可視情況跳過
    if not ai_html and not c and not d and not h:
        print("沒有新資料，跳過 Email 通知")
        return

    # 將資料轉為 DataFrame 方便轉 HTML 表格
    df_h = pd.DataFrame(h) if h else pd.DataFrame()
    df_c = pd.DataFrame(c) if c else pd.DataFrame()
    df_d = pd.DataFrame(d) if d else pd.DataFrame()

    # 處理庫存評價 (同步 Telegram 邏輯)
    if not df_h.empty:
        def get_comment(row):
            r_val = float(row['獲利(R)'].replace('R', ''))
            if "🛑" in row['建議動作']: return "💀 趨勢反轉，執行紀律賣出。"
            if "⚠️" in row['建議動作']: return "📉 跌破關鍵均線，縮減位能或嚴格守法。"
            if r_val >= 2.0: return "🔥 獲利豐厚，進入 2R 保本機制。"
            if r_val > 0.5: return "💪 走勢強勁，穩定續抱。"
            return "⏳ 處於震盪洗盤期，耐心觀察。"
        
        df_h['AI 診斷評價'] = df_h.apply(get_comment, axis=1)
        # 移除不需要顯示在郵件表格的欄位
        df_h = df_h.drop(columns=['created_at'], errors='ignore')

    # 移除掃描結果中的時間戳記欄位（讓表格乾淨點）
    if not df_c.empty: df_c = df_c.drop(columns=['created_at'], errors='ignore')
    if not df_d.empty: df_d = df_d.drop(columns=['created_at'], errors='ignore')

    # 統計主流產業
    top_ind = df_d['產業'].value_counts().head(3).index.tolist() if not df_d.empty else ["觀察中"]

    # --- HTML 樣式設定 ---
    style = """
    <style>
        body { font-family: 'Microsoft JhengHei', sans-serif; line-height: 1.6; color: #333; }
        .title { background: #2c3e50; color: white; padding: 12px; margin-top: 25px; font-weight: bold; border-radius: 5px; }
        .ai-box { background: #fffcf0; border: 1px solid #f1c40f; border-left: 6px solid #f1c40f; padding: 15px; margin: 15px 0; font-size: 14px; }
        .table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 20px; }
        .table th, .table td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        .table th { background-color: #f8f9fa; font-weight: bold; }
        .highlight { color: #e74c3c; font-weight: bold; }
    </style>
    """
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    html = f"""
    <html>
    <head>{style}</head>
    <body>
        <h2>📈 台股策略投資報告</h2>
        <p>📅 報告時間：{now_str}</p>
        <p>💰 主流板塊：<span class='highlight'>{', '.join(top_ind)}</span></p>

        <div class='title'>1. 🏥 庫存健檢 (全持股分析)</div>
        {df_h.to_html(classes='table', index=False, escape=False) if not df_h.empty else "<p>目前無持股資料</p>"}

        <div class='title' style='background:#8e44ad;'>4. 💎 深度診斷 (15min 雙重認證)</div>
        <div class='ai-box'>
            {ai_html if ai_html else "☕ 今日暫無新符合雙重認證之標的。"}
        </div>

        <div class='title'>2. 🚀 買入型態掃描 (最新)</div>
        {df_c.to_html(classes='table', index=False, escape=False) if not df_c.empty else "<p>期間內無新標的</p>"}

        <div class='title'>3. 👑 大戶動能評分 (最新)</div>
        {df_d.to_html(classes='table', index=False, escape=False) if not df_d.empty else "<p>期間內無新標的</p>"}
        
        <p style='color: #7f8c8d; font-size: 12px;'>* 本報告由 AI 自動生成，僅供參考，投資請謹慎評估風險。</p>
    </body>
    </html>
    """

    # --- 寄送邏輯 ---
    msg = MIMEMultipart()
    msg['Subject'] = f"台股策略報告 - {datetime.now().strftime('%m/%d')}"
    msg['From'] = GMAIL_USER
    msg['To'] = RECEIVER_EMAIL
    msg.attach(MIMEText(html, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print("Email 報告已成功寄出。")
    except Exception as e:
        print(f"Email 發送失敗: {e}")

if __name__ == "__main__":
    # 讀取 Sheet 中的持股
    creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    records = sh.worksheet("持股分析").get_all_records()
    PORTFOLIO = {str(r['代號']): {'cost': r['成本']} for r in records}

    system = StockSystem(PORTFOLIO)
    h_res, c_res, d_res = system.run()
    ai_h, ai_t, h_f = sync_and_prepare(h_res, c_res, d_res)
    
    send_telegram(ai_t, h_res)
    send_email(h_res, ai_h)
    print("Done!")
