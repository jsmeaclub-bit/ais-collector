import os
import json
import time
import math
import random
import asyncio
import subprocess
import sys
import websockets
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

# 海上ルーティングライブラリ (searoute) の自動インポート/インストール
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

# 1. マスターデータの取得（2000GT以上のみ厳格抽出）
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
                        cog, hdg = pos.get("Cog", 0.0), pos.get("TrueHeading", 511)
                        received[mmsi]["heading"] = round(hdg if hdg != 511 else cog, 1)
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

def parse_timestamp(ts_str):
    ts_str = str(ts_str).strip()
    try:
        return datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        try:
            clean_ts = ts_str.split(".")[0].replace(" +0000 UTC", "")
            return datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min

# ★ 一般的な航路（シーレーン）で陸地を回避して補間する関数
def interpolate_sea_route(p1, p2):
    # p1, p2: [lat, lng]
    try:
        # searoute は (lon, lat) の順で指定
        origin = (p1[1], p1[0])
        destination = (p2[1], p2[0])
        route = sr.searoute(origin, destination)
        coords = route.get("geometry", {}).get("coordinates", [])
        if coords and len(coords) >= 2:
            # [lat, lng] に変換して返す
            return [[round(c[1], 4), round(c[0], 4)] for c in coords]
    except Exception as e:
        pass
    return [p1, p2]

def update_history_and_generate_tracks(live_ships, master_map, client):
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
    for row in all_history[1:]:
        if len(row) < 5: continue
        mmsi_str = str(row[1]).strip().split(".")[0]
        if mmsi_str not in master_map: continue
        try:
            ts = parse_timestamp(row[0])
            pt = [round(float(row[3]), 4), round(float(row[4]), 4)]
            if mmsi_str not in tracks_map: tracks_map[mmsi_str] = []
            tracks_map[mmsi_str].append((ts, pt))
        except: continue

    routes_output = []
    for mmsi, data_list in tracks_map.items():
        if not data_list: continue
        data_list.sort(key=lambda x: x[0])
        
        cleaned_list = []
        for item in data_list:
            if not cleaned_list or cleaned_list[-1][1] != item[1]:
                cleaned_list.append(item)
                
        if len(cleaned_list) < 2: continue
        heading = live_ships[mmsi].get("heading", 0.0) if mmsi in live_ships else 0.0
        
        # ★ ワープ区間を「一般的な海上航路」で補完して繋ぐ処理
        full_coords = [cleaned_list[0][1]]
        for i in range(1, len(cleaned_list)):
            prev_t, prev_p = cleaned_list[i-1]
            curr_t, curr_p = cleaned_list[i]
            
            dlat = abs(curr_p[0] - prev_p[0])
            dlng = abs(curr_p[1] - prev_p[1])
            
            # 0.2度（約20km）以上離れている場合は圏外航行とみなし、海上航路で補間
            if dlat > 0.2 or dlng > 0.2:
                interpolated = interpolate_sea_route(prev_p, curr_p)
                # 補間された中間ポイントを追加（先頭を除く）
                for mid_p in interpolated[1:]:
                    full_coords.append(mid_p)
            else:
                full_coords.append(curr_p)

        if len(full_coords) < 2: continue

        # 描画負荷軽減（最大500点にリサンプリング）
        if len(full_coords) > 500:
            step = len(full_coords) / 500.0
            display_coords = [full_coords[int(i * step)] for i in range(500)]
            if display_coords[-1] != full_coords[-1]: display_coords.append(full_coords[-1])
        else:
            display_coords = full_coords

        smoothed = smooth_line(display_coords, iterations=2)
        segments = []
        total_pts = len(smoothed)
        for i in range(total_pts - 1):
            ratio = i / (total_pts - 1)
            # シャープな細いライン（0.6px〜1.4px）でフェード描画
            opacity = round(0.05 + (0.75 * ratio), 2)
            weight = round(0.6 + (0.8 * ratio), 1)
            segments.append({"coords": [smoothed[i], smoothed[i+1]], "opacity": opacity, "weight": weight})

        routes_output.append({"mmsi": mmsi, "heading": heading, "segments": segments})

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
            "heading": ship["heading"],
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

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"masterCounts": master_counts, "latest": list(existing_latest.values())}, f, ensure_ascii=False, indent=2)

    update_history_and_generate_tracks(live_ships, master_map, client)

if __name__ == "__main__":
    main()
