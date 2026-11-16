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

try:
    from global_land_mask import globe
    import numpy as np
    HAS_LAND_MASK = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "global-land-mask", "numpy"])
        from global_land_mask import globe
        import numpy as np
        HAS_LAND_MASK = True
    except Exception:
        HAS_LAND_MASK = False

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

MAX_GENERAL_SPEED_KNOTS = 26.0
MIN_TIME_HRS_FOR_SPEED  = 1.0  
SPIKE_COS_THETA         = -0.3
SPIKE_LEG_MIN_DEG       = 0.2

TSUGARU_STRAIT = [41.45, 140.55]
SOYA_STRAIT    = [45.65, 141.95]

GLOBAL_SAFE_GRIDS = set()
GLOBAL_ALGO_MAP = {}
GLOBAL_AVOID_NORTHERN = set()
MACRO_RESOLUTION = 2.0


def get_spreadsheet_client():
    if not GCP_SA_KEY:
        return None
    sa_info = json.loads(GCP_SA_KEY)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(sa_info, scopes=scopes))


def load_algorithm_map(client):
    """
    スプレッドシートからエリアごとのアルゴリズム設定と回避フラグ（北方領土・竹島等）を読み込む
    """
    global GLOBAL_ALGO_MAP, GLOBAL_AVOID_NORTHERN
    GLOBAL_ALGO_MAP.clear()
    GLOBAL_AVOID_NORTHERN.clear()
    
    if not client: return
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Algorithm_Map")
        records = sheet.get_all_records()
        for r in records:
            try:
                lat_b = float(r.get("Lat_Block", 0))
                lng_b = float(r.get("Lng_Block", 0))
                k = (lat_b, lng_b)
                
                algo = str(r.get("Manual_Algorithm", "")).strip()
                if not algo and "Manual_Override" in r:
                    algo = str(r.get("Manual_Override", "")).strip()
                if not algo:
                    algo = str(r.get("Auto_Algorithm", "")).strip()
                GLOBAL_ALGO_MAP[k] = algo
                
                avoid = str(r.get("Manual_Avoid_Northern", "")).strip().upper()
                if avoid not in ("TRUE", "FALSE"):
                    avoid = str(r.get("Auto_Avoid_Northern", "")).strip().upper()
                if avoid == "TRUE":
                    GLOBAL_AVOID_NORTHERN.add(k)
            except Exception:
                pass
    except Exception as e:
        pass


def get_route_algorithm(p1, p2):
    k1 = (math.floor(p1[0] / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
          math.floor(p1[1] / MACRO_RESOLUTION) * MACRO_RESOLUTION)
    k2 = (math.floor(p2[0] / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
          math.floor(p2[1] / MACRO_RESOLUTION) * MACRO_RESOLUTION)
          
    if GLOBAL_ALGO_MAP.get(k1) == "主要航路ベース" or GLOBAL_ALGO_MAP.get(k2) == "主要航路ベース":
        return "主要航路ベース"
    return "測定点ベース"


def parse_gt(gt_val):
    try:
        clean = str(gt_val).replace(",", "").strip()
        return float(clean)
    except Exception:
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
        if not mmsi or mmsi in ("nan", "None", "0"):
            continue
        
        gt_num = parse_gt(r.get("GT", 0))
        if gt_num > 0 and gt_num < 2000:
            continue
            
        cat = str(r.get("Category", "日本船社運航船")).strip()
        if cat == "日本籍船":
            flag_cnt += 1
        else:
            ctrl_cnt += 1
        
        ship_type = str(r.get("Ship Type", "-")).strip()
        if not ship_type:
            ship_type = "-"
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


def get_port_dictionary(client):
    port_dict = {}
    existing_ais_names = set()
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Port_Dictionary")
        records = sheet.get_all_records()
        for r in records:
            ais_name = str(r.get("変換元(AIS表記)", "")).strip().upper()
            if ais_name:
                existing_ais_names.add(ais_name)
                lat = r.get("Lat")
                lng = r.get("Lng")
                name = r.get("寄港地名(標準)")
                if lat and lng:
                    try:
                        port_dict[ais_name] = {
                            "name": str(name),
                            "lat": float(lat),
                            "lng": float(lng)
                        }
                    except ValueError:
                        pass
    except Exception:
        pass
    return port_dict, existing_ais_names


def robust_parse_timestamp(ts_str):
    ts_str = str(ts_str).strip()
    if not ts_str:
        return datetime.min
    formats = [
        "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S"
    ]
    clean_ts = ts_str.split(".")[0].replace(" +0000 UTC", "").replace(" UTC", "").replace("Z", "").replace("T", " ")
    for fmt in formats:
        try:
            return datetime.strptime(clean_ts, fmt)
        except ValueError:
            pass
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
        if last_time == datetime.min:
            continue
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
                    if data.get("MetaData", {}).get("time_utc"):
                        received[mmsi]["timestamp"] = data.get("MetaData", {}).get("time_utc")
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
    if not AISSTREAM_API_KEY:
        return received
    
    duration = 360 if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else 30
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
    except Exception: 
        pass
        
    await queue.join()
    consumer_task.cancel()
    return received


def is_point_on_land(lat, lng):
    if not HAS_LAND_MASK:
        return False
    try:
        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
            return bool(globe.is_land(lat, lng))
    except Exception:
        pass
    return False


def route_crosses_land(coords):
    if not HAS_LAND_MASK or len(coords) < 2:
        return False

    p_start = coords[0]
    p_end = coords[-1]

    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist == 0: continue
        
        steps = max(2, int(math.ceil(dist / 0.005)))
        for s in range(1, steps):
            t = s / float(steps)
            lat = a[0] + (b[0] - a[0]) * t
            lng = a[1] + (b[1] - a[1]) * t
            
            if math.hypot(lat - p_start[0], lng - p_start[1]) < 0.01: continue
            if math.hypot(lat - p_end[0], lng - p_end[1]) < 0.01: continue
            
            if is_point_on_land(lat, lng):
                return True
                
    return False


def intersects_northern_territories(coords):
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist == 0: continue
        
        steps = max(2, int(math.ceil(dist / 0.1)))
        for s in range(steps + 1):
            t = s / float(steps)
            lat = a[0] + (b[0] - a[0]) * t
            lng = a[1] + (b[1] - a[1]) * t
            
            if GLOBAL_AVOID_NORTHERN:
                k = (math.floor(lat / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
                     math.floor(lng / MACRO_RESOLUTION) * MACRO_RESOLUTION)
                if k in GLOBAL_AVOID_NORTHERN:
                    return True
            else:
                if 43.2 <= lat <= 45.6 and 144.9 <= lng <= 149.0:
                    return True
    return False


def get_offshore_candidates(pt):
    candidates = []
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.707, 0.707), (0.707, -0.707), (-0.707, 0.707), (-0.707, -0.707)]
    for radius in (0.05, 0.1, 0.2):
        for dy, dx in dirs:
            n_lat, n_lng = round(pt[0] + dy * radius, 4), round(pt[1] + dx * radius, 4)
            if not is_point_on_land(n_lat, n_lng):
                candidates.append([n_lat, n_lng])
    return candidates


def get_searoute_path(p1, p2):
    try:
        route = sr.searoute((p1[1], p1[0]), (p2[1], p2[0]))
        coords = route.get("geometry", {}).get("coordinates", [])
        if coords and len(coords) >= 2:
            res = [[round(c[1], 4), round(c[0], 4)] for c in coords]
            if len(res) > 2:
                return res
            
            cands1 = get_offshore_candidates(p1)
            cands2 = get_offshore_candidates(p2)
            for o1 in cands1:
                for o2 in cands2:
                    try:
                        alt_route = sr.searoute((o1[1], o1[0]), (o2[1], o2[0]))
                        alt_coords = alt_route.get("geometry", {}).get("coordinates", [])
                        if alt_coords and len(alt_coords) > 2:
                            return [p1] + [[round(c[1], 4), round(c[0], 4)] for c in alt_coords] + [p2]
                    except Exception:
                        pass
            return res
    except Exception:
        pass
    return None


def string_pull_path(path, max_lookahead=6):
    if len(path) <= 2:
        return path
    smoothed_path = [path[0]]
    current_idx = 0
    
    while current_idx < len(path) - 1:
        furthest_visible_idx = current_idx + 1
        search_end = min(len(path) - 1, current_idx + max_lookahead)
        for check_idx in range(search_end, current_idx, -1):
            if not route_crosses_land([path[current_idx], path[check_idx]]) and not intersects_northern_territories([path[current_idx], path[check_idx]]):
                furthest_visible_idx = check_idx
                break
        smoothed_path.append(path[furthest_visible_idx])
        current_idx = furthest_visible_idx
    return smoothed_path


def moving_average_smooth(path, window_size=7):
    if len(path) < window_size:
        return path
    smoothed = [path[0]]
    for i in range(1, len(path)-1):
        start = max(0, i - window_size // 2)
        end = min(len(path), i + window_size // 2 + 1)
        window = path[start:end]
        avg_lat = sum(p[0] for p in window) / len(window)
        avg_lng = sum(p[1] for p in window) / len(window)
        smoothed.append([avg_lat, avg_lng])
    smoothed.append(path[-1])
    return smoothed


def get_crowd_sourced_path(p1, p2):
    global GLOBAL_SAFE_GRIDS
    resolution = 0.25
    start_gk = (int(round(p1[0] / resolution)), int(round(p1[1] / resolution)))
    end_gk = (int(round(p2[0] / resolution)), int(round(p2[1] / resolution)))
    
    if math.hypot(start_gk[0]-end_gk[0], start_gk[1]-end_gk[1]) <= 1.5:
        return [p1, p2]
        
    open_set = {start_gk}
    came_from = {}
    g_score = {start_gk: 0}
    
    def heuristic(node):
        return math.hypot(node[0]-end_gk[0], node[1]-end_gk[1]) * 1.0
        
    f_score = {start_gk: heuristic(start_gk)}
    
    iterations = 0
    while open_set and iterations < 20000:
        iterations += 1
        current = min(open_set, key=lambda x: f_score.get(x, float('inf')))
        
        if current == end_gk:
            path = [p2]
            curr_node = current
            while curr_node in came_from:
                curr_node = came_from[curr_node]
                if curr_node != start_gk:
                    path.append([round(curr_node[0]*resolution, 4), round(curr_node[1]*resolution, 4)])
            path.append(p1)
            path.reverse()
            
            if len(path) <= 2:
                return path
                
            smoothed_path = moving_average_smooth(path, window_size=9)
            
            sampled = [smoothed_path[0]]
            step = 6 
            for k in range(step, len(smoothed_path) - 1, step):
                sampled.append(smoothed_path[k])
            sampled.append(smoothed_path[-1])
            
            if not route_crosses_land(sampled) and not intersects_northern_territories(sampled):
                return sampled
                
            return string_pull_path(smoothed_path, max_lookahead=6)
            
        open_set.remove(current)
        
        for dx, dy in [(0,1),(1,0),(0,-1),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)]:
            neighbor = (current[0]+dx, current[1]+dy)
            dist = math.hypot(dx, dy)
            penalty = 1.0 if neighbor in GLOBAL_SAFE_GRIDS else 10.0
            
            if not (neighbor in GLOBAL_SAFE_GRIDS):
                if is_point_on_land(neighbor[0]*resolution, neighbor[1]*resolution):
                    penalty = 200.0
                    
            tentative_g = g_score[current] + (dist * penalty)
            
            if tentative_g < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                f_score[neighbor] = tentative_g + heuristic(neighbor)
                open_set.add(neighbor)
                
    return None


def reroute_via_straits(p1, p2):
    best_alt = None
    best_alt_dist = float('inf')
    for via in [TSUGARU_STRAIT, SOYA_STRAIT]:
        r1 = get_searoute_path(p1, via)
        r2 = get_searoute_path(via, p2)
        if r1 and r2:
            alt_res = r1 + r2[1:]
            if not intersects_northern_territories(alt_res):
                d = sum(math.hypot(alt_res[i+1][0] - alt_res[i][0], alt_res[i+1][1] - alt_res[i][1]) for i in range(len(alt_res)-1))
                if d < best_alt_dist:
                    best_alt_dist = d
                    best_alt = alt_res
    return best_alt


def query_valid_searoute(p1, p2):
    res = get_searoute_path(p1, p2)
    if not res:
        return None
    
    if math.hypot(res[0][0] - p1[0], res[0][1] - p1[1]) > 0.001: res.insert(0, p1)
    if math.hypot(res[-1][0] - p2[0], res[-1][1] - p2[1]) > 0.001: res.append(p2)
    
    if intersects_northern_territories(res):
        alt_res = reroute_via_straits(p1, p2)
        if alt_res:
            return alt_res
        return None
        
    return res


def remove_d_spikes(pts):
    if len(pts) < 3:
        return pts
    
    cleaned = [pts[0]]
    for i in range(1, len(pts) - 1):
        prev_p = cleaned[-1]
        curr_p = pts[i]
        next_p = pts[i + 1]
        
        v1x, v1y = curr_p[0] - prev_p[0], curr_p[1] - prev_p[1]
        v2x, v2y = next_p[0] - curr_p[0], next_p[1] - curr_p[1]
        
        norm1 = math.hypot(v1x, v1y)
        norm2 = math.hypot(v2x, v2y)
        
        if norm1 > 0 and norm2 > 0:
            cos_theta = (v1x * v2x + v1y * v2y) / (norm1 * norm2)
            if cos_theta < SPIKE_COS_THETA and norm1 > SPIKE_LEG_MIN_DEG and norm2 > SPIKE_LEG_MIN_DEG:
                if not route_crosses_land([prev_p, next_p]):
                    continue
                
        cleaned.append(curr_p)
        
    cleaned.append(pts[-1])
    return cleaned


def subdivide_long_segments(coords, max_step_deg=0.1):
    if len(coords) < 2:
        return coords
    subdivided = [coords[0]]
    for i in range(1, len(coords)):
        p1 = subdivided[-1]
        p2 = coords[i]
        dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if dist > max_step_deg:
            n = int(math.ceil(dist / max_step_deg))
            for k in range(1, n):
                t = k / float(n)
                subdivided.append([
                    round(p1[0] + (p2[0] - p1[0]) * t, 5),
                    round(p1[1] + (p2[1] - p1[1]) * t, 5)
                ])
        subdivided.append(p2)
    return subdivided


def catmull_rom_chain(coords):
    if len(coords) < 3:
        return coords
        
    pts = [coords[0]]
    for c in coords[1:]:
        if abs(c[0] - pts[-1][0]) > 1e-6 or abs(c[1] - pts[-1][1]) > 1e-6:
            pts.append(c)
    if len(pts) < 3:
        return pts
        
    p_start = [2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1]]
    p_end = [2 * pts[-1][0] - pts[-2][0], 2 * pts[-1][1] - pts[-2][1]]
    ext_pts = [p_start] + pts + [p_end]
    
    alpha = 0.5 
    result = []

    def tj(ti, pi, pj):
        dist = math.hypot(pj[0] - pi[0], pj[1] - pi[1])
        return ti + max(1e-5, dist ** alpha)

    for i in range(len(ext_pts) - 3):
        p0, p1, p2, p3 = ext_pts[i], ext_pts[i + 1], ext_pts[i + 2], ext_pts[i + 3]
        t0 = 0.0
        t1 = tj(t0, p0, p1)
        t2 = tj(t1, p1, p2)
        t3 = tj(t2, p2, p3)
        seg_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        n_steps = max(10, min(30, int(seg_dist / 0.02) + 5))
        
        for step in range(n_steps):
            t = t1 + (t2 - t1) * (step / float(n_steps))
            a1 = [(t1 - t) / (t1 - t0) * p0[0] + (t - t0) / (t1 - t0) * p1[0],
                  (t1 - t) / (t1 - t0) * p0[1] + (t - t0) / (t1 - t0) * p1[1]]
            a2 = [(t2 - t) / (t2 - t1) * p1[0] + (t - t1) / (t2 - t1) * p2[0],
                  (t2 - t) / (t2 - t1) * p1[1] + (t - t1) / (t2 - t1) * p2[1]]
            a3 = [(t3 - t) / (t3 - t2) * p2[0] + (t - t2) / (t3 - t2) * p3[0],
                  (t3 - t) / (t3 - t2) * p2[1] + (t - t2) / (t3 - t2) * p3[1]]
            b1 = [(t2 - t) / (t2 - t0) * a1[0] + (t - t0) / (t2 - t0) * a2[0],
                  (t2 - t) / (t2 - t0) * a1[1] + (t - t0) / (t2 - t0) * a2[1]]
            b2 = [(t3 - t) / (t3 - t1) * a2[0] + (t - t1) / (t3 - t1) * a3[0],
                  (t3 - t) / (t3 - t1) * a2[1] + (t - t1) / (t3 - t1) * a3[1]]
            c = [(t2 - t) / (t2 - t1) * b1[0] + (t - t1) / (t2 - t1) * b2[0],
                 (t2 - t) / (t2 - t1) * b1[1] + (t - t1) / (t2 - t1) * b2[1]]
            result.append([round(c[0], 5), round(c[1], 5)])
            
    result.append([round(pts[-1][0], 5), round(pts[-1][1], 5)])
    return result


def interpolate_sea_route_linear(p1, p2, time_diff_hrs=None, is_port_route=False, prev_speed=0.0):
    straight_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    
    algo = get_route_algorithm(p1, p2)
    is_main_route = (algo == "主要航路ベース")
    
    direct_crosses = route_crosses_land([p1, p2])
    direct_northern = intersects_northern_territories([p1, p2])

    res = None

    if is_main_route:
        if straight_dist < 0.03 and not direct_crosses and not direct_northern:
            return [p1, p2]
        res = query_valid_searoute(p1, p2)
        if res:
            res = string_pull_path(res, max_lookahead=12)
    else:
        if straight_dist < 1.5 and not direct_crosses and not direct_northern:
            return [p1, p2]
            
        crowd_res = get_crowd_sourced_path(p1, p2)
        if crowd_res and not route_crosses_land(crowd_res) and not intersects_northern_territories(crowd_res):
            res = crowd_res
        else:
            res = query_valid_searoute(p1, p2)
            if res:
                res = subdivide_long_segments(res, max_step_deg=0.1)
                res = string_pull_path(res, max_lookahead=15)
                
        if not res:
            if not direct_crosses and not direct_northern:
                if straight_dist < 1.5:  
                    return [p1, p2]
            return None 

    if not is_port_route:
        if res: res = remove_d_spikes(res)
        if not res or len(res) < 2:
            res = [p1, p2]

        route_dist = sum(math.hypot(res[i+1][0] - res[i][0], res[i+1][1] - res[i][1]) for i in range(len(res) - 1))
        
        detour_limit = 4.0 if is_main_route else 2.0
        excess_limit = 1.0 if is_main_route else 0.4
        
        if route_dist > (straight_dist * detour_limit) and (route_dist - straight_dist) > excess_limit:
            if straight_dist < 1.5 and not direct_crosses and not direct_northern:
                res = [p1, p2]
            else:
                return None

        if time_diff_hrs and time_diff_hrs >= MIN_TIME_HRS_FOR_SPEED:
            speed_knots = (route_dist * 60.0) / time_diff_hrs
            if speed_knots > MAX_GENERAL_SPEED_KNOTS:
                return None

    if abs(res[0][0] - p1[0]) > 0.001 or abs(res[0][1] - p1[1]) > 0.001:
        res.insert(0, p1)
    else:
        res[0] = p1
    if abs(res[-1][0] - p2[0]) > 0.001 or abs(res[-1][1] - p2[1]) > 0.001:
        res.append(p2)
    else:
        res[-1] = p2

    return res


def generate_inferred_port_route(ship_history, current_pos, current_ts, current_dest, port_dict):
    if not current_pos or current_ts == datetime.min:
        return None
    
    reversed_history = list(reversed(ship_history))
    past_received_pos = None
    past_dest_str = None
    past_ts = datetime.min
    
    current_dest_upper = str(current_dest).strip().upper()
    if not current_dest_upper or current_dest_upper in ("-", "", "NONE"):
        return None
    
    for ts, pt, idx, r_dest, r_spd in reversed_history:
        if not r_dest or r_dest in ("-", "", "NONE"):
            continue
        if r_dest != current_dest_upper:
            past_dest_str = r_dest
            past_received_pos = pt
            past_ts = ts
            break
            
    if not past_dest_str or not past_received_pos or past_ts == datetime.min:
        return None
    port_info = port_dict.get(past_dest_str)
    if not port_info:
        return None
    port_pos = [port_info["lat"], port_info["lng"]]

    is_china = past_dest_str.startswith("CN") or (18.0 <= port_pos[0] <= 41.0 and 105.0 <= port_pos[1] <= 125.0)
    if not is_china:
        return None

    time_diff_hours = (current_ts - past_ts).total_seconds() / 3600.0
    if time_diff_hours <= 0:
        return None
        
    dist_deg = math.hypot(past_received_pos[0] - port_pos[0], past_received_pos[1] - port_pos[1]) + \
               math.hypot(current_pos[0] - port_pos[0], current_pos[1] - port_pos[1])
    dist_nm = dist_deg * 60.0  
    req_speed = dist_nm / time_diff_hours
    
    if req_speed > 22.0:
        return None

    for _, pt, _, _, _ in reversed_history:
        if math.hypot(pt[0] - port_pos[0], pt[1] - port_pos[1]) < 4.0:
            return None
        
    inferred_route_coords = []
    try:
        c1_list = interpolate_sea_route_linear(past_received_pos, port_pos, is_port_route=True)
        if not c1_list:
            return None
        inferred_route_coords.extend(c1_list)
        c2_list = interpolate_sea_route_linear(port_pos, current_pos, is_port_route=True)
        if not c2_list:
            return None
        inferred_route_coords.extend(c2_list[1:])
    except Exception:
        return None
    if len(inferred_route_coords) < 2:
        return None
    return {
        "port_name": port_info["name"],
        "coords": inferred_route_coords
    }


def register_segment_to_grid(p1, p2, mmsi, grid_map, grid_size):
    dist = max(abs(p2[0] - p1[0]), abs(p2[1] - p1[1]))
    steps = max(1, int(dist / (grid_size * 0.5)))
    keys = set()
    for s in range(steps + 1):
        t = s / float(steps)
        lat = p1[0] + (p2[0] - p1[0]) * t
        lng = p1[1] + (p2[1] - p1[1]) * t
        gk = (round(lat / grid_size), round(lng / grid_size))
        grid_map[gk].add(mmsi)
        keys.add(gk)
    return list(keys)


def update_history_and_generate_tracks(live_ships, master_map, existing_latest, port_dict):
    now_utc = datetime.now(timezone.utc)
    month_str = now_utc.strftime("%Y-%m")
    
    history_dir = os.path.join(DATA_DIR, "history", str(CURRENT_YEAR))
    os.makedirs(history_dir, exist_ok=True)
    history_file_path = os.path.join(history_dir, f"history_{month_str}.csv.gz")

    new_history_rows = []
    for mmsi, ship in live_ships.items():
        if mmsi not in master_map:
            continue
        if ship["lat"] is None or ship["lng"] is None:
            continue
        timestamp = now_utc.strftime("%Y/%m/%d %H:%M:%S")
        dest = ship.get("destination", "")
        eta = ship.get("eta", "")
        row = [timestamp, mmsi, ship["lat"], ship["lng"], ship["speed"], ship["heading"], dest, eta]
        new_history_rows.append(row)

    if new_history_rows:
        file_exists = os.path.exists(history_file_path)
        with gzip.open(history_file_path, "at", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "MMSI", "Lat", "Lng", "Speed", "Heading", "Destination", "ETA"])
            writer.writerows(new_history_rows)

    tracks_map = defaultdict(list)
    
    if os.path.exists(history_dir):
        for file in sorted(os.listdir(history_dir)):
            if not (file.endswith('.csv') or file.endswith('.csv.gz')):
                continue
            file_path = os.path.join(history_dir, file)
            try:
                open_func = gzip.open if file_path.endswith('.csv.gz') else open
                with open_func(file_path, "rt", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    c_ts, c_mmsi, c_lat, c_lng, c_spd = 0, 1, 2, 3, 4
                    for i, col_name in enumerate(header):
                        c_name = col_name.strip().lower()
                        if "lat" in c_name or "緯度" in c_name:
                            c_lat = i
                        elif "lon" in c_name or "lng" in c_name or "経度" in c_name:
                            c_lng = i
                        elif "mmsi" in c_name:
                            c_mmsi = i
                        elif "time" in c_name or "date" in c_name or "timestamp" in c_name or "日付" in c_name:
                            c_ts = i
                        elif "speed" in c_name or "sog" in c_name or "速力" in c_name:
                            c_spd = i

                    for idx, row in enumerate(reader):
                        if len(row) < 4:
                            continue
                        r_dest = ""
                        r_spd = 0.0
                        if len(row) == 6 or len(row) == 8:
                            r_ts, r_mmsi, r_lat, r_lng = row[0], row[1], row[2], row[3]
                            try:
                                r_spd = float(row[4])
                            except Exception:
                                pass
                            if len(row) == 8:
                                r_dest = str(row[6]).strip().upper()
                        elif len(row) > max(c_lat, c_lng, c_mmsi, c_ts):
                            r_ts, r_mmsi, r_lat, r_lng = row[c_ts], row[c_mmsi], row[c_lat], row[c_lng]
                            try:
                                if len(row) > c_spd:
                                    r_spd = float(row[c_spd])
                            except Exception:
                                pass
                            if len(row) > 6:
                                r_dest = str(row[6]).strip().upper()
                        else:
                            continue

                        mmsi_str = str(r_mmsi).strip().split(".")[0]
                        if mmsi_str not in master_map:
                            continue
                        try:
                            lat_val = float(r_lat)
                            lng_val = float(r_lng)
                            if not (-90.0 <= lat_val <= 90.0 and -180.0 <= lng_val <= 180.0):
                                continue
                            ts = robust_parse_timestamp(r_ts)
                            if ts == datetime.min:
                                continue
                            pt = [round(lat_val, 4), round(lng_val, 4)]
                            tracks_map[mmsi_str].append((ts, pt, idx, r_dest, r_spd))
                        except Exception:
                            continue
            except Exception:
                pass

    global GLOBAL_SAFE_GRIDS
    GLOBAL_SAFE_GRIDS.clear()
    CROWD_RESOLUTION = 0.25
    
    if os.path.exists(COVERAGE_POINTS_FILE):
        try:
            with open(COVERAGE_POINTS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                for item in raw_data:
                    if len(item) >= 2:
                        gk = (int(round(item[0] / CROWD_RESOLUTION)), int(round(item[1] / CROWD_RESOLUTION)))
                        GLOBAL_SAFE_GRIDS.add(gk)
        except Exception:
            pass

    for mmsi, data_list in tracks_map.items():
        for item in data_list:
            pt = item[1]
            gk = (int(round(pt[0] / CROWD_RESOLUTION)), int(round(pt[1] / CROWD_RESOLUTION)))
            GLOBAL_SAFE_GRIDS.add(gk)

    routes_output = []
    calculated_headings = {}
    grid_all_map = defaultdict(set)
    GRID_SIZE = 0.08

    for mmsi, data_list in tracks_map.items():
        if not data_list:
            continue
        data_list.sort(key=lambda x: (x[0], x[2]))
        
        cleaned_list = []
        for item in data_list:
            if not cleaned_list:
                cleaned_list.append((item[0], item[1], item[4]))
                continue
            
            last_ts, last_pt, last_s = cleaned_list[-1]
            curr_ts, curr_pt, curr_s = item[0], item[1], item[4]
            
            if curr_pt == last_pt:
                continue
                
            time_diff_hrs = (curr_ts - last_ts).total_seconds() / 3600.0
            if time_diff_hrs > 0:
                dist_nm = math.hypot(curr_pt[0] - last_pt[0], curr_pt[1] - last_pt[1]) * 60.0
                speed = dist_nm / time_diff_hrs
                if dist_nm > 3.0 and speed > 50.0:
                    continue
            
            cleaned_list.append((curr_ts, curr_pt, curr_s))
                
        latest_coord = None
        if mmsi in existing_latest and existing_latest[mmsi].get("lat") is not None and existing_latest[mmsi].get("lng") is not None:
            curr_lat = existing_latest[mmsi]["lat"]
            curr_lng = existing_latest[mmsi]["lng"]
            curr_spd = existing_latest[mmsi].get("speed", 0.0)
            latest_coord = [round(float(curr_lat), 4), round(float(curr_lng), 4)]
            latest_ts_str = existing_latest[mmsi].get("timestamp", "")
            latest_ts = robust_parse_timestamp(latest_ts_str)
            if latest_ts == datetime.min:
                latest_ts = datetime.now(timezone.utc).replace(tzinfo=None)

            if not cleaned_list:
                cleaned_list.append((latest_ts, latest_coord, curr_spd))
            elif cleaned_list[-1][1] != latest_coord:
                last_ts, last_pt, last_s = cleaned_list[-1]
                time_diff_hrs = (latest_ts - last_ts).total_seconds() / 3600.0
                if time_diff_hrs > 0:
                    dist_nm = math.hypot(latest_coord[0] - last_pt[0], latest_coord[1] - last_pt[1]) * 60.0
                    if not (dist_nm > 3.0 and (dist_nm / time_diff_hrs) > 50.0):
                        cleaned_list.append((latest_ts, latest_coord, curr_spd))

        if cleaned_list and mmsi not in existing_latest:
            last_t, last_p, last_s = cleaned_list[-1]
            m_info = master_map.get(mmsi, {})
            existing_latest[mmsi] = {
                "mmsi": mmsi,
                "name": m_info.get("name") or f"MMSI:{mmsi}",
                "imo": m_info.get("imo", "-"),
                "category": m_info.get("category", "日本船社運航船"),
                "flag": m_info.get("flag", "-"),
                "type": m_info.get("type", "-"),
                "gt": m_info.get("gt", "-"),
                "owner": m_info.get("owner", "-"),
                "operator": m_info.get("operator", "-"),
                "survey": m_info.get("survey", "-"),
                "lat": last_p[0],
                "lng": last_p[1],
                "speed": last_s,
                "heading": 0.0,
                "destination": "-",
                "eta": "-",
                "timestamp": last_t.strftime("%Y/%m/%d %H:%M:%S") if last_t != datetime.min else ""
            }
            latest_coord = last_p
            
        raw_points = []
        for ts, pt, spd in cleaned_list:
            if ts != datetime.min:
                raw_points.append({"lat": pt[0], "lng": pt[1], "ts": ts.strftime("%m/%d %H:%M"), "mmsi": mmsi})

        if len(cleaned_list) < 2: 
            calculated_headings[mmsi] = 0.0
            continue
            
        category = master_map.get(mmsi, {}).get("category", "外国籍船")

        # 軌跡をブツ切りにせず、全区間を一気通貫のリスト（global_route_coords）として結合する
        global_route_coords = []
        curr_p = cleaned_list[0][1]
        global_route_coords.append(curr_p)

        for i in range(1, len(cleaned_list)):
            prev_t, prev_p, prev_s = cleaned_list[i-1]
            curr_t, curr_p, curr_s = cleaned_list[i]
            
            time_diff_hrs = (curr_t - prev_t).total_seconds() / 3600.0 if prev_t != datetime.min else 0
            
            interpolated = interpolate_sea_route_linear(prev_p, curr_p, time_diff_hrs=time_diff_hrs, prev_speed=prev_s)
            
            if interpolated and len(interpolated) >= 2:
                for pt in interpolated[1:]:
                    if abs(pt[0] - global_route_coords[-1][0]) > 1e-6 or abs(pt[1] - global_route_coords[-1][1]) > 1e-6:
                        global_route_coords.append(pt)
            else:
                if abs(curr_p[0] - global_route_coords[-1][0]) > 1e-6 or abs(curr_p[1] - global_route_coords[-1][1]) > 1e-6:
                    global_route_coords.append(curr_p)

        current_ts_str = existing_latest.get(mmsi, {}).get("timestamp", "")
        current_ts = robust_parse_timestamp(current_ts_str)
        current_dest = existing_latest.get(mmsi, {}).get("destination", "")
        
        inferred_route = generate_inferred_port_route(data_list, latest_coord, current_ts, current_dest, port_dict)
        if inferred_route and len(inferred_route["coords"]) >= 2:
            for pt in inferred_route["coords"][1:]:
                if abs(pt[0] - global_route_coords[-1][0]) > 1e-6 or abs(pt[1] - global_route_coords[-1][1]) > 1e-6:
                    global_route_coords.append(pt)

        if len(global_route_coords) < 2:
            continue

        processed_blocks = []
        
        if len(global_route_coords) > 500:
            step = len(global_route_coords) / 500.0
            sub_sampled = [global_route_coords[int(j * step)] for j in range(500)]
            if sub_sampled[-1] != global_route_coords[-1]:
                sub_sampled.append(global_route_coords[-1])
        else:
            sub_sampled = global_route_coords
            
        # 全区間を一気通貫で巨大なスプライン曲線として処理
        # 実測値（中継地点）での接線の断絶が消え、完全に滑らかな1本のカーブになる
        smoothed = catmull_rom_chain(sub_sampled)
        
        if route_crosses_land(smoothed) or intersects_northern_territories(smoothed):
            smoothed = sub_sampled
            
        if len(smoothed) >= 2:
            processed_blocks.append(smoothed)

        if not processed_blocks: 
            calculated_headings[mmsi] = 0.0
            continue

        last_coords = processed_blocks[-1]
        heading = calculate_bearing(last_coords[-2], last_coords[-1])
        calculated_headings[mmsi] = heading

        segments = []
        for coords in processed_blocks:
            n_pts = len(coords)
            num_chunks = min(12, max(1, (n_pts - 1) // 6))
            chunk_size = (n_pts - 1) / float(num_chunks)
            for c_idx in range(num_chunks):
                s_i = int(round(c_idx * chunk_size))
                e_i = int(round((c_idx + 1) * chunk_size))
                if e_i <= s_i:
                    e_i = min(n_pts - 1, s_i + 1)
                sub_chunk = coords[s_i : e_i + 1]
                if len(sub_chunk) >= 2:
                    chunk_g_keys = set()
                    for k in range(len(sub_chunk) - 1):
                        gks = register_segment_to_grid(sub_chunk[k], sub_chunk[k+1], mmsi, grid_all_map, GRID_SIZE)
                        chunk_g_keys.update(gks)
                    seg_obj = {
                        "coords": sub_chunk,
                        "g_keys": list(chunk_g_keys)
                    }
                    segments.append(seg_obj)

        if segments:
            routes_output.append({
                "mmsi": mmsi, 
                "category": category, 
                "heading": heading, 
                "segments": segments,
                "raw_points": raw_points
            })

    for r in routes_output:
        for seg in r["segments"]:
            g_keys = seg.pop("g_keys", [])
            shared = set()
            if g_keys:
                count = max(len(grid_all_map.get(gk, set())) for gk in g_keys)
                for gk in g_keys:
                    shared.update(grid_all_map.get(gk, set()))
            else:
                count = 1
            if not shared:
                shared.add(r["mmsi"])
                
            w_mult = min(3.2, 1.0 + (0.5 * math.log2(max(1, count))))
            seg["weight_multiplier"] = round(w_mult, 2)
            seg["shared_mmsis"] = list(shared)

    for mmsi, ship_data in existing_latest.items():
        ship_data["heading"] = calculated_headings.get(mmsi, 0.0)
        dest_val = ship_data.get("destination", "-")
        ship_data["dest_name"] = port_dict.get(dest_val.upper(), {}).get("name", "") if dest_val != "-" else ""

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
    if not client:
        return
        
    load_algorithm_map(client)
    
    port_dict, existing_ais_names = get_port_dictionary(client)
    master_map, master_counts = get_master_data(client)
    
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                for s in json.load(f).get("latest", []):
                    m_key = str(s.get("mmsi", "")).strip().split(".")[0]
                    if m_key and m_key in master_map and s.get("lat") is not None and s.get("lng") is not None:
                        existing_latest[m_key] = s
        except Exception:
            pass

    bboxes = generate_targeted_bboxes(master_map, existing_latest)

    live_ships = asyncio.run(collect_ais_data(set(master_map.keys()), bboxes))

    for mmsi, ship in live_ships.items():
        if mmsi not in master_map:
            continue
        m_info = master_map.get(mmsi, {})
        prev = existing_latest.get(mmsi, {})
        
        new_lat = ship["lat"] if ship["lat"] is not None else prev.get("lat")
        new_lng = ship["lng"] if ship["lng"] is not None else prev.get("lng")
        if new_lat is None or new_lng is None:
            continue

        new_speed = ship["speed"] if ship["lat"] is not None else prev.get("speed", 0.0)
        new_ts = ship["timestamp"] if (ship["lat"] is not None and ship.get("timestamp")) else prev.get("timestamp", ship.get("timestamp"))

        existing_latest[mmsi] = {
            "mmsi": mmsi,
            "name": m_info.get("name") or ship["name"] or prev.get("name") or f"MMSI:{mmsi}",
            "imo": m_info.get("imo", "-"),
            "category": m_info.get("category", "日本船社運航船"),
            "flag": m_info.get("flag", "-"),
            "type": m_info.get("type", "-"),
            "gt": m_info.get("gt", "-"),
            "owner": m_info.get("owner", "-"),
            "operator": m_info.get("operator", "-"),
            "survey": m_info.get("survey", "-"),
            "lat": new_lat,
            "lng": new_lng,
            "speed": new_speed,
            "heading": prev.get("heading", 0.0),
            "destination": ship["destination"] or prev.get("destination", "-"),
            "eta": ship["eta"] or prev.get("eta", "-"),
            "timestamp": new_ts
        }

    for mmsi, ship_data in existing_latest.items():
        if mmsi in master_map:
            m = master_map[mmsi]
            if not ship_data.get("name") or ship_data["name"].startswith("MMSI:"):
                ship_data["name"] = m["name"] or ship_data["name"]
            if ship_data.get("imo") in ("-", "", None):
                ship_data["imo"] = m["imo"]
            if ship_data.get("category") in ("日本船社運航船", "", None):
                ship_data["category"] = m["category"]
            if ship_data.get("flag") in ("-", "", None):
                ship_data["flag"] = m["flag"]
            if ship_data.get("type") in ("-", "", None):
                ship_data["type"] = m["type"]
            if ship_data.get("gt") in ("-", "", None):
                ship_data["gt"] = m["gt"]
            if ship_data.get("owner") in ("-", "", None):
                ship_data["owner"] = m["owner"]
            if ship_data.get("operator") in ("-", "", None):
                ship_data["operator"] = m["operator"]
            if ship_data.get("survey") in ("-", "", None):
                ship_data["survey"] = m["survey"]
            
        dest_val = ship_data.get("destination", "-")
        ship_data["dest_name"] = port_dict.get(dest_val.upper(), {}).get("name", "") if dest_val != "-" else ""

    update_history_and_generate_tracks(live_ships, master_map, existing_latest, port_dict)
    update_coverage_from_all_latest(existing_latest)
    
    recent_count = len([mmsi for mmsi, ship in live_ships.items() if ship.get("lat") is not None and ship.get("lng") is not None])

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "masterCounts": master_counts, 
            "recent_count": recent_count,
            "latest": list(existing_latest.values())
        }, f, ensure_ascii=False, indent=2)

    seen_dests = set()
    for ship in existing_latest.values():
        d = str(ship.get("destination", "")).strip().upper()
        if d and d not in ("-", "NONE"):
            seen_dests.add(d)
    
    new_dests = seen_dests - existing_ais_names
    if new_dests:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Port_Dictionary")
            append_data = [[d, "", ""] for d in sorted(list(new_dests))]
            sheet.append_rows(append_data)
            print(f"[INFO] 辞書に {len(new_dests)} 件の新しい仕向地を追加しました。")
        except Exception:
            pass


if __name__ == "__main__":
    main()
