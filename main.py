import os
import smtplib
import pandas as pd
import numpy as np
import yfinance as yf
import twstock
import requests
import gspread
from tqdm import tqdm
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from oauth2client.service_account import ServiceAccountCredentials
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

# 環境變數
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
        """全量移植考特賣出法則"""
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
                if curr < cost: 
                    action = "🛑 清倉賣出(保本)"
                    reason.append("獲利回吐觸及成本")
                else: 
                    reason.append(f"達2R({round(r_multiple,1)}R)啟動保本")
            
            is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
            check_ma = ma10 if is_super else ma20
            if curr < check_ma:
                action = "⚠️ 警戒/賣出"
                reason.append(f"跌破{'10MA' if is_super else '20MA'}")
            
            return {"代號": ticker, "名稱": name, "現價": round(curr, 2), "獲利(R)": f"{round(r_multiple, 1)}R", "建議動作": action, "防守價": round(max(hard_stop, check_ma), 2), "原因": " | ".join(reason)}
        except: return None

    def analyze_chose(self, ticker, name, df, bench_roc):
        """全量移植買入型態判斷"""
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
        """全量移植 DRIVE 深度評分"""
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
# 📊 輔助引擎
# ==========================================
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
        trades, in_pos, entry_p, init_stop_pct = [], False, 0, 0.07 
        start_idx = len(df) - 750
        for i in range(start_idx, len(df)):
            curr_c = float(c_series.iloc[i]); dt = df.index[i]
            if not in_pos:
                if curr_c < 20 or avg_vol_20.iloc[i] < 800000: continue
                if not (curr_c > ma50.iloc[i] > ma200.iloc[i]): continue
                s_roc = float(c_series.iloc[i] / c_series.iloc[i-20] - 1)
                if (s_roc - bench_roc_series.get(dt, 0)) < 0: continue
                y_high, p20_high = float(h_series.iloc[i-250:i].max()), float(h_series.iloc[i-21:i].max())
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

def generate_ai_diagnostic(row_c, row_d, df, bench_series):
    try:
        close = df['Close'].iloc[:, 0] if isinstance(df['Close'], pd.DataFrame) else df['Close']
        buy_price = row_c['建議買價']
        init_stop = round(buy_price * 0.93, 2)
        ma10, ma20 = round(float(close.rolling(10).mean().iloc[-1]), 2), round(float(close.rolling(20).mean().iloc[-1]), 2)
        win_rate, cumulative_ret = backtest_3y_strategy(row_c['代號'], bench_series)
        star_tag = "<b style='color:#f1c40f;'>🌟 歷史績優生</b>" if win_rate >= 60 and cumulative_ret > 50 else ""
        is_super = (close.iloc[-35:] > close.rolling(10).mean().iloc[-35:]).all()
        defense_ma_name = "10MA" if is_super else "20MA"
        defense_ma_val = ma10 if is_super else ma20
        diagnostic = (
            f"<b>【{row_c['名稱']} ({row_c['代號'].split('.')[0]})】</b> {star_tag}<br>"
            f"➡️ <b>診斷結論：</b> 該股觸發了 <b>{row_c['型態']}</b>，評分 <b>{row_d['評分']} 分</b>，"
            f"RS 達 <b>{row_d['RS']}</b>。<br>"
            f"📊 <b>策略回測 (3Y)：</b> 勝率 <b style='color:#27ae60;'>{win_rate}%</b> | 總報酬 <b style='color:#27ae60;'>{cumulative_ret}%</b><br>"
            f"✅ <b>技術特徵：</b> {row_d['吸籌特徵']}<br>"
            f"📍 <b>佈局建議：</b> 建議在 <b>{buy_price}</b> 附近佈局。<br>"
            f"🛡️ <b>風險控管 (停損預估)：</b> 初始 <b>{init_stop}</b> | 強勢 <b>{ma10}</b> | 最後 <b>{ma20}</b><br>"
            f"💡 <b>當前防守重點：</b> 盯住 <b>{defense_ma_name} ({defense_ma_val})</b><br><br>"
            f"<hr style='border:0.5px dashed #ddd;'>"
        )
        return diagnostic
    except: return f"【{row_c['名稱']}】解析異常。<br>"

# ==========================================
# 🚀 整合輸出與同步邏輯
# ==========================================

def sync_to_gsheet_and_prepare_reports(h, c, d):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', scope)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    today = datetime.now().strftime('%Y-%m-%d')
    
    bench_df = yf.download('0050.TW', period='4y', progress=False, auto_adjust=True)
    bench_close = bench_df['Close'].iloc[:, 0] if isinstance(bench_df['Close'], pd.DataFrame) else bench_df['Close']
    bench_series = bench_close.pct_change(20).to_dict()

    # 1. 雙重認證與 AI 診斷
    ai_email_content, ai_tg_data = "", []
    if c and d:
        df_c, df_d = pd.DataFrame(c), pd.DataFrame(d)
        inter_ids = list(set(df_c['代號']) & set(df_d['代號']))
        ws_ai = sh.worksheet("雙重認證個股深度分析")
        for tid in inter_ids:
            row_c, row_d = df_c[df_c['代號'] == tid].iloc[0], df_d[df_d['代號'] == tid].iloc[0]
            df_temp = yf.download(tid, period='4y', progress=False, auto_adjust=True)
            # 生成 Email 用的 HTML
            ai_email_content += generate_ai_diagnostic(row_c, row_d, df_temp, bench_series)
            # 整理寫入 Sheets 的資料
            wr, tr = backtest_3y_strategy(tid, bench_series)
            ws_ai.append_row([today, tid, row_c['名稱'], row_d['產業'], "推薦", f"{wr}%/{tr}%", row_d['吸籌特徵'], row_c['建議買價'], "策略停損", "防禦線"])
            ai_tg_data.append(f"• <b>{row_c['名稱']} ({tid})</b>\n  勝率:{wr}% | 評分:{row_d['評分']}\n  型態:{row_c['型態']}")

    # 2. 買入型態掃描 (CHOSE)
    if c:
        ws_c = sh.worksheet("買入型態掃描")
        for item in c:
            ws_c.append_row([today, item['代號'], item['名稱'], item['現價'], item['型態'], item['RS'], item['建議買價'], item['買入原因']])

    # 3. 大戶動能評分 (DRIVE)
    if d:
        ws_d = sh.worksheet("大戶動能評分")
        for item in d:
            ws_d.append_row([today, item['代號'], item['名稱'], item['產業'], item['評分'], item['RS'], item['吸籌特徵']])

    return ai_email_content, ai_tg_data, bench_series

def send_telegram(ai_tg_list, c_data):
    if not ai_tg_list and not c_data: return
    msg = f"🚀 <b>台股策略掃描報告 ({datetime.now().strftime('%m/%d')})</b>\n\n"
    if ai_tg_list:
        msg += "💎 <b>雙重認證標的</b>\n" + "\n".join(ai_tg_list) + "\n\n"
    if c_data:
        msg += "📈 <b>買入型態精選</b>\n"
        for item in c_data[:3]:
            msg += f"• {item['名稱']} | {item['型態']} (RS:{item['RS']})\n"
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"})

def send_email_final(h, c, d, ai_html):
    df_h, df_c, df_d = pd.DataFrame(h), pd.DataFrame(c), pd.DataFrame(d)
    top_ind = df_d['產業'].value_counts().head(3).index.tolist() if not df_d.empty else []
    style = "<style>body{font-family:sans-serif;line-height:1.6;color:#333;}.title{background:#2c3e50;color:white;padding:12px;margin-top:25px;font-weight:bold;border-radius:5px;}.ai-box{background:#fffcf0;border:1px solid #f1c40f;border-left:6px solid #f1c40f;padding:15px;margin:15px 0;font-size:14px;}.table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:20px;}.table th,.table td{border:1px solid #ddd;padding:10px;text-align:left;}.table th{background-color:#f8f9fa;}</style>"
    html = f"<html><head>{style}</head><body><h2>📈 台股動能策略報告</h2><p>💰 本日主流板塊：{', '.join(top_ind)}</p>"
    html += "<div class='title'>1. 🏥 庫存健檢</div>" + (df_h.to_html(classes='table', index=False) if not df_h.empty else "<p>無資料</p>")
    html += "<div class='title' style='background:#8e44ad;'>4. 💎 深度診斷</div><div class='ai-box'>" + (ai_html if ai_html else "今日無雙重認證標標的") + "</div>"
    html += "<div class='title'>2. 🚀 買入型態</div>" + (df_c.to_html(classes='table', index=False) if not df_c.empty else "<p>無資料</p>")
    html += "<div class='title'>3. 👑 大戶評分</div>" + (df_d.to_html(classes='table', index=False) if not df_d.empty else "<p>無資料</p>")
    html += "</body></html>"
    msg = MIMEMultipart(); msg['Subject'] = f"台股策略報告 - {datetime.now().strftime('%Y-%m-%d')}"
    msg['From'], msg['To'] = GMAIL_USER, RECEIVER_EMAIL
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD); s.send_message(msg)

if __name__ == "__main__":
    system = StockSystem()
    h_res, c_res, d_res = system.run()
    
    print("正在執行試算表同步與 AI 分析...")
    ai_html, ai_tg, _ = sync_to_gsheet_and_prepare_reports(h_res, c_res, d_res)
    
    print("發送 Telegram...")
    send_telegram(ai_tg, c_res)
    
    print("寄送 Email...")
    send_email_final(h_res, c_res, d_res, ai_html)
    
    print("Done!")
