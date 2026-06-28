#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NQ 選擇權 GEX 全自動爬蟲 v1.0
- 自動登入 Barchart，下載 NQ 選擇權 CSV
- 從 Yahoo Finance 抓 ^VXN（波動率）與 ^NDX（現價）
- Black-Scholes Gamma 計算真實 GEX
- 累積翻正標記 Zero Gamma
- 寫入 Google 試算表「NQ 合併」工作表
"""

import os, csv, math, json, time, glob
from datetime import datetime, timedelta
from collections import defaultdict
from playwright.sync_api import sync_playwright

# ---------- 設定區 ----------
SIGMA_SOURCE = "^VXN"     # 那斯達克波動率指數
S_SOURCE     = "^NDX"     # 那斯達克100指數
R            = 0.0525     # 美國無風險利率
MULT         = 20         # NQ 選擇權乘數 ($20/點)

SPREADSHEET_ID = "1oPHb8dhDBpoN623zU0zEpC7cuiFLCrzWmvlcZsfSYFM"  # ← 請換成你自己的
CSV_DOWNLOAD_DIR = "/tmp/nq_csv"

# Barchart 登入資訊（從 GitHub Secrets 讀取）
BARCHART_USER = os.environ.get("BARCHART_USER", "")
BARCHART_PASS = os.environ.get("BARCHART_PASS", "")

# Google Sheets 金鑰（從 GitHub Secrets 讀取）
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "{}")

# ────────────────────────────────────────────
# 1. 瀏覽器自動下載 Barchart CSV
# ────────────────────────────────────────────
def download_barchart_csv():
    if not BARCHART_USER or not BARCHART_PASS:
        raise RuntimeError("請設定 BARCHART_USER 和 BARCHART_PASS Secrets")

    os.makedirs(CSV_DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # 登入 Barchart
        page.goto("https://www.barchart.com/login")
        page.fill("input[name='email']", BARCHART_USER)
        page.fill("input[name='password']", BARCHART_PASS)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        print("✅ 已登入 Barchart")

        # 前往 NQ 選擇權頁面
        page.goto("https://www.barchart.com/futures/quotes/NQ*0/options")
        page.wait_for_timeout(3000)

        # 點擊下載按鈕
        with page.expect_download() as download_info:
            page.click("text=Download")
        download = download_info.value

        csv_path = os.path.join(CSV_DOWNLOAD_DIR, "nq_options.csv")
        download.save_as(csv_path)
        print(f"✅ CSV 已下載至 {csv_path}")

        browser.close()
        return csv_path

# ────────────────────────────────────────────
# 2. Yahoo Finance 抓取數據
# ────────────────────────────────────────────
def get_yahoo_quote(symbol):
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="1d")
    if not hist.empty:
        return hist['Close'].iloc[-1]
    else:
        return ticker.fast_info.last_price

# ────────────────────────────────────────────
# 3. 解析 Barchart CSV
# ────────────────────────────────────────────
def parse_barchart_csv(file_path):
    rows = []
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for r in reader:
            strike_str = r.get('Strike', '')
            oi_str = r.get('Open Int', '0')
            typ = r.get('Type', '')
            time_str = r.get('Time', '')

            if not strike_str:
                continue
            is_call = strike_str.endswith('C')
            is_put = strike_str.endswith('P')
            if not is_call and not is_put:
                continue

            try:
                strike = float(strike_str[:-1].replace(',', ''))
            except:
                continue
            try:
                oi = float(oi_str.replace(',', ''))
            except:
                oi = 0.0

            expiry = None
            if time_str and time_str != '0':
                try:
                    expiry = datetime.strptime(time_str, '%m/%d/%y')
                except:
                    pass

            rows.append({
                'strike': strike,
                'oi': oi,
                'type': 'call' if is_call else 'put',
                'expiry': expiry
            })
    return rows

# ────────────────────────────────────────────
# 4. Black-Scholes Gamma + GEX 計算
# ────────────────────────────────────────────
def bs_gamma(S, K, T, sigma):
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S/K) + (R + sigma**2/2)*T) / (sigma * math.sqrt(T))
    return math.exp(-d1**2/2) / (S * sigma * math.sqrt(T)) / math.sqrt(2*math.pi)

def time_to_expiry(expiry):
    if not expiry:
        return 30.0 / 365.0
    days = (expiry - datetime.now()).days
    return max(days, 1) / 365.0

def calc_nq_gex(details, S, sigma):
    if not details or S is None:
        return []

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
            "履約價": K,
            "call_oi": v["call_oi"], "put_oi": v["put_oi"],
            "gex": net_gex, "cp_ratio": cp_ratio, "weight": weight,
            "is_zero_gamma": False, "is_big_money": weight >= 5.0
        })

    # ZG 累積翻正
    sorted_res = sorted(result, key=lambda x: x["履約價"])
    cum = 0.0; prev_cum = 0.0; zg_strike = None
    for r in sorted_res:
        cum += r["gex"]
        if prev_cum < 0 and cum >= 0 and zg_strike is None:
            zg_strike = r["履約價"]
            break
        prev_cum = cum
    if zg_strike is None:
        best_diff = float('inf'); cum = 0.0
        for r in sorted_res:
            cum += r["gex"]
            if abs(cum) < best_diff:
                best_diff = abs(cum); zg_strike = r["履約價"]
    for r in result:
        if r["履約價"] == zg_strike:
            r["is_zero_gamma"] = True
    return result

# ────────────────────────────────────────────
# 5. 生成 TradingView 字串
# ────────────────────────────────────────────
def generate_tv_string(gex_data):
    parts = []
    for d in sorted(gex_data, key=lambda x: x["履約價"], reverse=True):
        zg = 1 if d["is_zero_gamma"] else 0
        parts.append(f"{d['履約價']},{d['call_oi']},{d['put_oi']},{d['gex']:.2f},{d['cp_ratio']},{zg}")
    return ";".join(parts)

# ────────────────────────────────────────────
# 6. 寫入 Google Sheets
# ────────────────────────────────────────────
def connect_gsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

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

    rows = []
    for d in sorted(gex_data, key=lambda x: x["履約價"], reverse=True):
        rows.append([
            datetime.now().strftime("%Y/%m/%d"),
            d["履約價"],
            d["call_oi"], d["put_oi"],
            d["gex"], d["cp_ratio"], d["weight"],
            "✅ 多空分界" if d["is_zero_gamma"] else "",
            "💰 大資金" if d["is_big_money"] else "",
        ])
    ws.append_rows(rows)
    print(f"✅ 已寫入 {len(rows)} 筆至 NQ 合併")

# ────────────────────────────────────────────
# 7. 主程式
# ────────────────────────────────────────────
def main():
    print("=" * 60)
    print("NQ GEX 全自動爬蟲 v1.0")
    print("=" * 60)

    print("\n📥 下載 Barchart CSV...")
    csv_path = download_barchart_csv()

    print("\n📈 抓取 ^VXN 與 ^NDX...")
    sigma = get_yahoo_quote(SIGMA_SOURCE)
    S = get_yahoo_quote(S_SOURCE)
    if sigma is None:
        sigma = 0.20
    if S is None:
        raise RuntimeError("無法取得 ^NDX 現價")
    print(f"✅ ^VXN = {sigma:.4f}, ^NDX = {S:.2f}")

    print("\n🧾 解析 CSV...")
    details = parse_barchart_csv(csv_path)
    print(f"✅ 讀取 {len(details)} 筆明細")

    print("\n🧮 計算 Black-Scholes GEX...")
    gex_data = calc_nq_gex(details, S, sigma)

    top_call = sorted(gex_data, key=lambda x: x["call_oi"], reverse=True)[0]
    top_put  = sorted(gex_data, key=lambda x: x["put_oi"], reverse=True)[0]
    zg_row   = next((r for r in gex_data if r["is_zero_gamma"]), None)
    print(f"  最大壓力: {top_call['履約價']} (Call OI {top_call['call_oi']:,})")
    print(f"  最大支撐: {top_put['履約價']} (Put OI {top_put['put_oi']:,})")
    if zg_row:
        print(f"  Zero Gamma: {zg_row['履約價']}")

    tv_string = generate_tv_string(gex_data)

    print("\n📝 寫入 Google Sheets...")
    write_to_sheet(gex_data, tv_string)

    os.remove(csv_path)
    print(f"\n✅ 全部完成！")

if __name__ == "__main__":
    main()
