import os
import json
import time
import math
import asyncio
import subprocess
import sys
import websockets
import gzip
import csv
import glob
from collections import defaultdict
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

try:
    import searoute as sr
    from shapely.geometry import Point, mapping, LineString
    from shapely.ops import unary_union
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely"])
    import searoute as sr
    from shapely.geometry import Point, mapping, LineString
    from shapely.ops import unary_union

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

CURRENT_YEAR = datetime.now(timezone.utc).year
LATEST_FILE = os.path.join(DATA_DIR, f"latest_{CURRENT_YEAR}.json")
ROUTES_FILE = os.path.join(DATA_DIR, f"routes_{CURRENT_YEAR}.json")
COVERAGE_POINTS_FILE = os.path.join(DATA_DIR, "coverage_points.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage.json")
EXPIRATION_SECONDS = 180 * 24 * 3600  

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
    type_counts_dict = defaultdict(int) 
    
    for r in records:
        mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
        if not mmsi or mmsi in ("nan", "None", "0"): continue
        
        gt_num = parse_gt(r.get("GT", 0))
        if gt_num > 0 and gt_num < 2000:
            continue
            
        cat = str(r.get("Category", "日本船社運航船")).strip()
        if cat == "日本籍船": flag_cnt += 1
        else: ctrl_cnt += 1
        
        ship_type = str(r.get("Ship Type", "-")).strip()
        if not ship_type: ship_type = "-"
        type_counts_dict[ship_type] += 1 
        
        master_map[mmsi] = {
            "mmsi": mmsi,
            "name": str(r.get("Name of Ship", "")).strip(),
            "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
            "category": cat,
            "flag": str(r.get("Flag", "-")).strip(),
            "type": ship_type,
            "gt": str(r.get("GT", "-")).strip(),
            "owner": str(r.get("Registered Owner", "-")).strip(),
            "operator": str(r.get("Operator", "-")).strip(),
            "survey": str(r.get("Special Survey", "-")).strip()
        }
        
    sorted_types = sorted(type_counts_dict.items(), key=lambda x: x[1], reverse=True)
    type_counts_list = [{"type": k, "count": v} for k, v in sorted_types]
    
    return master_map, {"total": len(master_map), "flag": flag_cnt, "ctrl": ctrl_cnt, "type_counts": type_counts_list}

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

def generate_targeted_bboxes(master_map, existing_latest):
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    missing_or_old_ships = []

    for mmsi in master_map.keys():
        if mmsi not in existing_latest:
            continue 
            
        ship = existing_latest[mmsi]
        ts_str = ship.get("timestamp", "")
        last_time = robust_parse_timestamp(ts_str)
        hours_offline = (now_utc - last_time).total_seconds() / 3600.0
        
        if hours_offline > 24:
            lat = ship.get("lat")
            lng = ship.get("lng")
            if lat is not None and lng is not None:
                expansion = min(20.0, hours_offline * 0.35) 
                missing_or_old_ships.append((lat, lng, expansion))
    
    targeted_bboxes = []
    missing_or_old_ships.sort(key=lambda x: x[2], reverse=True)
    
    for lat, lng, exp in missing_or_old_ships:
        min_lat = max(-90, lat - exp - 2)
        max_lat = min(90, lat + exp + 2)
        min_lng = max(-180, lng - exp - 2)
        max_lng = min(180, lng + exp + 2)
        targeted_bboxes.append([[min_lat, min_lng], [max_lat, max_lng]])
    
    base_bboxes = [
        [[-40, -20], [35, 60]],  
        [[-15, 60], [30, 140]],  
        [[-90, -180], [90, 180]] 
    ]
    
    final_bboxes = targeted_bboxes[:5] + base_bboxes
    return final_bboxes

async def process_message_queue(queue, target_mmsis, received):
    while True:
        msg = await queue.get()
        try:
            data = json.loads(msg)
            mmsi = str(data.get("MetaData", {}).get("MMSI", "")).strip().split(".")[0]
            
            if mmsi in target_mmsis:
                if mmsi not in received:
                    received[mmsi] = {
                        "mmsi": mmsi, "lat": None, "lng": None, "speed": 0.0, "heading": 0.0,
                        "name": data.get("MetaData", {}).get("ShipName", "").strip(),
                        "destination": "", "eta": "", "timestamp": data.get("MetaData", {}).get("time_utc")
                    }
                
                msg_type = data.get("MessageType")
                if msg_type == "PositionReport":
                    pos = data.get("Message", {}).get("PositionReport", {})
                    received[mmsi]["lat"] = pos.get("Latitude", received[mmsi]["lat"])
                    received[mmsi]["lng"] = pos.get("Longitude", received[mmsi]["lng"])
                    received[mmsi]["speed"] = round(pos.get("Sog", 0.0), 1)
                elif msg_type == "ShipStaticData":
                    static = data.get("Message", {}).get("ShipStaticData", {})
                    if dest := static.get("Destination", "").strip():
                        received[mmsi]["destination"] = dest
                    eta = static.get("Eta", {})
                    if eta and eta.get("Month"):
                        received[mmsi]["eta"] = f"{eta.get('Month'):02d}-{eta.get('Day'):02d} {eta.get('Hour'):02d}:{eta.get('Minute'):02d}"
        except Exception:
            pass
        finally:
            queue.task_done()

async def collect_ais_data(target_mmsis, bboxes):
    received = {}
    if not AISSTREAM_API_KEY: return received
    
    duration = 360 if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else 60
    url = "wss://stream.aisstream.io/v0/stream"
    sub_msg = {"APIKey": AISSTREAM_API_KEY, "BoundingBoxes": bboxes, "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    queue = asyncio.Queue()
    consumer_task = asyncio.create_task(process_message_queue(queue, target_mmsis, received))

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps(sub_msg))
            start = time.time()
            while time.time() - start < duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    await queue.put(msg)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
    except Exception as e: 
        print(f"[ERROR] WebSocket connection: {e}")
        
    await queue.join()
    consumer_task.cancel()
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

def simplify_line(coords, tolerance=0.002):
    if len(coords) < 3: return coords
    try:
        line = LineString([(c[1], c[0]) for c in coords])
        simplified = line.simplify(tolerance, preserve_topology=False)
        return [[round(c[1], 5), round(c[0], 5)] for c in simplified.coords]
    except Exception:
        return coords

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

def update_history_and_generate_tracks(live_ships, master_map, existing_latest):
    now_utc = datetime.now(timezone.utc)
    month_str = now_utc.strftime("%Y-%m")
    
    history_dir = os.path.join(DATA_DIR, "history", str(CURRENT_YEAR))
    os.makedirs(history_dir, exist_ok=True)
    history_file_path = os.path.join(history_dir, f"history_{month_str}.csv.gz")

    new_history_rows = []
    for mmsi, ship in live_ships.items():
        if mmsi not in master_map: continue
        if ship["lat"] is None or ship["lng"] is None: continue
        timestamp = now_utc.strftime("%Y/%m/%d %H:%M:%S")
        row = [timestamp, mmsi, ship["lat"], ship["lng"], ship["speed"], ship["heading"]]
        new_history_rows.append(row)

    if new_history_rows:
        file_exists = os.path.exists(history_file_path)
        with gzip.open(history_file_path, "at", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "MMSI", "Lat", "Lng", "Speed", "Heading"])
            writer.writerows(new_history_rows)

    tracks_map = defaultdict(list)
    
    # ★ 改善：data/history 内のすべての csv と csv.gz を見つけ出し、BOMを無視して安全に読み込む
    history_base_dir = os.path.join(DATA_DIR, "history")
    if os.path.exists(history_base_dir):
        # サブフォルダ含め再帰的に探索
        all_csv_files = glob.glob(os.path.join(history_base_dir, "**", "*.csv*"), recursive=True)
        for file_path in all_csv_files:
            if not (file_path.endswith('.csv') or file_path.endswith('.csv.gz')):
                continue
            try:
                open_func = gzip.open if file_path.endswith('.csv.gz') else open
                # utf-8-sig を指定し、Excel等の不要なBOMを排除
                with open_func(file_path, "rt", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # ヘッダーをスキップ
                    for idx, row in enumerate(reader):
                        if len(row) < 4: continue
                        mmsi_str = str(row[1]).strip()
                        if mmsi_str not in master_map: continue
                        try:
                            ts = robust_parse_timestamp(row[0])
                            pt = [round(float(row[2]), 4), round(float(row[3]), 4)]
                            tracks_map[mmsi_str].append((ts, pt, idx))
                        except: continue
            except Exception as e:
                print(f"Error reading history file {file_path}: {e}")

    routes_output = []
    calculated_headings = {}
    grid_mmsi_map = defaultdict(set)
    GRID_SIZE = 0.08

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
                    cleaned_list.append((datetime.now(timezone.utc).replace(tzinfo=None), latest_coord))

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

        smoothed_sub_lines = []
        for sub in sub_lines:
            if len(sub) > 200:
                step = len(sub) / 200.0
                sub_sampled = [sub[int(j * step)] for j in range(200)]
                if sub_sampled[-1] != sub[-1]: sub_sampled.append(sub[-1])
            else:
                sub_sampled = sub
            
            smoothed = smooth_line(sub_sampled, iterations=2)
            smoothed = simplify_line(smoothed, tolerance=0.002)
            smoothed_sub_lines.append(smoothed)

        total_segments = sum(len(s) - 1 for s in smoothed_sub_lines if len(s) >= 2)
        segments = []

        if total_segments > 0:
            current_seg_idx = 0
            for smoothed in smoothed_sub_lines:
                for i in range(len(smoothed) - 1):
                    p1, p2 = smoothed[i], smoothed[i+1]
                    ratio = current_seg_idx / max(1, total_segments - 1)
                    opacity = round(0.15 + (0.80 * ratio), 2)
                    
                    mid_lat = (p1[0] + p2[0]) / 2.0
                    mid_lng = (p1[1] + p2[1]) / 2.0
                    g_key = (round(mid_lat / GRID_SIZE), round(mid_lng / GRID_SIZE))
                    grid_mmsi_map[g_key].add(mmsi)

                    segments.append({"coords": [p1, p2], "opacity": opacity, "g_key": g_key})
                    current_seg_idx += 1

        if segments:
            routes_output.append({
                "mmsi": mmsi, 
                "category": category, 
                "heading": heading, 
                "segments": segments
            })

    for r in routes_output:
        for seg in r["segments"]:
            g_key = seg.pop("g_key", None)
            count = len(grid_mmsi_map.get(g_key, set()))
            w_mult = min(3.2, 1.0 + (0.5 * math.log2(max(1, count))))
            seg["weight_multiplier"] = round(w_mult, 2)

    # ★ 改善：計算できた進行方向（Heading）のみ更新し、計算できない場合は前回の向きを維持する
    for mmsi, ship in existing_latest.items():
        ship["heading"] = calculated_headings.get(mmsi, ship.get("heading", 0.0))

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_output, f, ensure_ascii=False)

    return calculated_headings

def update_coverage_from_all_latest(existing_latest):
    current_time = time.time()
    six_months_ago = current_time - EXPIRATION_SECONDS

    existing_points_dict = {}
    if os.path.exists(COVERAGE_POINTS_FILE):
        try:
            with open(COVERAGE_POINTS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                for item in raw_data:
                    if len(item) >= 2:
                        lat, lng = item[0], item[1]
                        ts = item[2] if len(item) >= 3 else current_time
                        existing_points_dict[(lat, lng)] = ts
        except Exception:
            pass

    for mmsi, ship in existing_latest.items():
        lat = ship.get("lat")
        lng = ship.get("lng")
        if lat is not None and lng is not None:
            ts_str = ship.get("timestamp", "")
            dt = robust_parse_timestamp(ts_str)
            ts = dt.replace(tzinfo=timezone.utc).timestamp() if dt != datetime.min else current_time
            existing_points_dict[(round(float(lat), 2), round(float(lng), 2))] = ts

    active_points = {}
    for coords, ts in existing_points_dict.items():
        if ts >= six_months_ago:
            active_points[coords] = ts

    merged_points = [[k[0], k[1], v] for k, v in active_points.items()]
    with open(COVERAGE_POINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged_points, f, ensure_ascii=False)

    if active_points:
        BUFFER_DEG = 0.33
        circles = [Point(lng, lat).buffer(BUFFER_DEG) for lat, lng in active_points.keys()]
        merged_polygon = unary_union(circles)
        
        coverage_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": mapping(merged_polygon),
                    "properties": {
                        "name": "Global AIS Coverage (Active)",
                        "buffer_nm": 20
                    }
                }
            ]
        }
        with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(coverage_geojson, f, ensure_ascii=False)

def main():
    client = get_spreadsheet_client()
    if not client: return
    master_map, master_counts = get_master_data(client)
    
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                for s in json.load(f).get("latest", []):
                    m_key = str(s.get("mmsi", "")).strip().split(".")[0]
                    if m_key and m_key in master_map:
                        existing_latest[m_key] = s
        except Exception: pass

    bboxes = generate_targeted_bboxes(master_map, existing_latest)

    live_ships = asyncio.run(collect_ais_data(set(master_map.keys()), bboxes))

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
            # ★ 改善：初回の処理では過去の進行方向を引き継ぐ（ゼロリセットを防ぐ）
            "heading": existing_latest.get(mmsi, {}).get("heading", 0.0),
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

    update_coverage_from_all_latest(existing_latest)
    update_history_and_generate_tracks(live_ships, master_map, existing_latest)
    
    recent_count = len([mmsi for mmsi, ship in live_ships.items() if ship.get("lat") is not None and ship.get("lng") is not None])

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "masterCounts": master_counts, 
            "recent_count": recent_count,
            "latest": list(existing_latest.values())
        }, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
