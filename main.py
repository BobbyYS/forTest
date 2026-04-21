import os
import smtplib
import pandas as pd
import numpy as np
import yfinance as yf
import twstock
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from tqdm import tqdm
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ==========================================
# ⚙️ 使用者設定區
# ==========================================
MY_PORTFOLIO = {
    '4939.TW': {'cost': 51.2, 'stop_loss_pct': 0.07},
    '3346.TW': {'cost': 50.8, 'stop_loss_pct': 0.07},
    '2492.TW': {'cost': 133.5, 'stop_loss_pct': 0.07},
    '2317.TW': {'cost': 227.2, 'stop_loss_pct': 0.07}
}

# 環境變數 (GitHub Secrets / Local Env)
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SHEET_ID = os.environ.get('GOOGLE_SHEET_ID')

class StockSystem:
    def __init__(self):
        self.bench_ticker = '0050.TW'
        self.min_price = 20
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
            cost = data['cost']
            init_risk_pct = data['stop_loss_pct']
            init_risk_amt = cost * init_risk_pct
            pnl_amt = curr - cost
            r_multiple = pnl_amt / init_risk_amt
            ma10 = float(close.rolling(10).mean().iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            action, reason = "✅ 續抱", []
            hard_stop = cost * (1 - init_risk_pct)
            if curr < hard_stop:
                action = "🛑 清倉賣出(停損)"
                reason.append(f"跌破初始停損 {round(hard_stop, 2)}")
            elif r_multiple >= 2:
                if curr < cost: action = "🛑 清倉賣出(保本)"; reason.append("獲利回吐觸及成本")
                else: reason.append(f"達2R({round(r_multiple,1)}R)啟動保本")
            is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
            check_ma = ma10 if is_super else ma20
            if curr < check_ma:
                action = "⚠️ 警戒/賣出"
                reason.append(f"跌破{'10MA' if is_super else '20MA'}")
            return {"代號": ticker, "名稱": name, "現價": round(curr, 2), "獲利(R)": f"{round(r_multiple, 1)}R", "建議動作": action, "防守價": round(max(hard_stop, check_ma), 2), "原因": " | ".join(reason)}
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
            year_high = float(high.iloc[-250:].max())
            prev_20_high = float(high.iloc[-21:-1].max())
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
                return {"代號": ticker, "名稱": name, "現價": round(curr, 2), "型態": setup, "RS": round(rs_rating, 1), "建議買價": round(prev_20_high, 2), "買入原因": reason}
            return None
        except: return None

    def analyze_drive(self, item, df, bench_roc):
        try:
            close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
            high = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
            low = df['Low'].iloc[:, 0] if isinstance(df['Low'], pd.DataFrame) else df['Low']
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
                return {"代號": item['ticker'], "名稱": item['name'], "產業": item['industry'], "評分": score, "RS": round(rs_rating, 1), "吸籌特徵": " + ".join(comments)}
            return None
        except: return None

    def run(self):
        codes = twstock.codes
        all_stocks = [{'ticker': c+('.TW' if r.market=='上市' else '.TWO'), 'name': r.name, 'industry': r.group} for c,r in codes.items() if r.type=='股票']
        bench_c, bench_d = self.get_benchmark_roc(20), self.get_benchmark_roc(60)
        res_h, res_c, res_d = [], [], []
        print(f"🚀 全力掃描 {len(all_stocks)} 檔標的...")
        for item in tqdm(all_stocks[:100]): # 範例限制前100檔，實際請拿掉[:100]
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

def backtest_3y_strategy(ticker, bench_roc_series):
    try:
        df = yf.download(ticker, period='4y', progress=False, auto_adjust=True)
        if df.empty or len(df) < 300: return 0, 0
        c_series = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
        h_series = df['High'].iloc[:, 0] if isinstance(df['High'], pd.DataFrame) else df['High']
        o_series = df['Open'].iloc[:, 0] if isinstance(df['Open'], pd.DataFrame) else df['Open']
        v_series = df['Volume'].iloc[:, 0] if isinstance(df['Volume'], pd.DataFrame) else df['Volume']
        ma10 = c_series.rolling(10).mean(); ma20 = c_series.rolling(20).mean()
        ma50 = c_series.rolling(50).mean(); ma200 = c_series.rolling(200).mean()
        avg_vol_20 = v_series.rolling(20).mean()
        trades = []; in_pos = False; entry_p = 0; init_stop_pct = 0.07 
        start_idx = len(df) - 750
        for i in range(start_idx, len(df)):
            curr_c = float(c_series.iloc[i]); dt = df.index[i]
            if not in_pos:
                if curr_c < 20 or avg_vol_20.iloc[i] < 800000: continue
                if not (curr_c > ma50.iloc[i] > ma200.iloc[i]): continue
                s_roc = float(c_series.iloc[i] / c_series.iloc[i-20] - 1)
                if (s_roc - bench_roc_series.get(dt, 0)) < 0: continue
                y_high = float(h_series.iloc[i-250:i].max())
                p20_high = float(h_series.iloc[i-21:i].max())
                is_break = (curr_c > p20_high) and (c_series.iloc[i-1] < p20_high)
                rally = (h_series.iloc[i-60:i].max() - c_series.iloc[i-60:i].min()) / c_series.iloc[i-60:i].min()
                if (rally > 0.8 and (y_high - curr_c)/y_high < 0.25 and is_break) or \
                   ((o_series.iloc[i] - c_series.iloc[i-1])/c_series.iloc[i-1] > 0.08) or \
                   (is_break and (y_high - curr_c)/y_high < 0.15):
                    entry_p = curr_c; in_pos = True
            elif in_pos:
                r_mult = (curr_c - entry_p) / (entry_p * init_stop_pct)
                is_super = (c_series.iloc[i-34:i+1] > ma10.iloc[i-34:i+1]).all()
                check_ma = ma10.iloc[i] if is_super else ma20.iloc[i]
                if curr_c < entry_p * (1 - init_stop_pct) or (r_mult >= 2 and curr_c < entry_p) or curr_c < check_ma:
                    trades.append((curr_c - entry_p) / entry_p); in_pos = False
        if not trades: return 0, 0
        wr = len([t for t in trades if t > 0]) / len(trades) * 100
        tr = (np.prod([1 + t for t in trades]) - 1) * 100
        return round(wr, 1), round(tr, 1)
    except: return 0, 0

# ==========================================
# 📊 資料流轉區 (GSHEET / TG / EMAIL)
# ==========================================

def process_results_to_sheets(h, c, d):
    """將原始數據轉換為試算表格式並寫入，回傳結構化資料供後續使用"""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', scope)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 準備回測基準
    bench_df = yf.download('0050.TW', period='4y', progress=False, auto_adjust=True)
    bench_series = (bench_df['Close'].iloc[:, 0] if isinstance(bench_df['Close'], pd.DataFrame) else bench_df['Close']).pct_change(20).to_dict()

    # 1. 處理「雙重認證個股深度分析」
    ai_list = []
    if c and d:
        df_c, df_d = pd.DataFrame(c), pd.DataFrame(d)
        inter_ids = list(set(df_c['代號']) & set(df_d['代號']))
        ws_ai = sh.worksheet("雙重認證個股深度分析")
        for tid in inter_ids:
            row_c = df_c[df_c['代號'] == tid].iloc[0]
            row_d = df_d[df_d['代號'] == tid].iloc[0]
            df_temp = yf.download(tid, period='4y', progress=False, auto_adjust=True)
            close = df_temp['Close'].iloc[:, 0] if isinstance(df_temp['Close'], pd.DataFrame) else df_temp['Close']
            wr, tr = backtest_3y_strategy(tid, bench_series)
            ma10 = round(float(close.rolling(10).mean().iloc[-1]), 2)
            ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2)
            is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
            def_ma = f"{'10MA' if is_super else '20MA'} ({ma10 if is_super else ma20})"
            
            ai_item = {
                "日期": today, "代號": tid, "名稱": row_c['名稱'], "產業": row_d['產業'],
                "診斷結論": f"觸發 {row_c['型態']}，評分 {row_d['評分']}",
                "策略回測 (3Y)": f"勝率 {wr}% | 總報 {tr}%",
                "技術特徵": row_d['吸籌特徵'], "佈局建議": f"建議買價 {row_c['建議買價']} 附近",
                "風險控管 (停損預估)": f"初始停損 {round(row_c['建議買價']*0.93, 2)}",
                "當前防守重點": def_ma
            }
            ai_list.append(ai_item)
            ws_ai.append_row(list(ai_item.values()))

    # 2. 寫入「買入型態掃描」
    if c:
        ws_c = sh.worksheet("買入型態掃描")
        for item in c:
            ws_c.append_row([today] + list(item.values()))

    # 3. 寫入「大戶動能評分」
    if d:
        ws_d = sh.worksheet("大戶動能評分")
        for item in d:
            ws_d.append_row([today] + list(item.values()))
            
    return ai_list

def send_telegram_msg(ai_data, chose_data):
    if not ai_data and not chose_data: return
    text = f"🚀 *台股動能投資報告 ({datetime.now().strftime('%m/%d')})*\n\n"
    if ai_data:
        text += "💎 *雙重認證個股*\n"
        for item in ai_data:
            text += f"• *{item['名稱']} ({item['代號'].split('.')[0]})*\n"
            text += f"  - 結論: {item['診斷結論']}\n"
            text += f"  - 回測: {item['策略回測 (3Y)']}\n"
            text += f"  - 防守: {item['當前防守重點']}\n\n"
    if chose_data:
        text += "📈 *買入型態(前3筆)*\n"
        for item in chose_data[:3]:
            text += f"• {item['名稱']}: {item['型態']} (RS:{item['RS']})\n"
    
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                  json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"})

def send_email(h, ai_list, c, d):
    # 建立表格與 AI 診斷區塊 (維持原信件格式)
    top_ind = list(set([x['產業'] for x in d]))[:3] if d else []
    ai_section = ""
    for item in ai_list:
        star = "🌟 歷史績優生" if "勝率 60" in item['策略回測 (3Y)'] else ""
        ai_section += f"<b>【{item['名稱']} ({item['代號']})】</b> {star}<br>➡️ <b>診斷結論：</b> {item['診斷結論']}。RS 強度顯示為其板塊領頭羊。<br>📊 <b>策略回測 (3Y)：</b> {item['策略回測 (3Y)']}<br>✅ <b>技術特徵：</b> {item['技術特徵']}<br>📍 <b>佈局建議：</b> {item['佈局建議']}<br>🛡️ <b>風險控管：</b> {item['風險控管 (停損預估)']}<br>💡 <b>防守重點：</b> {item['當前防守重點']}<br><hr style='border:0.5px dashed #ddd;'>"

    style = "<style>body{font-family:sans-serif;line-height:1.6;color:#333;}.title{background:#2c3e50;color:white;padding:12px;margin-top:25px;font-weight:bold;border-radius:5px;}.ai-box{background:#fffcf0;border:1px solid #f1c40f;border-left:6px solid #f1c40f;padding:15px;margin:15px 0;font-size:14px;}.table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:20px;}.table th,.table td{border:1px solid #ddd;padding:10px;text-align:left;}.table th{background-color:#f8f9fa;}</style>"
    html = f"<html><head>{style}</head><body><h2>📈 台股動能投資策略報告 ({datetime.now().strftime('%Y-%m-%d')})</h2><p>💰 本日主流板塊：{', '.join(top_ind)}</p>"
    html += "<div class='title'>1. 🏥 庫存健檢</div>" + (pd.DataFrame(h).to_html(classes='table', index=False) if h else "<p>無資料</p>")
    html += "<div class='title' style='background:#8e44ad;'>4. 💎 雙重認證 (AI 診斷)</div><div class='ai-box'>" + (ai_section if ai_section else "今日無雙重認證標的") + "</div>"
    html += "<div class='title'>2. 🚀 買入型態</div>" + (pd.DataFrame(c).to_html(classes='table', index=False) if c else "<p>無資料</p>")
    html += "<div class='title'>3. 👑 大戶動能</div>" + (pd.DataFrame(d).to_html(classes='table', index=False) if d else "<p>無資料</p>")
    html += "</body></html>"

    msg = MIMEMultipart(); msg['Subject'] = f"台股策略報告 - {datetime.now().strftime('%Y-%m-%d')}"
    msg['From'], msg['To'] = GMAIL_USER, RECEIVER_EMAIL
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD); s.send_message(msg)

if __name__ == "__main__":
    system = StockSystem()
    h, c, d = system.run()
    
    print("Step 1: 寫入 Google Sheets...")
    ai_data = process_results_to_sheets(h, c, d)
    
    print("Step 2: 從 Google Sheets 取得資料並發送 Telegram...")
    # 這裡直接使用剛才處理好的 ai_data，若要嚴格從 Sheet 讀取可再呼叫 gspread get_all_records
    send_telegram_msg(ai_data, c)
    
    print("Step 3: 寄送 Email...")
    send_email(h, ai_data, c, d)
    
    print("任務完成!")
