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

def smooth_line(coords, iterations=2):
    if len(coords) < 3: return coords
    current = coords
    for _ in range(iterations):
        smoothed = [current[0]]
        for i in range(len(current) - 1):
            p0, p1 = current[i], current[i + 1]
            smoothed.append([p0[0] * 0.75 + p1[0] * 0.25, p0[1] * 0.75 + p1[1] * 0.25])
            smoothed.append([p0[0] * 0.25 + p1[0] * 0.75, p0[1] * 0.25 + p1[1] * 0.75])
        smoothed.append(current[-1])
        current = smoothed
    return current

def update_history_and_generate_tracks(live_ships, master_map, client):
    wb = client.open_by_key(SPREADSHEET_ID)
    try: ws_history = wb.worksheet("Traffic_History_2026")
    except: ws_history = wb.add_worksheet("Traffic_History_2026", 1000, 15)
    
    # 1. Traffic_History_2026 へ最新データをフル項目で追記
    new_history_rows = []
    for mmsi, ship in live_ships.items():
        if ship["lat"] is None or ship["lng"] is None: continue
        m_info = master_map.get(mmsi, {})
        timestamp = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
        row = [
            timestamp,
            mmsi,
            ship["name"] or m_info.get("name", ""),
            ship["lat"],
            ship["lng"],
            ship["speed"],
            m_info.get("category", ""),
            m_info.get("owner", ""),
            m_info.get("type", ""),
            m_info.get("imo", ""),
            m_info.get("survey", "")
        ]
        new_history_rows.append(row)
        
    if new_history_rows:
        ws_history.append_rows(new_history_rows)
        print(f"[INFO] Appended {len(new_history_rows)} logs to Traffic_History_2026.")

    # 2. 同シートの全ログを読み込んで軌跡を生成
    all_history = ws_history.get_all_values()
    tracks_map = {}
    for row in all_history[1:]:
        if len(row) < 5: continue
        mmsi_str = str(row[1]).strip()
        if not mmsi_str.isdigit(): continue
        try:
            pt = [round(float(row[3]), 4), round(float(row[4]), 4)]
            if mmsi_str not in tracks_map: tracks_map[mmsi_str] = []
            if not tracks_map[mmsi_str] or tracks_map[mmsi_str][-1] != pt:
                tracks_map[mmsi_str].append(pt)
        except: continue

    routes_output = []
    for mmsi, coords in tracks_map.items():
        if len(coords) < 2: continue
        heading = live_ships[mmsi].get("heading", 0.0) if mmsi in live_ships else 0.0
        
        # 間引き処理
        if len(coords) > 500:
            step = len(coords) / 500.0
            display_coords = [coords[int(i * step)] for i in range(500)]
            if display_coords[-1] != coords[-1]: display_coords.append(coords[-1])
        else:
            display_coords = coords

        # ★ ワープ切断処理 (範囲外から戻ってきた時の直線を切る)
        sub_lines = []
        curr_sub = [display_coords[0]]
        for i in range(1, len(display_coords)):
            prev = display_coords[i-1]
            curr = display_coords[i]
            if abs(curr[0] - prev[0]) > 1.0 or abs(curr[1] - prev[1]) > 1.0:
                if len(curr_sub) >= 2: sub_lines.append(curr_sub)
                curr_sub = [curr]
            else:
                curr_sub.append(curr)
        if len(curr_sub) >= 2:
            sub_lines.append(curr_sub)

        segments = []
        total_pts = sum(len(sub) for sub in sub_lines)
        if total_pts >= 2:
            current_idx = 0
            for sub in sub_lines:
                smoothed = smooth_line(sub, iterations=2)
                for i in range(len(smoothed) - 1):
                    ratio = current_idx / (total_pts - 1)
                    opacity = round(0.0 + (0.9 * ratio), 2)
                    weight = round(1.0 + (1.5 * ratio), 1)
                    segments.append({"coords": [smoothed[i], smoothed[i+1]], "opacity": opacity, "weight": weight})
                    current_idx += 1

        if segments:
            routes_output.append({"mmsi": mmsi, "heading": heading, "segments": segments})

    # フラットな配列構造で出力（UI側のエラー回避）
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_output, f, ensure_ascii=False)

def main():
    client = get_spreadsheet_client()
    if not client: return
    master_map, master_counts = get_master_data(client)
    live_ships = asyncio.run(collect_ais_data(set(master_map.keys())))
    
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                for s in json.load(f).get("latest", []): existing_latest[str(s["mmsi"])] = s
        except Exception: pass

    for mmsi, ship in live_ships.items():
        m_info = master_map.get(mmsi, {})
        existing_latest[mmsi] = {
            "mmsi": mmsi, "name": m_info.get("name") or ship["name"] or f"MMSI:{mmsi}", "imo": m_info.get("imo", "-"),
            "category": m_info.get("category", "日本船社運航船"), "flag": m_info.get("flag", "-"), "type": m_info.get("type", "-"),
            "gt": m_info.get("gt", "-"), "owner": m_info.get("owner", "-"), "operator": m_info.get("operator", "-"), "survey": m_info.get("survey", "-"),
            "lat": ship["lat"], "lng": ship["lng"], "speed": ship["speed"], "heading": ship["heading"],
            "destination": ship["destination"] or existing_latest.get(mmsi, {}).get("destination", "-"),
            "eta": ship["eta"] or existing_latest.get(mmsi, {}).get("eta", "-"), "timestamp": ship["timestamp"]
        }

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"masterCounts": master_counts, "latest": list(existing_latest.values())}, f, ensure_ascii=False, indent=2)

    update_history_and_generate_tracks(live_ships, master_map, client)

if __name__ == "__main__":
    main()
