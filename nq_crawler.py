#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NQ 選擇權 GEX 全自動爬蟲 v1.4
- 假日不執行
- 防錯機制（數據量異常時跳過寫入）
- 優先使用本地 CSV，自動下載為備援
"""

import os, csv, math, json, time
from datetime import datetime, timedelta, date
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

# ---------- 假日判斷 ----------
def is_us_market_open():
    """簡單判斷：周末一定休市"""
    today = date.today()
    # 周六(5) 周日(6) 休市
    if today.weekday() >= 5:
        return False
    # 以後可以手動加入美股假日列表
    # holidays = ["2026-01-01", "2026-07-04", "2026-12-25"]
    # if today.strftime("%Y-%m-%d") in holidays:
    #     return False
    return True

# ---------- 1. 下載 CSV ----------
def download_barchart_csv():
    local_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nq_options.csv")
    
    if os.path.exists(local_csv):
        print(f"✅ 使用本地 CSV: {local_csv}")
        return local_csv

    if not BARCHART_USER or not BARCHART_PASS:
        raise FileNotFoundError("請上傳 nq_options.csv 或設定 Barchart 帳號")

    os.makedirs(CSV_DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # 登入
        page.goto("https://www.barchart.com/login", wait_until="networkidle")
        page.fill("input[name='email']", BARCHART_USER)
        page.fill("input[type='password']", BARCHART_PASS)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        print("✅ 已登入")

        # 使用完整履約價 URL
        download_url = "https://www.barchart.com/futures/quotes/NQU26/options/sep-26?futuresOptionsView=merged&moneyness=allRows"
        
        # 嘗試直接下載
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

            try:
                strike = float(strike_str[:-1].replace(',', ''))
            except: continue
            try:
                oi = float(oi_str.replace(',', ''))
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
        best_diff = float('inf'); cum = 0.0
        for r in sorted_res:
            cum += r["gex"]
            if abs(cum) < best_diff:
                best_diff = abs(cum); zg_strike = r["履約價"]
    for r in result:
        if r["履約價"] == zg_strike: r["is_zero_gamma"] = True
    return result

# ---------- 4. 寫入 Google Sheets ----------
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
            d["履約價"], d["call_oi"], d["put_oi"],
            d["gex"], d["cp_ratio"], d["weight"],
            "✅ 多空分界" if d["is_zero_gamma"] else "",
            "💰 大資金" if d["is_big_money"] else "",
        ])
    ws.append_rows(rows)
    print(f"✅ 已寫入 {len(rows)} 筆至 NQ 合併")

# ---------- 5. 主程式 ----------
def main():
    print("=" * 60)
    print("NQ GEX 全自動爬蟲 v1.4")
    print("=" * 60)

    # 假日判斷
    if not is_us_market_open():
        print("⏸️ 今日為美股休市日（週末），跳過爬蟲")
        return

    csv_path = download_barchart_csv()

    import yfinance as yf
    sigma = yf.Ticker(SIGMA_SOURCE).history(period="1d")['Close'].iloc[-1]
    S = yf.Ticker(S_SOURCE).history(period="1d")['Close'].iloc[-1]
    print(f"✅ ^VXN = {sigma:.2f}, ^NDX = {S:.2f}")

    details = parse_barchart_csv(csv_path)
    print(f"✅ 解析 {len(details)} 筆明細")
    
    # 防錯機制：數據量異常時跳過
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
    write_to_sheet(gex_data, tv_string)
    print("\n✅ 全部完成！")

if __name__ == "__main__":
    main()
