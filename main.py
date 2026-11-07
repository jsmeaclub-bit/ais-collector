import os
import json
import time
import math
import random
import asyncio
import subprocess
import sys
import websockets
from collections import defaultdict
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

try:
    import searoute as sr
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute"])
    import searoute as sr

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

def parse_gt(gt_val):
    try:
        clean = str(gt_val).replace(",", "").strip()
        return float(clean)
    except:
        return 0.0

def calculate_bearing(p1, p2):
    lat1 = math.radians(p1[0])
    lat2 = math.radians(p2[0])
    diff_long = math.radians(p2[1] - p1[1])
    x = math.sin(diff_long) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(diff_long))
    initial_bearing = math.degrees(math.atan2(x, y))
    return round((initial_bearing + 360) % 360, 1)

def get_master_data(client):
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Japan_Master")
    records = sheet.get_all_records()
    master_map = {}
    flag_cnt, ctrl_cnt = 0, 0
    for r in records:
        mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
        if not mmsi or mmsi in ("nan", "None", "0"): continue
        
        gt_num = parse_gt(r.get("GT", 0))
        if gt_num > 0 and gt_num < 2000:
            continue
            
        cat = str(r.get("Category", "日本船社運航船")).strip()
        if cat == "日本籍船": flag_cnt += 1
        else: ctrl_cnt += 1
        
        master_map[mmsi] = {
            "mmsi": mmsi,
            "name": str(r.get("Name of Ship", "")).strip(),
            "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
            "category": cat,
            "flag": str(r.get("Flag", "-")).strip(),
            "type": str(r.get("Ship Type", "-")).strip(),
            "gt": str(r.get("GT", "-")).strip(),
            "owner": str(r.get("Registered Owner", "-")).strip(),
            "operator": str(r.get("Operator", "-")).strip(),
            "survey": str(r.get("Special Survey", "-")).strip()
        }
    return master_map, {"total": len(master_map), "flag": flag_cnt, "ctrl": ctrl_cnt}

async def collect_ais_data(target_mmsis):
    received = {}
    if not AISSTREAM_API_KEY: return received
    duration = random.randint(120, 600) if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else 30
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
                        received[mmsi] = {
                            "mmsi": mmsi, "lat": None, "lng": None, "speed": 0.0, "heading": 0.0,
                            "name": data.get("MetaData", {}).get("ShipName", "").strip(),
                            "destination": "", "eta": "", "timestamp": data.get("MetaData", {}).get("time_utc")
                        }
                    
                    if data.get("MessageType") == "PositionReport":
                        pos = data.get("Message", {}).get("PositionReport", {})
                        received[mmsi]["lat"] = pos.get("Latitude", received[mmsi]["lat"])
                        received[mmsi]["lng"] = pos.get("Longitude", received[mmsi]["lng"])
                        received[mmsi]["speed"] = round(pos.get("Sog", 0.0), 1)
                    elif data.get("MessageType") == "ShipStaticData":
                        static = data.get("Message", {}).get("ShipStaticData", {})
                        if dest := static.get("Destination", "").strip():
                            received[mmsi]["destination"] = dest
                        eta = static.get("Eta", {})
                        if eta and eta.get("Month"):
                            received[mmsi]["eta"] = f"{eta.get('Month'):02d}-{eta.get('Day'):02d} {eta.get('Hour'):02d}:{eta.get('Minute'):02d}"
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

def robust_parse_timestamp(ts_str):
    ts_str = str(ts_str).strip()
    if not ts_str: return datetime.min
    formats = [
        "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        "%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"
    ]
    clean_ts = ts_str.split(".")[0].replace(" +0000 UTC", "").replace(" UTC", "").replace("Z", "").replace("T", " ")
    for fmt in formats:
        try: return datetime.strptime(clean_ts, fmt)
        except ValueError: pass
    return datetime.min

def interpolate_sea_route(p1, p2):
    try:
        origin = (p1[1], p1[0])
        destination = (p2[1], p2[0])
        route = sr.searoute(origin, destination)
        coords = route.get("geometry", {}).get("coordinates", [])
        if coords and len(coords) >= 2:
            return [[round(c[1], 4), round(c[0], 4)] for c in coords]
    except Exception: pass
    return None

def update_history_and_generate_tracks(live_ships, master_map, existing_latest, client):
    wb = client.open_by_key(SPREADSHEET_ID)
    try: ws_history = wb.worksheet("Traffic_History_2026")
    except: ws_history = wb.add_worksheet("Traffic_History_2026", 1000, 15)
    
    new_history_rows = []
    for mmsi, ship in live_ships.items():
        if mmsi not in master_map: continue
        if ship["lat"] is None or ship["lng"] is None: continue
        m_info = master_map.get(mmsi, {})
        timestamp = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M:%S")
        row = [
            timestamp, mmsi, ship["name"] or m_info.get("name", ""), ship["lat"], ship["lng"], 
            ship["speed"], m_info.get("category", ""), m_info.get("owner", ""), m_info.get("type", ""), 
            m_info.get("imo", ""), m_info.get("survey", "")
        ]
        new_history_rows.append(row)
        
    if new_history_rows:
        ws_history.append_rows(new_history_rows)

    all_history = ws_history.get_all_values()
    tracks_map = {}
    for idx, row in enumerate(all_history[1:]):
        if len(row) < 5: continue
        mmsi_str = str(row[1]).strip().split(".")[0]
        if mmsi_str not in master_map: continue
        try:
            ts = robust_parse_timestamp(row[0])
            pt = [round(float(row[3]), 4), round(float(row[4]), 4)]
            if mmsi_str not in tracks_map: tracks_map[mmsi_str] = []
            tracks_map[mmsi_str].append((ts, pt, idx))
        except: continue

    routes_output = []
    calculated_headings = {}
    grid_counts = defaultdict(int)
    GRID_SIZE = 0.08  # 約8km〜9kmメッシュで重複航路を集計

    for mmsi, data_list in tracks_map.items():
        if not data_list: continue
        data_list.sort(key=lambda x: (x[0], x[2]))
        
        cleaned_list = []
        for item in data_list:
            if not cleaned_list or cleaned_list[-1][1] != item[1]:
                cleaned_list.append((item[0], item[1]))
                
        latest_coord = None
        if mmsi in existing_latest:
            curr_lat = existing_latest[mmsi].get("lat")
            curr_lng = existing_latest[mmsi].get("lng")
            if curr_lat is not None and curr_lng is not None:
                latest_coord = [round(float(curr_lat), 4), round(float(curr_lng), 4)]
                if not cleaned_list or cleaned_list[-1][1] != latest_coord:
                    cleaned_list.append((datetime.now(timezone.utc), latest_coord))

        if len(cleaned_list) < 2: continue
        category = master_map.get(mmsi, {}).get("category", "外国籍船")

        sub_lines = []
        curr_sub = [cleaned_list[0][1]]
        for i in range(1, len(cleaned_list)):
            prev_t, prev_p = cleaned_list[i-1]
            curr_t, curr_p = cleaned_list[i]
            dlat = abs(curr_p[0] - prev_p[0])
            dlng = abs(curr_p[1] - prev_p[1])
            
            if dlat > 0.25 or dlng > 0.25:
                interpolated = interpolate_sea_route(prev_p, curr_p)
                if interpolated and len(interpolated) >= 2:
                    for mid_p in interpolated[1:]: curr_sub.append(mid_p)
                else:
                    curr_sub.append(curr_p)
            else:
                curr_sub.append(curr_p)

        if len(curr_sub) >= 2:
            sub_lines.append(curr_sub)
        elif len(curr_sub) == 1 and sub_lines:
            sub_lines[-1].append(curr_sub[0])

        if latest_coord and sub_lines and sub_lines[-1]:
            sub_lines[-1][-1] = latest_coord

        heading = 0.0
        if len(sub_lines) > 0 and len(sub_lines[-1]) >= 2:
            p_prev = sub_lines[-1][-2]
            p_last = sub_lines[-1][-1]
            heading = calculate_bearing(p_prev, p_last)
            calculated_headings[mmsi] = heading

        segments = []
        total_pts = sum(len(sub) for sub in sub_lines)
        if total_pts >= 2:
            current_idx = 0
            for sub in sub_lines:
                if len(sub) > 200:
                    step = len(sub) / 200.0
                    sub_sampled = [sub[int(j * step)] for j in range(200)]
                    if sub_sampled[-1] != sub[-1]: sub_sampled.append(sub[-1])
                else:
                    sub_sampled = sub
                    
                smoothed = smooth_line(sub_sampled, iterations=2)
                for i in range(len(smoothed) - 1):
                    p1, p2 = smoothed[i], smoothed[i+1]
                    ratio = current_idx / (total_pts - 1)
                    opacity = round(0.18 + (0.77 * ratio), 2)
                    
                    # グリッド通過カウント
                    mid_lat = (p1[0] + p2[0]) / 2.0
                    mid_lng = (p1[1] + p2[1]) / 2.0
                    g_key = (round(mid_lat / GRID_SIZE), round(mid_lng / GRID_SIZE))
                    grid_counts[g_key] += 1

                    segments.append({"coords": [p1, p2], "opacity": opacity, "g_key": g_key})
                    current_idx += 1

        if segments:
            routes_output.append({
                "mmsi": mmsi, 
                "category": category, 
                "heading": heading, 
                "segments": segments
            })

    # ★ 各セグメントに重なり回数（count）を割り当て
    for r in routes_output:
        for seg in r["segments"]:
            g_key = seg.pop("g_key", None)
            seg["count"] = grid_counts.get(g_key, 1)

    for mmsi, ship in existing_latest.items():
        ship["heading"] = calculated_headings.get(mmsi, 0.0)

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_output, f, ensure_ascii=False)

    return calculated_headings

def main():
    client = get_spreadsheet_client()
    if not client: return
    master_map, master_counts = get_master_data(client)
    live_ships = asyncio.run(collect_ais_data(set(master_map.keys())))
    
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                for s in json.load(f).get("latest", []):
                    m_key = str(s.get("mmsi", "")).strip().split(".")[0]
                    if m_key and m_key in master_map:
                        existing_latest[m_key] = s
        except Exception: pass

    for mmsi, ship in live_ships.items():
        if mmsi not in master_map: continue
        m_info = master_map.get(mmsi, {})
        existing_latest[mmsi] = {
            "mmsi": mmsi,
            "name": m_info.get("name") or ship["name"] or f"MMSI:{mmsi}",
            "imo": m_info.get("imo", "-"),
            "category": m_info.get("category", "日本船社運航船"),
            "flag": m_info.get("flag", "-"),
            "type": m_info.get("type", "-"),
            "gt": m_info.get("gt", "-"),
            "owner": m_info.get("owner", "-"),
            "operator": m_info.get("operator", "-"),
            "survey": m_info.get("survey", "-"),
            "lat": ship["lat"],
            "lng": ship["lng"],
            "speed": ship["speed"],
            "heading": 0.0,
            "destination": ship["destination"] or existing_latest.get(mmsi, {}).get("destination", "-"),
            "eta": ship["eta"] or existing_latest.get(mmsi, {}).get("eta", "-"),
            "timestamp": ship["timestamp"]
        }

    for mmsi, ship_data in existing_latest.items():
        if mmsi in master_map:
            m = master_map[mmsi]
            if not ship_data.get("name") or ship_data["name"].startswith("MMSI:"):
                ship_data["name"] = m["name"] or ship_data["name"]
            if ship_data.get("imo") in ("-", "", None): ship_data["imo"] = m["imo"]
            if ship_data.get("category") in ("日本船社運航船", "", None): ship_data["category"] = m["category"]
            if ship_data.get("flag") in ("-", "", None): ship_data["flag"] = m["flag"]
            if ship_data.get("type") in ("-", "", None): ship_data["type"] = m["type"]
            if ship_data.get("gt") in ("-", "", None): ship_data["gt"] = m["gt"]
            if ship_data.get("owner") in ("-", "", None): ship_data["owner"] = m["owner"]
            if ship_data.get("operator") in ("-", "", None): ship_data["operator"] = m["operator"]
            if ship_data.get("survey") in ("-", "", None): ship_data["survey"] = m["survey"]

    update_history_and_generate_tracks(live_ships, master_map, existing_latest, client)

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"masterCounts": master_counts, "latest": list(existing_latest.values())}, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
