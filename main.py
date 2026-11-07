import os
import json
import time
import math
import random
import asyncio
import websockets
from datetime import datetime, timezone
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union
import gspread
from google.oauth2.service_account import Credentials

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

LATEST_FILE = os.path.join(DATA_DIR, "latest_2026.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage_2026.json")
POINTS_CACHE_FILE = os.path.join(DATA_DIR, "coverage_points.json")
TRACKS_CACHE_FILE = os.path.join(DATA_DIR, "tracks_cache_2026.json")
ROUTES_FILE = os.path.join(DATA_DIR, "routes_2026.json")

# ★ 測定期間（最大1年分：1日24回×365日 ＝ 約8760ポイント）の軌跡をすべて保持
MAX_TRACK_POINTS = 8760

def get_master_data():
    if not GCP_SA_KEY or not SPREADSHEET_ID:
        print("[ERROR] GCP_SA_KEY or SPREADSHEET_ID is not configured in Secrets.")
        return {}, {}
    try:
        sa_info = json.loads(GCP_SA_KEY)
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Japan_Master")
        records = sheet.get_all_records()

        master_map = {}
        flag_cnt, ctrl_cnt = 0, 0

        for r in records:
            raw_mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
            if not raw_mmsi or raw_mmsi in ("nan", "None", "0"): continue
            category = str(r.get("Category", "日本船社運航船")).strip()
            if category == "日本籍船": flag_cnt += 1
            else: ctrl_cnt += 1

            master_map[raw_mmsi] = {
                "mmsi": raw_mmsi,
                "name": str(r.get("Name of Ship", "")).strip(),
                "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
                "category": category,
                "flag": str(r.get("Flag", "-")).strip(),
                "type": str(r.get("Ship Type", "-")).strip(),
                "gt": str(r.get("GT", "-")).strip(),
                "owner": str(r.get("Registered Owner", "-")).strip(),
                "operator": str(r.get("Operator", "-")).strip(),
                "survey": str(r.get("Special Survey", "-")).strip()
            }
        print(f"[INFO] Successfully loaded {len(master_map)} target vessels.")
        return master_map, {"total": len(master_map), "flag": flag_cnt, "ctrl": ctrl_cnt}
    except Exception as e:
        print(f"[ERROR] Failed to fetch master: {e}")
        return {}, {}

async def collect_ais_data(target_mmsis):
    received_ships = {}
    if not AISSTREAM_API_KEY: return received_ships

    event_name = os.environ.get("GITHUB_EVENT_NAME")
    listen_duration = random.randint(120, 600) if event_name == "schedule" else 120
    print(f"[INFO] Connecting to AISStream.io for {listen_duration} seconds...")

    url = "wss://stream.aisstream.io/v0/stream"
    subscribe_msg = {"APIKey": AISSTREAM_API_KEY, "BoundingBoxes": [[[-90, -180], [90, 180]]], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(subscribe_msg))
            start_time = time.time()
            while time.time() - start_time < listen_duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    mmsi = str(data.get("MetaData", {}).get("MMSI", "")).strip().split(".")[0]
                    if mmsi not in target_mmsis: continue
                    msg_type = data.get("MessageType")
                    meta = data.get("MetaData", {})

                    if mmsi not in received_ships:
                        received_ships[mmsi] = {"mmsi": mmsi, "name": meta.get("ShipName", "").strip(), "lat": meta.get("latitude"), "lng": meta.get("longitude"), "speed": 0.0, "heading": 0.0, "destination": "", "eta": "", "timestamp": meta.get("time_utc")}
                    ship = received_ships[mmsi]
                    if msg_type == "PositionReport":
                        pos = data.get("Message", {}).get("PositionReport", {})
                        ship["lat"], ship["lng"] = pos.get("Latitude", ship["lat"]), pos.get("Longitude", ship["lng"])
                        ship["speed"] = round(pos.get("Sog", 0.0), 1)
                        cog, hdg = pos.get("Cog", 0.0), pos.get("TrueHeading", 511)
                        ship["heading"] = round(hdg if hdg != 511 else cog, 1)
                    elif msg_type == "ShipStaticData":
                        static = data.get("Message", {}).get("ShipStaticData", {})
                        if dest := static.get("Destination", "").strip(): ship["destination"] = dest
                        eta = static.get("Eta", {})
                        if eta and eta.get("Month"): ship["eta"] = f"{eta.get('Month'):02d}-{eta.get('Day'):02d} {eta.get('Hour'):02d}:{eta.get('Minute'):02d}"
                except asyncio.TimeoutError: continue
                except Exception: continue
    except Exception as e: print(f"[ERROR] WebSocket connection error: {e}")
    print(f"[INFO] Collected live data for {len(received_ships)} target vessels.")
    return received_ships

def update_coverage_geojson(all_points_list):
    all_points = set()
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r", encoding="utf-8") as f:
                all_points = {tuple(p) for p in json.load(f)}
        except Exception: pass
    for lat, lng in all_points_list:
        if lat is not None and lng is not None: all_points.add((round(lat, 2), round(lng, 2)))
    with open(POINTS_CACHE_FILE, "w", encoding="utf-8") as f: json.dump([list(p) for p in all_points], f)
    if not all_points: return

    radius_deg = 50.0 / 60.0
    polygons = []
    for lat, lng in all_points:
        cos_lat = max(math.cos(math.radians(lat)), 0.1)
        pts = [(lng + (radius_deg / cos_lat) * math.cos(2 * math.pi * i / 16), lat + radius_deg * math.sin(2 * math.pi * i / 16)) for i in range(16)]
        polygons.append(Polygon(pts))

    simplified_geom = unary_union(polygons).simplify(0.02, preserve_topology=True)
    geojson_data = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "AIS Coverage (50nm)"}, "geometry": mapping(simplified_geom)}]}
    with open(COVERAGE_FILE, "w", encoding="utf-8") as f: json.dump(geojson_data, f, ensure_ascii=False)

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

def update_routes(live_ships):
    tracks_map = {}
    if os.path.exists(TRACKS_CACHE_FILE):
        try:
            with open(TRACKS_CACHE_FILE, "r", encoding="utf-8") as f: tracks_map = json.load(f)
        except Exception: pass

    # 1. 座標データの蓄積（最大1年分）
    for mmsi, ship in live_ships.items():
        if ship["lat"] is None or ship["lng"] is None: continue
        if mmsi not in tracks_map: tracks_map[mmsi] = []
        new_pt = [round(ship["lat"], 4), round(ship["lng"], 4)]
        if not tracks_map[mmsi] or tracks_map[mmsi][-1] != new_pt:
            tracks_map[mmsi].append(new_pt)
        if len(tracks_map[mmsi]) > MAX_TRACK_POINTS:
            tracks_map[mmsi] = tracks_map[mmsi][-MAX_TRACK_POINTS:]

    with open(TRACKS_CACHE_FILE, "w", encoding="utf-8") as f: json.dump(tracks_map, f)

    # 2. 全軌跡に対するグラデーション・フェード効果の計算
    routes_output = []
    for mmsi, coords in tracks_map.items():
        if len(coords) < 2: continue
        heading = live_ships[mmsi].get("heading", 0.0) if mmsi in live_ships else 0.0
        
        # ブラウザの負荷を下げるため、表示用の点は最大300点に間引く（期間は1年のまま粗くする）
        if len(coords) > 300:
            step = len(coords) / 300.0
            display_coords = [coords[int(i * step)] for i in range(300)]
            if display_coords[-1] != coords[-1]: display_coords.append(coords[-1])
        else:
            display_coords = coords

        smoothed_coords = smooth_line(display_coords, iterations=2)
        
        segments = []
        total_pts = len(smoothed_coords)
        for i in range(total_pts - 1):
            ratio = i / (total_pts - 1) # 0.0(最古) -> 1.0(最新)
            opacity = round(0.0 + (0.9 * ratio), 2) # 最古は完全に透明にして消す
            weight = round(1.0 + (1.5 * ratio), 1)  # 最新に近づくほど太くする
            
            segments.append({
                "coords": [smoothed_coords[i], smoothed_coords[i+1]],
                "opacity": opacity,
                "weight": weight
            })

        routes_output.append({
            "mmsi": mmsi,
            "heading": heading,
            "segments": segments # 全長グラデーションデータ
        })

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_output, f, ensure_ascii=False)
    print(f"[INFO] routes_2026.json saved with {len(routes_output)} fading tracks.")

def main():
    master_map, master_counts = get_master_data()
    if not master_map: return
    
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

    all_active_points = [(s["lat"], s["lng"]) for s in existing_latest.values() if s.get("lat") is not None and s.get("lng") is not None]
    
    update_coverage_geojson(all_active_points)
    update_routes(live_ships)


if __name__ == "__main__":
    main()
