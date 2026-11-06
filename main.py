import os
import sys
import json
import math
import heapq
import asyncio
import datetime
import websockets
import requests
import searoute as sr

try:
    import shapely
    from shapely.geometry import Point, LineString, Polygon, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "shapely"])
    import shapely
    from shapely.geometry import Point, LineString, Polygon, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 幾何学陸地障害物ポリゴン（Shapely図形交差判定用）
# ==============================================================================
LAND_OBSTACLES = unary_union([
    # 羽田空港（D滑走路含む）
    Polygon([(139.74, 35.52), (139.835, 35.52), (139.835, 35.575), (139.74, 35.575)]),
    # 川崎臨海部・扇島・浮島・水江町
    Polygon([(139.69, 35.48), (139.79, 35.48), (139.79, 35.54), (139.69, 35.54)]),
    # 横浜港・本牧・大黒埠頭基部
    Polygon([(139.63, 35.41), (139.70, 35.41), (139.70, 35.48), (139.63, 35.48)]),
    # 房総半島西岸（木更津・君津・富津）
    Polygon([(139.81, 35.10), (140.15, 35.10), (140.15, 35.55), (139.95, 35.55), (139.81, 35.32)]),
    # 三浦半島東岸（横須賀・観音崎）
    Polygon([(139.55, 35.10), (139.725, 35.10), (139.725, 35.35), (139.55, 35.35)])
])

# ==============================================================================
# 2. 海上安全水路ネットワーク（陸地を一切踏まない航路ノード）
# ==============================================================================
MARITIME_NODES = {
    # 瀬戸内海
    "IYO_WEST":     (33.650, 132.200), "IYO_MID":      (33.800, 132.500), "AKI_NADA":     (34.120, 132.700),
    "SAI_WEST":     (34.050, 132.800), "SAI_MID":      (34.090, 132.880), "NOMA_OFF":     (34.135, 132.930),
    "OGE_SETO":     (34.168, 132.970), "KURUSHIMA_W":  (34.138, 133.002), "KURUSHIMA_BRG":(34.118, 133.015),
    "KURUSHIMA_E":  (34.092, 133.045), "HUCHI_W":      (34.110, 133.100), "HUCHI_MID":    (34.200, 133.350),
    "BISAN_W":      (34.330, 133.650), "BISAN_MID":    (34.380, 133.850), "BISAN_E":      (34.420, 134.150),
    "HARIMA_MID":   (34.500, 134.600), "AKASHI_STR":   (34.610, 135.010), "OSAKA_BAY":    (34.550, 135.250),
    "KII_STR":      (34.100, 134.900),
    
    # 東京湾（水域のみを厳密に結ぶノード）
    "URAGA_S":      (35.150, 139.730), # 浦賀水道南口外洋
    "URAGA_N":      (35.250, 139.730), # 浦賀水道航路内
    "TYO_MID":      (35.400, 139.810), # 東京湾中央（中ノ瀬北方海域）
    "YOKOHAMA_OFF": (35.435, 139.710), # 横浜本牧・大黒沖合
    "KAWASAKI_OFF": (35.485, 139.805), # 川崎扇島沖合
    "HANEDA_OFF":   (35.545, 139.845), # 羽田D滑走路東側沖合水域
    "TOKYO_ENT":    (35.590, 139.820), # 東京港航路進入点
    "TOKYO_INN":    (35.615, 139.780), # 東京港内（品川沖）
    "CHIBA_OFF":    (35.540, 140.000), # 千葉港沖合
    "KISARAZU_OFF": (35.410, 139.910), # 木更津沖合

    # 海外狭水路
    "VN_OFFSHORE":  (10.250, 107.030), "VN_ENTRANCE":  (10.380, 107.030), "VN_CMIT_S":    (10.490, 107.020),
    "VN_CMIT_N":    (10.550, 107.015), "VN_THIVAI_UP": (10.600, 107.005),
}

MARITIME_EDGES = [
    # 瀬戸内海
    ("IYO_WEST", "IYO_MID"), ("IYO_MID", "SAI_WEST"), ("IYO_MID", "AKI_NADA"), ("AKI_NADA", "SAI_MID"), 
    ("SAI_WEST", "SAI_MID"), ("SAI_MID", "NOMA_OFF"), ("NOMA_OFF", "OGE_SETO"), ("OGE_SETO", "KURUSHIMA_W"), 
    ("KURUSHIMA_W", "KURUSHIMA_BRG"), ("KURUSHIMA_BRG", "KURUSHIMA_E"), ("KURUSHIMA_E", "HUCHI_W"), 
    ("HUCHI_W", "HUCHI_MID"), ("HUCHI_MID", "BISAN_W"), ("BISAN_W", "BISAN_MID"), ("BISAN_MID", "BISAN_E"),
    ("BISAN_E", "HARIMA_MID"), ("HARIMA_MID", "AKASHI_STR"), ("AKASHI_STR", "OSAKA_BAY"),
    ("HARIMA_MID", "KII_STR"), ("OSAKA_BAY", "KII_STR"),
    
    # 東京湾海上ルート
    ("URAGA_S", "URAGA_N"),
    ("URAGA_N", "TYO_MID"),
    ("TYO_MID", "YOKOHAMA_OFF"),
    ("TYO_MID", "KAWASAKI_OFF"),
    ("TYO_MID", "KISARAZU_OFF"),
    ("TYO_MID", "CHIBA_OFF"),
    ("YOKOHAMA_OFF", "KAWASAKI_OFF"),
    ("KAWASAKI_OFF", "HANEDA_OFF"),
    ("HANEDA_OFF", "TOKYO_ENT"),
    ("TOKYO_ENT", "TOKYO_INN"),
    ("HANEDA_OFF", "CHIBA_OFF"),

    # 海外
    ("VN_OFFSHORE", "VN_ENTRANCE"), ("VN_ENTRANCE", "VN_CMIT_S"), 
    ("VN_CMIT_S", "VN_CMIT_N"), ("VN_CMIT_N", "VN_THIVAI_UP"),
]

GRAPH = {node: [] for node in MARITIME_NODES}
for u, v in MARITIME_EDGES:
    p1, p2 = MARITIME_NODES[u], MARITIME_NODES[v]
    dist = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
    GRAPH[u].append((v, dist))
    GRAPH[v].append((u, dist))

def find_nearest_safe_node(lat, lon, max_dist_deg=0.45):
    """陸地ポリゴンを跨がずに直結できる最も近い海上ノードを探索"""
    nearest, min_dist = None, float("inf")
    for name, (n_lat, n_lon) in MARITIME_NODES.items():
        d = math.hypot(lat - n_lat, lon - n_lon)
        if d < min_dist and d <= max_dist_deg:
            connect_line = LineString([(lon, lat), (n_lon, n_lat)])
            if not connect_line.intersects(LAND_OBSTACLES):
                min_dist, nearest = d, name
    return nearest

def dijkstra_shortest_path(start_node, end_node):
    if start_node == end_node: return [MARITIME_NODES[start_node]]
    queue = [(0.0, start_node, [])]
    visited = {}
    while queue:
        cost, curr, path = heapq.heappop(queue)
        if curr in visited and visited[curr] <= cost: continue
        visited[curr] = cost
        new_path = path + [curr]
        if curr == end_node: return [MARITIME_NODES[n] for n in new_path]
        for neighbor, edge_cost in GRAPH.get(curr, []):
            if neighbor not in visited:
                heapq.heappush(queue, (cost + edge_cost, neighbor, new_path))
    return None

def resolve_graph_route(lat1, lon1, lat2, lon2):
    n1 = find_nearest_safe_node(lat1, lon1)
    n2 = find_nearest_safe_node(lat2, lon2)
    if n1 and n2 and n1 != n2:
        path_nodes = dijkstra_shortest_path(n1, n2)
        if path_nodes:
            route = [[lat1, lon1]]
            for p in path_nodes:
                if not (abs(route[-1][0] - p[0]) < 1e-4 and abs(route[-1][1] - p[1]) < 1e-4):
                    route.append(list(p))
            if not (abs(route[-1][0] - lat2) < 1e-4 and abs(route[-1][1] - lon2) < 1e-4):
                route.append([lat2, lon2])
            return route
    return None

# ==============================================================================
# 3. 図形処理による幾何航路計算（陸地交差の完全防止）
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    direct_line = LineString([(lon1, lat1), (lon2, lat2)])
    
    # ★図形判定: 陸地ポリゴンと一切交差せず、かつ近距離であれば実測直線を許可
    if not direct_line.intersects(LAND_OBSTACLES):
        dist_deg = math.hypot(lat1 - lat2, lon1 - lon2)
        if dist_deg < 0.20:
            return [[round(lat1, 4), round(lon1, 4)], [round(lat2, 4), round(lon2, 4)]]

    # 陸地と交差する場合、または長距離の場合は海上グラフで迂回
    graph_route = resolve_graph_route(lat1, lon1, lat2, lon2)
    if graph_route and len(graph_route) >= 2:
        return graph_route

    # 外洋航路計算
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 4), round(pt[0], 4)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2:
                return coords
    except Exception:
        pass

    return [[round(lat1, 4), round(lon1, 4)], [round(lat2, 4), round(lon2, 4)]]

def unwrap_coordinates(coords):
    if not coords or len(coords) < 2: return coords
    unwrapped = [coords[0]]
    prev_lon = coords[0][1]
    offset = 0.0
    for i in range(1, len(coords)):
        lat, lon = coords[i][0], coords[i][1]
        raw_lon = lon + offset
        diff = raw_lon - prev_lon
        if diff > 180.0:
            offset -= 360.0
            raw_lon -= 360.0
        elif diff < -180.0:
            offset += 360.0
            raw_lon += 360.0
        unwrapped.append([lat, round(raw_lon, 4)])
        prev_lon = raw_lon
    return unwrapped

def chaikin_smooth(points, iterations=3):
    if not points or len(points) < 3:
        if len(points) == 2:
            p0, p1 = points[0], points[1]
            return [p0, [round((p0[0]+p1[0])/2, 4), round((p0[1]+p1[1])/2, 4)], p1]
        return points
    
    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0 = current[i]
            p1 = current[i + 1]
            next_pts.append([round(0.75*p0[0] + 0.25*p1[0], 4), round(0.75*p0[1] + 0.25*p1[1], 4)])
            next_pts.append([round(0.25*p0[0] + 0.75*p1[0], 4), round(0.25*p0[1] + 0.75*p1[1], 4)])
        next_pts.append(current[-1])
        current = next_pts
    return current

def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    while delta_lambda > math.pi: delta_lambda -= 2 * math.pi
    while delta_lambda < -math.pi: delta_lambda += 2 * math.pi
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    theta = math.atan2(y, x)
    return round((math.degrees(theta) + 360.0) % 360.0, 1)

# ==============================================================================
# 4. 基礎データ取得
# ==============================================================================
def fetch_base_data_from_gas(session):
    print("Fetching target MMSIs and historical tracks from GAS...")
    mmsi_url = f"{GAS_URL}?api=mmsi"
    tracks_url = f"{GAS_URL}?api=ship_tracks"
    latest_url = f"{GAS_URL}?api=latest"

    target_mmsis = set()
    res_mmsi = session.get(mmsi_url, timeout=30)
    if res_mmsi.status_code == 200:
        target_mmsis = set(str(m) for m in res_mmsi.json())
        print(f"Loaded {len(target_mmsis)} target MMSIs.")

    existing_tracks = {}
    res_tracks = session.get(tracks_url, timeout=60)
    if res_tracks.status_code == 200:
        for item in res_tracks.json():
            mmsi = str(item.get("mmsi"))
            existing_tracks[mmsi] = {
                "name": item.get("name", "Unknown"),
                "cat": item.get("cat", "日本支配船"),
                "points": item.get("points", [])
            }

    base_latest = {}
    res_latest = session.get(latest_url, timeout=60)
    if res_latest.status_code == 200:
        base_latest = res_latest.json()

    return target_mmsis, existing_tracks, base_latest

# ==============================================================================
# 5. AIS受信 (180秒)
# ==============================================================================
async def collect_ais_data(target_mmsis, duration_sec=180):
    uri = "wss://stream.aisstream.io/v0/stream"
    subscribe_message = {
        "APIKey": API_KEY,
        "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"]
    }

    ships_data = {}
    print(f"Connecting to AISStream.io. Collecting for {duration_sec} seconds...")

    try:
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            print("WebSocket connected. Stream active...")
            start_time = asyncio.get_event_loop().time()

            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= duration_sec:
                    break

                try:
                    raw_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    msg = json.loads(raw_msg)
                    msg_type = msg.get("MessageType")
                    metadata = msg.get("MetaData", {})
                    mmsi = str(metadata.get("MMSI", ""))

                    if mmsi not in target_mmsis:
                        continue

                    if mmsi not in ships_data:
                        ships_data[mmsi] = {
                            "mmsi": mmsi,
                            "name": metadata.get("ShipName", "").strip(),
                            "lat": None,
                            "lng": None,
                            "speed": 0.0,
                            "timestamp": metadata.get("time_utc", ""),
                            "destination": "-",
                            "eta": "-"
                        }

                    if msg_type == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                        ships_data[mmsi]["lat"] = pos.get("Latitude")
                        ships_data[mmsi]["lng"] = pos.get("Longitude")
                        ships_data[mmsi]["speed"] = pos.get("Sog", 0.0)
                        if metadata.get("time_utc"):
                            ships_data[mmsi]["timestamp"] = metadata.get("time_utc")

                    elif msg_type == "ShipStaticData":
                        static = msg.get("Message", {}).get("ShipStaticData", {})
                        dest = static.get("Destination", "").strip()
                        if dest:
                            ships_data[mmsi]["destination"] = dest
                        eta_dict = static.get("Eta", {})
                        if eta_dict:
                            mo = eta_dict.get("Month", 0)
                            day = eta_dict.get("Day", 0)
                            hr = eta_dict.get("Hour", 0)
                            mn = eta_dict.get("Minute", 0)
                            if 1 <= mo <= 12 and 1 <= day <= 31 and hr < 24 and mn < 60:
                                current_year = datetime.datetime.utcnow().year
                                ships_data[mmsi]["eta"] = f"{current_year}/{mo:02d}/{day:02d} {hr:02d}:{mn:02d} UTC"

                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue

    except Exception as e:
        print(f"WebSocket error: {e}")

    valid_ships = [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]
    return valid_ships

# ==============================================================================
# 6. 完全同期幾何計算（先端アンカー結合）
# ==============================================================================
def process_and_synchronize_tracks(new_ships, existing_tracks, base_latest):
    print("Synchronizing received AIS points...")
    
    latest_list = base_latest.get("latest", [])
    latest_map = {str(s["mmsi"]): s for s in latest_list}

    for s in new_ships:
        mmsi = str(s["mmsi"])
        if mmsi in latest_map:
            latest_map[mmsi]["lat"] = s["lat"]
            latest_map[mmsi]["lng"] = s["lng"]
            latest_map[mmsi]["speed"] = s["speed"]
            if s["destination"] != "-": latest_map[mmsi]["destination"] = s["destination"]
            if s["eta"] != "-": latest_map[mmsi]["eta"] = s["eta"]
        else:
            latest_map[mmsi] = {
                "mmsi": mmsi,
                "name": s["name"] or mmsi,
                "category": "日本支配船",
                "lat": s["lat"],
                "lng": s["lng"],
                "speed": s["speed"],
                "destination": s["destination"],
                "eta": s["eta"],
                "etaPassed": False,
                "owner": "-",
                "type": "-",
                "survey": "-"
            }

    synchronized_latest_data = {
        "masterCounts": base_latest.get("masterCounts", {"total": len(latest_map), "flag": 0, "ctrl": len(latest_map)}),
        "latest": list(latest_map.values())
    }

    processed_tracks = []
    for mmsi, l_ship in latest_map.items():
        lat = l_ship.get("lat")
        lng = l_ship.get("lng")
        if lat is None or lng is None: continue

        pts = []
        if mmsi in existing_tracks:
            pts = existing_tracks[mmsi]["points"][:]
        
        if not pts or (pts[-1][0] != lat or pts[-1][1] != lng):
            pts.append([lat, lng])

        cleaned_pts = []
        for i, p in enumerate(pts):
            p_lat, p_lon = p[0], p[1]
            if not (-90 <= p_lat <= 90 and -180 <= p_lon <= 180): continue
            if not cleaned_pts:
                cleaned_pts.append([p_lat, p_lon])
            else:
                is_last_point = (i == len(pts) - 1)
                if is_last_point or math.hypot(p_lat - cleaned_pts[-1][0], p_lon - cleaned_pts[-1][1]) > 0.001:
                    cleaned_pts.append([p_lat, p_lon])

        if len(cleaned_pts) < 2: continue

        full_track = []
        for i in range(len(cleaned_pts) - 1):
            seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
            full_track.extend(seg if not full_track else seg[1:])

        unwrapped_track = unwrap_coordinates(full_track)
        curved_track = chaikin_smooth(unwrapped_track, iterations=3)

        # 先端アンカー固定
        curved_track[-1] = [lat, lng]

        total_pts = len(curved_track)
        num_segments = 14
        step = max(1, (total_pts - 1) // num_segments)
        fade_segments = []

        for i in range(0, total_pts - 1, step):
            end_index = min(i + step + 1, total_pts)
            sub_coords = curved_track[i:end_index]
            if len(sub_coords) < 2: continue
            
            t_val = end_index / total_pts
            weight = round(1.0 + (2.8 * (t_val ** 1.2)), 2)
            opacity = round(0.06 + (0.89 * (t_val ** 1.5)), 3)
            
            fade_segments.append({
                "coords": sub_coords,
                "weight": weight,
                "opacity": opacity
            })

        heading = 0.0
        if len(curved_track) >= 2:
            last_p = curved_track[-1]
            for prev_p in reversed(curved_track[:-1]):
                if math.hypot(last_p[0] - prev_p[0], last_p[1] - prev_p[1]) > 0.0005:
                    heading = calculate_bearing(prev_p[0], prev_p[1], last_p[0], last_p[1])
                    break

        processed_tracks.append({
            "mmsi": mmsi,
            "name": l_ship.get("name", "Unknown"),
            "cat": l_ship.get("category", "日本支配船"),
            "coords": curved_track,
            "segments": fade_segments,
            "heading": heading
        })

    return synchronized_latest_data, processed_tracks

# ==============================================================================
# 7. カバレッジ生成
# ==============================================================================
def generate_dynamic_coverage(session):
    print("Generating dynamic AIS coverage...")
    url = f"{GAS_URL}?api=coverage_points"
    try:
        res = session.get(url, allow_redirects=True, timeout=60)
        if res.status_code != 200: return
        points_data = res.json()
        if not points_data: return

        buffers = [Point(pt[1], pt[0]).buffer(0.85, resolution=8) for pt in points_data]
        merged = unary_union(buffers)
        feature = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"description": "地上AIS受信実績圏"},
                "geometry": mapping(merged)
            }]
        }
        with open("data/coverage_2026.json", "w", encoding="utf-8") as f:
            json.dump(feature, f, ensure_ascii=False, separators=(',', ':'))
        print("data/coverage_2026.json generated.")
    except Exception as e:
        print("Error in dynamic coverage:", e)

# ==============================================================================
# 8. メイン実行フロー
# ==============================================================================
def main():
    os.makedirs("data", exist_ok=True)
    session = requests.Session()

    target_mmsis, existing_tracks, base_latest = fetch_base_data_from_gas(session)
    if not target_mmsis:
        print("No target MMSIs loaded. Exiting.")
        sys.exit(0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    new_ships = loop.run_until_complete(collect_ais_data(target_mmsis, duration_sec=180))

    synchronized_latest, processed_tracks = process_and_synchronize_tracks(
        new_ships, existing_tracks, base_latest
    )

    with open("data/latest_2026.json", "w", encoding="utf-8") as f:
        json.dump(synchronized_latest, f, ensure_ascii=False, separators=(',', ':'))
    with open("data/routes_2026.json", "w", encoding="utf-8") as f:
        json.dump(processed_tracks, f, ensure_ascii=False, separators=(',', ':'))
    print(f"data/routes_2026.json cached ({len(processed_tracks)} tracks).")

    generate_dynamic_coverage(session)

    if new_ships:
        print(f"Posting {len(new_ships)} ships to GAS...")
        try:
            res = requests.post(GAS_URL, json={"ships": new_ships}, timeout=60)
            print(f"GAS response: {res.status_code}")
        except Exception as e:
            print(f"Error posting to GAS: {e}")

if __name__ == "__main__":
    main()
