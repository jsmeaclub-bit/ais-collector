import os
import json
import time
import math
import random
import asyncio
import websockets
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
LATEST_FILE = os.path.join(DATA_DIR, "latest_2026.json")
ROUTES_FILE = os.path.join(DATA_DIR, "routes_2026.json")


def get_spreadsheet_client():
    if not GCP_SA_KEY: return None
    sa_info = json.loads(GCP_SA_KEY)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(sa_info, scopes=scopes))

# 1. マスターデータの取得
def get_master_data(client):
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Japan_Master")
    records = sheet.get_all_records()
    master_map = {}
    flag_cnt, ctrl_cnt = 0, 0
    for r in records:
        mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
        if not mmsi or mmsi in ("nan", "None", "0"): continue
        cat = str(r.get("Category", "日本船社運航船")).strip()
        if cat == "日本籍船": flag_cnt += 1
        else: ctrl_cnt += 1
        master_map[mmsi] = {
            "mmsi": mmsi, "name": str(r.get("Name of Ship", "")).strip(), "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
            "category": cat, "flag": str(r.get("Flag", "-")).strip(), "type": str(r.get("Ship Type", "-")).strip(),
            "gt": str(r.get("GT", "-")).strip(), "owner": str(r.get("Registered Owner", "-")).strip(),
            "operator": str(r.get("Operator", "-")).strip(), "survey": str(r.get("Special Survey", "-")).strip()
        }
    return master_map, {"total": len(master_map), "flag": flag_cnt, "ctrl": ctrl_cnt}

# 2. AIS ライブデータの受信
async def collect_ais_data(target_mmsis):
    received = {}
    if not AISSTREAM_API_KEY: return received
    duration = random.randint(120, 600) if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else 120
    url = "wss://stream.aisstream.io/v0/stream"
    sub_msg = {"APIKey": AISSTREAM_API_KEY, "BoundingBoxes": [[[-90, -180], [90, 180]]], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(sub_msg))
            start = time.time()
            while time.time() - start < duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    mmsi = str(data.get("MetaData", {}).get("MMSI", "")).strip().split(".")[0]
                    if mmsi not in target_mmsis: continue
                    
                    if mmsi not in received:
                        received[mmsi] = {"mmsi": mmsi, "lat": None, "lng": None, "speed": 0.0, "heading": 0.0, "name": data.get("MetaData", {}).get("ShipName", "").strip(), "destination": "", "eta": "", "timestamp": data.get("MetaData", {}).get("time_utc")}
                    
                    if data.get("MessageType") == "PositionReport":
                        pos = data.get("Message", {}).get("PositionReport", {})
                        received[mmsi]["lat"] = pos.get("Latitude", received[mmsi]["lat"])
                        received[mmsi]["lng"] = pos.get("Longitude", received[mmsi]["lng"])
                        received[mmsi]["speed"] = round(pos.get("Sog", 0.0), 1)
                        cog, hdg = pos.get("Cog", 0.0), pos.get("TrueHeading", 511)
                        received[mmsi]["heading"] = round(hdg if hdg != 511 else cog, 1)
                except Exception: continue
    except Exception as e: print(f"[ERROR] WebSocket: {e}")
    return received

# 3. 本来のアーキテクチャによる各シートの更新処理
def process_sheets(live_ships, master_map, client):
    wb = client.open_by_key(SPREADSHEET_ID)
    
    # シートの取得
    try: ws_traffic = wb.worksheet("Route_Traffic")
    except: ws_traffic = wb.add_worksheet("Route_Traffic", 1000, 15)
    
    try: ws_history = wb.worksheet("Traffic_History_2026")
    except: ws_history = wb.add_worksheet("Traffic_History_2026", 1000, 15)
    
    try: ws_summary = wb.worksheet("Route_Summary_2026")
    except: 
        ws_summary = wb.add_worksheet("Route_Summary_2026", 1000, 7)
        ws_summary.append_row(["SegmentID", "Category", "StartLat", "StartLng", "EndLat", "EndLng", "Count"])

    # 既存の最新位置 (Route_Traffic) を読み込む（前回位置の取得用）
    traffic_records = ws_traffic.get_all_records()
    prev_positions = {str(r.get("MMSI", "")): {"lat": r.get("Latitude"), "lng": r.get("Longitude")} for r in traffic_records if r.get("MMSI")}
    
    # 既存のサマリー線分 (Route_Summary_2026) を読み込む
    summary_data = ws_summary.get_all_values()
    summary_headers = summary_data[0] if summary_data else ["SegmentID", "Category", "StartLat", "StartLng", "EndLat", "EndLng", "Count"]
    summary_rows = summary_data[1:] if len(summary_data) > 1 else []
    
    summary_map = {}
    for i, r in enumerate(summary_rows):
        if len(r) > 0: summary_map[r[0]] = {"row_idx": i + 2, "count": int(r[6]) if len(r) > 6 and str(r[6]).isdigit() else 1}

    new_history_rows = []
    updated_summary_cells = []
    new_summary_rows = []
    
    # ライブデータをもとに処理
    for mmsi, ship in live_ships.items():
        if ship["lat"] is None or ship["lng"] is None: continue
        
        # 1. 履歴用データ作成
        timestamp = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
        cat = master_map.get(mmsi, {}).get("category", "")
        new_history_rows.append([timestamp, mmsi, ship["name"], ship["lat"], ship["lng"], ship["speed"], cat])
        
        # 2. 線分(セグメント)の作成 (前回位置 -> 今回位置)
        prev = prev_positions.get(mmsi)
        if prev and prev["lat"] and prev["lng"]:
            start_lat, start_lng = round(float(prev["lat"]), 2), round(float(prev["lng"]), 2)
            end_lat, end_lng = round(float(ship["lat"]), 2), round(float(ship["lng"]), 2)
            
            # 同じ場所に停泊している場合は線を引かない
            if start_lat != end_lat or start_lng != end_lng:
                seg_id = f"{start_lat}_{start_lng}_{end_lat}_{end_lng}"
                
                if seg_id in summary_map:
                    # すでに同じルートがあればカウントを増やす（ヒートマップ効果）
                    summary_map[seg_id]["count"] += 1
                    updated_summary_cells.append({"range": f"G{summary_map[seg_id]['row_idx']}", "values": [[summary_map[seg_id]["count"]]]})
                else:
                    # 新しいルートなら追加
                    new_summary_rows.append([seg_id, cat, start_lat, start_lng, end_lat, end_lng, 1])
                    summary_map[seg_id] = {"row_idx": len(summary_rows) + len(new_summary_rows) + 1, "count": 1}

    # スプレッドシートへの書き込み実行
    if new_history_rows:
        ws_history.append_rows(new_history_rows)
    if new_summary_rows:
        ws_summary.append_rows(new_summary_rows)
    if updated_summary_cells:
        ws_summary.batch_update(updated_summary_cells)
        
    print(f"[INFO] Processed: Added {len(new_history_rows)} logs, Created {len(new_summary_rows)} new segments.")

    # 画面描画用のJSONファイル (routes_2026.json) に集計結果を書き出す
    all_summary = ws_summary.get_all_records()
    routes_json = []
    for s in all_summary:
        try:
            coords = [[float(s["StartLat"]), float(s["StartLng"])], [float(s["EndLat"]), float(s["EndLng"])]]
            # カウント（通行量）に応じて線の太さと透明度を計算
            weight = min(4.0, 1.0 + (float(s["Count"]) * 0.1))
            opacity = min(0.9, 0.3 + (float(s["Count"]) * 0.05))
            routes_json.append({"coords": coords, "weight": round(weight, 1), "opacity": round(opacity, 2), "cat": s.get("Category", "")})
        except: continue

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_json, f, ensure_ascii=False)


def main():
    client = get_spreadsheet_client()
    if not client: return
    
    master_map, master_counts = get_master_data(client)
    live_ships = asyncio.run(collect_ais_data(set(master_map.keys())))
    
    process_sheets(live_ships, master_map, client)

if __name__ == "__main__":
    main()
