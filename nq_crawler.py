#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NQ 選擇權 GEX 全自動爬蟲 v1.7
- 自動判斷季度合約
- 每日總 Call/Put OI 變化 + 累計趨勢（基於試算表前一日數據）
- 自動清理 90 天前舊行
- 美股盤後數據（NDX, SPX, SOX, VIX, VXN, AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, NQ期貨, TSM）
"""

import os, csv, math, json, time
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict
from playwright.sync_api import sync_playwright

# ---------- 設定 ----------
SIGMA_SOURCE = "^VXN"
S_SOURCE     = "^NDX"
R            = 0.0525
MULT         = 20

SPREADSHEET_ID = "1oPHb8dhDBpoN623zU0zEpC7cuiFLCrzWmvlcZsfSYFM"
CSV_DOWNLOAD_DIR = "/tmp/nq_csv"

BARCHART_USER = os.environ.get("BARCHART_USER", "")
BARCHART_PASS = os.environ.get("BARCHART_PASS", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "{}")

NQ_QUARTER_MAP = {
    3:  {"code": "H", "name": "mar"},
    6:  {"code": "M", "name": "jun"},
    9:  {"code": "U", "name": "sep"},
    12: {"code": "Z", "name": "dec"},
}

# ---------- 輔助函數 ----------
def get_third_friday(year, month):
    first_day = datetime(year, month, 1)
    days_until_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + timedelta(days=days_until_friday)
    return first_friday + timedelta(days=14)

def get_current_nq_contract():
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    today = tw_now.date()
    current_year = tw_now.year
    for month in [3, 6, 9, 12]:
        expiry = get_third_friday(current_year, month)
        if today <= expiry.date(): 
            yr_suffix = str(current_year)[-2:]
            info = NQ_QUARTER_MAP[month]
            return {
                "symbol": f"NQ{info['code']}{yr_suffix}",
                "month_str": f"{info['name']}-{yr_suffix}",
                "expiry": expiry.strftime("%Y-%m-%d")
            }
    # 若都不符合，找明年三月
    next_year = current_year + 1
    yr_suffix = str(next_year)[-2:]
    info = NQ_QUARTER_MAP[3]
    expiry = get_third_friday(next_year, 3)
    return {
        "symbol": f"NQ{info['code']}{yr_suffix}",
        "month_str": f"{info['name']}-{yr_suffix}",
        "expiry": expiry.strftime("%Y-%m-%d")
    }

def is_us_market_open():
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    today = tw_now.date()
    return today.weekday() < 5  # 週末不跑

# ---------- 1. 下載 CSV ----------
def download_barchart_csv():
    local_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nq_options.csv")
    if os.path.exists(local_csv):
        print(f"✅ 使用本地 CSV: {local_csv}")
        return local_csv
    if not BARCHART_USER or not BARCHART_PASS:
        raise FileNotFoundError("請上傳 nq_options.csv 或設定 Barchart 帳號")
    os.makedirs(CSV_DOWNLOAD_DIR, exist_ok=True)
    contract = get_current_nq_contract()
    print(f"📅 當前合約: {contract['symbol']}（到期日: {contract['expiry']}）")
    download_url = (
        f"https://www.barchart.com/futures/quotes/{contract['symbol']}/"
        f"options/{contract['month_str']}?futuresOptionsView=merged&moneyness=allRows"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto("https://www.barchart.com/login", wait_until="networkidle")
        page.fill("input[name='email']", BARCHART_USER)
        page.fill("input[type='password']", BARCHART_PASS)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        print("✅ 已登入")
        try:
            with page.expect_download(timeout=15000) as download_info:
                page.goto(download_url)
            download = download_info.value
            csv_path = os.path.join(CSV_DOWNLOAD_DIR, "nq_options.csv")
            download.save_as(csv_path)
            print(f"✅ CSV 已下載至 {csv_path}")
            success = True
        except Exception as e:
            print(f"❌ 直接下載失敗: {e}")
            success = False
        browser.close()
        if not success:
            if os.path.exists(local_csv):
                print("✅ 回退使用本地 CSV")
                return local_csv
            raise RuntimeError("自動下載失敗，且無本地 CSV")
    return csv_path

# ---------- 2. 解析 CSV ----------
def parse_barchart_csv(file_path):
    rows = []
    with open(file_path, 'r', encoding='utf-8') as f:
        first_line = f.readline()
        f.seek(0)
        delimiter = '\t' if '\t' in first_line else ','
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        strike_key = next((h for h in headers if h.lower().startswith('strike')), 'Strike')
        oi_key = next((h for h in headers if 'open int' in h.lower()), 'Open Int')
        type_key = next((h for h in headers if h.lower().strip() == 'type'), 'Type')
        time_key = next((h for h in headers if h.lower().strip() == 'time'), 'Time')
        for r in reader:
            strike_str = r.get(strike_key, '')
            oi_str = r.get(oi_key, '0')
            typ = r.get(type_key, '')
            time_str = r.get(time_key, '')
            if not strike_str: continue
            is_call = strike_str.endswith('C')
            is_put = strike_str.endswith('P')
            if not is_call and not is_put: continue
            try: strike = float(strike_str[:-1].replace(',', ''))
            except: continue
            try: oi = float(oi_str.replace(',', ''))
            except: oi = 0.0
            expiry = None
            if time_str and time_str != '0':
                try: expiry = datetime.strptime(time_str, '%m/%d/%y')
                except: pass
            rows.append({'strike': strike, 'oi': oi, 'type': 'call' if is_call else 'put', 'expiry': expiry})
    return rows

# ---------- 3. GEX 計算 ----------
def bs_gamma(S, K, T, sigma):
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0: return 0.0
    d1 = (math.log(S/K) + (R + sigma**2/2)*T) / (sigma * math.sqrt(T))
    return math.exp(-d1**2/2) / (S * sigma * math.sqrt(T)) / math.sqrt(2*math.pi)

def time_to_expiry(expiry):
    if not expiry: return 30.0/365.0
    days = (expiry - datetime.now()).days
    return max(days, 1) / 365.0

def calc_nq_gex(details, S, sigma):
    if not details or S is None: return []
    strike_map = defaultdict(lambda: {"call_oi":0,"put_oi":0,"call_gex":0.0,"put_gex":0.0})
    for d in details:
        K = d["strike"]
        T = time_to_expiry(d["expiry"])
        gamma = bs_gamma(S, K, T, sigma)
        if d["type"] == "call":
            strike_map[K]["call_oi"] += d["oi"]
            strike_map[K]["call_gex"] += gamma * d["oi"] * MULT * S
        else:
            strike_map[K]["put_oi"] += d["oi"]
            strike_map[K]["put_gex"] += gamma * d["oi"] * MULT * S

    result = []
    total_oi = sum(v["call_oi"]+v["put_oi"] for v in strike_map.values())
    for K, v in strike_map.items():
        net_gex = v["call_gex"] - v["put_gex"]
        cp_ratio = round(v["call_oi"]/v["put_oi"], 2) if v["put_oi"] > 0 else 999.0
        weight = round((v["call_oi"]+v["put_oi"])/total_oi*100, 1) if total_oi > 0 else 0
        result.append({
            "履約價": K, "call_oi": v["call_oi"], "put_oi": v["put_oi"],
            "gex": net_gex, "cp_ratio": cp_ratio, "weight": weight,
            "is_zero_gamma": False, "is_big_money": weight >= 5.0
        })

    sorted_res = sorted(result, key=lambda x: x["履約價"])
    cum = 0.0; prev_cum = 0.0; zg_strike = None
    for r in sorted_res:
        cum += r["gex"]
        if prev_cum < 0 and cum >= 0 and zg_strike is None:
            zg_strike = r["履約價"]
            break
        prev_cum = cum
    if zg_strike is None:
        best_diff = float('inf')
        cum = 0.0
        for r in sorted_res:
            cum += r["gex"]
            diff = abs(cum)
            if diff < best_diff:
                best_diff = diff
                zg_strike = r["履約價"]
        if zg_strike is None and len(sorted_res) > 0:
            zg_strike = sorted_res[0]["履約價"]
    for r in result:
        if r["履約價"] == zg_strike: r["is_zero_gamma"] = True
    return result

# ---------- 4. Google Sheets 寫入 ----------
def connect_gsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def ensure_sheet(sh, name, rows=500, cols=12):
    try:
        return sh.worksheet(name)
    except:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)

def write_to_sheet(gex_data, tv_string):
    sh = connect_gsheet()
    try:
        ws = sh.worksheet("NQ 合併")
        ws.clear()
    except:
        ws = sh.add_worksheet(title="NQ 合併", rows=500, cols=12)
    ws.append_row(["📋 NQ 合併 TradingView 數據字串（每天複製 A2 貼到指標）"])
    ws.append_row([tv_string])
    ws.append_row([""])
    ws.append_row(["更新日期","履約價","CallOI","PutOI","GEX","C/P比","佔比%","Zero Gamma","大資金"])
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    date_str = tw_now.strftime("%Y/%m/%d")
    rows = []
    for d in sorted(gex_data, key=lambda x: x["履約價"], reverse=True):
        rows.append([
            date_str,
            d["履約價"], d["call_oi"], d["put_oi"],
            d["gex"], d["cp_ratio"], d["weight"],
            "✅ 多空分界" if d["is_zero_gamma"] else "",
            "💰 大資金" if d["is_big_money"] else "",
        ])
    ws.append_rows(rows)
    print(f"✅ 已寫入 {len(rows)} 筆至 NQ 合併")

def write_nq_chips_analysis(sh, gex_data, today_str):
    ws = ensure_sheet(sh, "NQ 籌碼分析", rows=500, cols=10)
    total_call = sum(d["call_oi"] for d in gex_data)
    total_put = sum(d["put_oi"] for d in gex_data)
    cp_ratio = round(total_call / total_put, 2) if total_put > 0 else 0
    max_call = max(gex_data, key=lambda x: x["call_oi"])
    max_put = max(gex_data, key=lambda x: x["put_oi"])

    # 從試算表最後一筆數據取得前一日總 OI，計算變化量
    all_rows = ws.get_all_values()
    call_change = 0
    put_change = 0
    if len(all_rows) > 1:  # 已有歷史數據
        last_row = all_rows[-1]
        try:
            prev_total_call = float(last_row[1])
            prev_total_put = float(last_row[2])
            call_change = total_call - prev_total_call
            put_change = total_put - prev_total_put
        except:
            pass

    # 確保標題行
    if len(all_rows) == 0:
        ws.append_row(["日期","總CallOI","總PutOI","C/P比","Call變化","Put變化","最大壓力價","最大壓力OI","最大支撐價","最大支撐OI"])
        all_rows = [["日期"]]

    dates = ws.col_values(1)
    row_data = [today_str, total_call, total_put, cp_ratio, int(call_change), int(put_change),
                max_call["履約價"], max_call["call_oi"],
                max_put["履約價"], max_put["put_oi"]]

    if today_str in dates:
        idx = dates.index(today_str) + 1
        ws.update(values=[row_data], range_name=f"A{idx}:J{idx}")
    else:
        ws.append_row(row_data)

    # 自動刪除 90 天前的舊行
    cutoff_date = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=90)).strftime("%Y/%m/%d")
    all_values = ws.get_all_values()
    rows_to_delete = []
    for i, row in enumerate(all_values[1:], start=2):
        if row[0] < cutoff_date:
            rows_to_delete.append(i)
    for row_index in reversed(rows_to_delete):
        ws.delete_rows(row_index)

    print(f"✅ 已寫入 NQ 籌碼分析（call變化: {call_change}, put變化: {put_change}）")

def write_nq_cumulative(sh, gex_data, today_str):
    ws = ensure_sheet(sh, "NQ 累計趨勢", rows=500, cols=8)
    total_call = sum(d["call_oi"] for d in gex_data)
    total_put = sum(d["put_oi"] for d in gex_data)

    all_rows = ws.get_all_values()
    call_change = 0
    put_change = 0
    if len(all_rows) > 1:
        last_row = all_rows[-1]
        try:
            prev_total_call = float(last_row[1])
            prev_total_put = float(last_row[2])
            call_change = total_call - prev_total_call
            put_change = total_put - prev_total_put
        except:
            pass

    # 取得上一行累計值
    prev_cum_call = 0
    prev_cum_put = 0
    if len(all_rows) > 1:
        last_row = all_rows[-1]
        try:
            prev_cum_call = int(last_row[5])
            prev_cum_put = int(last_row[6])
        except:
            pass

    cum_call = prev_cum_call + int(call_change)
    cum_put = prev_cum_put + int(put_change)

    if len(all_rows) == 0:
        ws.append_row(["日期","總CallOI","總PutOI","Call變化","Put變化","累積Call","累積Put"])
        all_rows = [["日期"]]

    dates = ws.col_values(1)
    row_data = [today_str, total_call, total_put, int(call_change), int(put_change), cum_call, cum_put]

    if today_str in dates:
        idx = dates.index(today_str) + 1
        ws.update(values=[row_data], range_name=f"A{idx}:G{idx}")
    else:
        ws.append_row(row_data)

    # 自動刪除 90 天前的舊行
    cutoff_date = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=90)).strftime("%Y/%m/%d")
    all_values = ws.get_all_values()
    rows_to_delete = []
    for i, row in enumerate(all_values[1:], start=2):
        if row[0] < cutoff_date:
            rows_to_delete.append(i)
    for row_index in reversed(rows_to_delete):
        ws.delete_rows(row_index)

    print(f"✅ 已寫入 NQ 累計趨勢（call變化: {call_change}, put變化: {put_change}）")

def write_us_market_data(sh, us_data, ndx_close, ndx_change, today_str):
    ws = ensure_sheet(sh, "NQ 市場數據", rows=100, cols=30)
    ws.clear()
    ws.append_row([
        "日期",
        "NDX", "NDX漲跌%",
        "SPX", "SPX漲跌%",
        "SOX", "SOX漲跌%",
        "VIX", "VXN",
        "AAPL", "AAPL漲跌%",
        "MSFT", "MSFT漲跌%",
        "NVDA", "NVDA漲跌%",
        "GOOGL", "GOOGL漲跌%",
        "AMZN", "AMZN漲跌%",
        "META", "META漲跌%",
        "TSLA", "TSLA漲跌%",
        "NQ期貨", "NQ期貨漲跌%",
        "TSM", "TSM漲跌%",
    ])
    row = [
        today_str,
        round(ndx_close, 2), round(ndx_change, 2),
        us_data.get("SPX", {}).get("close", 0), us_data.get("SPX", {}).get("change_pct", 0),
        us_data.get("SOX", {}).get("close", 0), us_data.get("SOX", {}).get("change_pct", 0),
        us_data.get("VIX", {}).get("close", 0),
        us_data.get("VXN", {}).get("close", 0),
        us_data.get("AAPL", {}).get("close", 0), us_data.get("AAPL", {}).get("change_pct", 0),
        us_data.get("MSFT", {}).get("close", 0), us_data.get("MSFT", {}).get("change_pct", 0),
        us_data.get("NVDA", {}).get("close", 0), us_data.get("NVDA", {}).get("change_pct", 0),
        us_data.get("GOOGL", {}).get("close", 0), us_data.get("GOOGL", {}).get("change_pct", 0),
        us_data.get("AMZN", {}).get("close", 0), us_data.get("AMZN", {}).get("change_pct", 0),
        us_data.get("META", {}).get("close", 0), us_data.get("META", {}).get("change_pct", 0),
        us_data.get("TSLA", {}).get("close", 0), us_data.get("TSLA", {}).get("change_pct", 0),
        us_data.get("NQ_F", {}).get("close", 0), us_data.get("NQ_F", {}).get("change_pct", 0),
        us_data.get("TSM", {}).get("close", 0), us_data.get("TSM", {}).get("change_pct", 0),
    ]
    ws.append_row(row)
    print(f"✅ 已寫入 NQ 市場數據（含七雄）")

# ---------- 5. 主程式 ----------
def main():
    print("=" * 60)
    print("NQ GEX 全自動爬蟲 v1.7")
    print("=" * 60)

    if not is_us_market_open():
        print("⏸️ 今日為美股休市日（週末），跳過爬蟲")
        return

    csv_path = download_barchart_csv()

    import yfinance as yf

    # 波動率與標的價格
    sigma = yf.Ticker(SIGMA_SOURCE).history(period="1d")['Close'].iloc[-1]
    S = yf.Ticker(S_SOURCE).history(period="1d")['Close'].iloc[-1]
    ndx_open = yf.Ticker(S_SOURCE).history(period="1d")['Open'].iloc[-1]
    ndx_change = (S - ndx_open) / ndx_open * 100
    print(f"✅ ^VXN = {sigma:.2f}, ^NDX = {S:.2f} ({ndx_change:+.2f}%)")

    # 美股盤後數據
    print("\n📊 抓取美股盤後數據...")
    us_tickers = {
        "SPX": "^GSPC",
        "SOX": "^SOX",
        "VIX": "^VIX",
        "AAPL": "AAPL",
        "MSFT": "MSFT",
        "NVDA": "NVDA",
        "GOOGL": "GOOGL",
        "AMZN": "AMZN",
        "META": "META",
        "TSLA": "TSLA",
        "NQ_F": "NQ=F",
        "TSM": "TSM",
    }
    us_data = {}
    for name, symbol in us_tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if not hist.empty:
                close = hist['Close'].iloc[-1]
                open_price = hist['Open'].iloc[-1]
                change_pct = (close - open_price) / open_price * 100
                us_data[name] = {
                    "close": round(close, 2),
                    "change_pct": round(change_pct, 2)
                }
                print(f"  {name}: {close:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            print(f"  ⚠️ {name} 抓取失敗: {e}")
            us_data[name] = {"close": 0, "change_pct": 0}
    us_data["VXN"] = {"close": round(sigma, 2), "change_pct": 0}

    # 解析 CSV + 計算 GEX
    details = parse_barchart_csv(csv_path)
    print(f"✅ 解析 {len(details)} 筆明細")
    if len(details) < 50:
        print(f"❌ 數據量異常（僅 {len(details)} 筆），可能是 Barchart 尚未更新，跳過寫入")
        return
    gex_data = calc_nq_gex(details, S, sigma)
    if not gex_data:
        print("❌ GEX 計算失敗，跳過寫入")
        return

    top_call = sorted(gex_data, key=lambda x: x["call_oi"], reverse=True)[0]
    top_put  = sorted(gex_data, key=lambda x: x["put_oi"], reverse=True)[0]
    zg_row = next((r for r in gex_data if r["is_zero_gamma"]), None)
    print(f"  最大壓力: {top_call['履約價']} (Call OI {top_call['call_oi']:,})")
    print(f"  最大支撐: {top_put['履約價']} (Put OI {top_put['put_oi']:,})")
    if zg_row: print(f"  Zero Gamma: {zg_row['履約價']}")

    tv_string = ";".join([f"{d['履約價']},{d['call_oi']},{d['put_oi']},{d['gex']:.2f},{d['cp_ratio']},{1 if d['is_zero_gamma'] else 0}"
                          for d in sorted(gex_data, key=lambda x: x["履約價"], reverse=True)])

    tw_now = datetime.now(timezone(timedelta(hours=8)))
    today_str = tw_now.strftime("%Y/%m/%d")

    sh = connect_gsheet()
    write_to_sheet(gex_data, tv_string)
    write_nq_chips_analysis(sh, gex_data, today_str)
    write_nq_cumulative(sh, gex_data, today_str)
    write_us_market_data(sh, us_data, S, ndx_change, today_str)

    print("\n✅ 全部完成！")

if __name__ == "__main__":
    main()
