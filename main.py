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
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "shapely"])
    import shapely
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 沿岸狭水路ネットワーク定義
# ==============================================================================
MARITIME_NODES = {
    "IYO_WEST":     (33.650, 132.200), "IYO_MID":      (33.800, 132.500), "AKI_NADA":     (34.120, 132.700),
    "SAI_WEST":     (34.050, 132.800), "SAI_MID":      (34.090, 132.880), "NOMA_OFF":     (34.135, 132.930),
    "OGE_SETO":     (34.168, 132.970), "KURUSHIMA_W":  (34.138, 133.002), "KURUSHIMA_BRG":(34.118, 133.015),
    "KURUSHIMA_E":  (34.092, 133.045), "HUCHI_W":      (34.110, 133.100), "HUCHI_MID":    (34.200, 133.350),
    "BISAN_W":      (34.330, 133.650), "BISAN_MID":    (34.380, 133.850), "BISAN_E":      (34.420, 134.150),
    "HARIMA_MID":   (34.500, 134.600), "AKASHI_STR":   (34.610, 135.010), "OSAKA_BAY":    (34.550, 135.250),
    "KII_STR":      (34.100, 134.900), "TYO_ENT":      (34.950, 139.750), "URAGA_CH":     (35.220, 139.730),
    "TYO_MID":      (35.450, 139.850), "TYO_INN_W":    (35.580, 139.800), "TYO_INN_E":    (35.580, 139.980),
    "VN_OFFSHORE":  (10.250, 107.030), "VN_ENTRANCE":  (10.380, 107.030), "VN_CMIT_S":    (10.490, 107.020),
    "VN_CMIT_N":    (10.550, 107.015), "VN_THIVAI_UP": (10.600, 107.005),
}

MARITIME_EDGES = [
    ("IYO_WEST", "IYO_MID"), ("IYO_MID", "SAI_WEST"), ("IYO_MID", "AKI_NADA"), ("AKI_NADA", "SAI_MID"), 
    ("SAI_WEST", "SAI_MID"), ("SAI_MID", "NOMA_OFF"), ("NOMA_OFF", "OGE_SETO"), ("OGE_SETO", "KURUSHIMA_W"), 
    ("KURUSHIMA_W", "KURUSHIMA_BRG"), ("KURUSHIMA_BRG", "KURUSHIMA_E"), ("KURUSHIMA_E", "HUCHI_W"), 
    ("HUCHI_W", "HUCHI_MID"), ("HUCHI_MID", "BISAN_W"), ("BISAN_W", "BISAN_MID"), ("BISAN_MID", "BISAN_E"),
    ("BISAN_E", "HARIMA_MID"), ("HARIMA_MID", "AKASHI_STR"), ("AKASHI_STR", "OSAKA_BAY"),
    ("HARIMA_MID", "KII_STR"), ("OSAKA_BAY", "KII_STR"), ("TYO_ENT", "URAGA_CH"), ("URAGA_CH", "TYO_MID"), 
    ("TYO_MID", "TYO_INN_W"), ("TYO_MID", "TYO_INN_E"), ("VN_OFFSHORE", "VN_ENTRANCE"), 
    ("VN_ENTRANCE", "VN_CMIT_S"), ("VN_CMIT_S", "VN_CMIT_N"), ("VN_CMIT_N", "VN_THIVAI_UP"),
]

GRAPH = {node: [] for node in MARITIME_NODES}
for u, v in MARITIME_EDGES:
    p1, p2 = MARITIME_NODES[u], MARITIME_NODES[v]
    dist = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
    GRAPH[u].append((v, dist))
    GRAPH[v].append((u, dist))

def find_nearest_node(lat, lon, max_dist_deg=0.45):
    nearest, min_dist = None, float("inf")
    for name, (n_lat, n_lon) in MARITIME_NODES.items():
        d = math.hypot(lat - n_lat, lon - n_lon)
        if d < min_dist:
            min_dist, nearest = d, name
    return nearest if min_dist <= max_dist_deg else None

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
    n1, n2 = find_nearest_node(lat1, lon1), find_nearest_node(lat2, lon2)
    if n1 and n2:
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

def calculate_sea_route(lat1, lon1, lat2, lon2):
    graph_route = resolve_graph_route(lat1, lon1, lat2, lon2)
    if graph_route and len(graph_route) >= 2:
        return graph_route
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
    """角を丸めて滑らかな曲線にする"""
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
# ステップ 1: GASからマスタ情報・過去履歴を取得
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
# ステップ 2: AISStream.io からリアルタイム受信 (180秒)
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
        print(f"WebSocket connection error: {e}")

    valid_ships = [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]
    return valid_ships

# ==============================================================================
# ステップ 3: 受信データを最先端として結合し、軌跡・セグメントを完全事前計算
# ==============================================================================
def process_and_synchronize_tracks(new_ships, existing_tracks, base_latest):
    print("Synchronizing latest AIS hits directly into track endpoints...")
    
    # 1. 最新位置マップを更新（今回受信した船を最優先で反映）
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

    # 2. 過去航跡の末尾に、今回の最新座標を直接アペンド
    processed_tracks = []
    
    for mmsi, l_ship in latest_map.items():
        lat = l_ship.get("lat")
        lng = l_ship.get("lng")
        if lat is None or lng is None: continue

        pts = []
        if mmsi in existing_tracks:
            pts = existing_tracks[mmsi]["points"][:]
        
        # 今回の最新位置を航跡の末尾として追加
        if not pts or (pts[-1][0] != lat or pts[-1][1] != lng):
            pts.append([lat, lng])

        # 頂点の整理
        cleaned_pts = []
        for i, p in enumerate(pts):
            p_lat, p_lon = p[0], p[1]
            if not (-90 <= p_lat <= 90 and -180 <= p_lon <= 180): continue
            if not cleaned_pts:
                cleaned_pts.append([p_lat, p_lon])
            else:
                is_last_point = (i == len(pts) - 1)
                # 最後の点（現在地）は絶対に間引かず採用
                if is_last_point or math.hypot(p_lat - cleaned_pts[-1][0], p_lon - cleaned_pts[-1][1]) > 0.001:
                    cleaned_pts.append([p_lat, p_lon])

        if len(cleaned_pts) < 2: continue

        # 海洋航路計算
        full_track = []
        for i in range(len(cleaned_pts) - 1):
            seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
            full_track.extend(seg if not full_track else seg[1:])

        unwrapped_track = unwrap_coordinates(full_track)
        curved_track = chaikin_smooth(unwrapped_track, iterations=3)

        # ★最重要アンカー固定: Chaikin平滑化によって先端がズレないよう、最後の座標を最新位置に強制一致
        curved_track[-1] = [lat, lng]

        # 14段階フェードセグメントの事前計算（先端へ向かって太く・濃く）
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

        # 船首方位角（Heading）の事前計算
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
# ステップ 4: JSON出力 & GASへの保存
# ==============================================================================
def save_outputs(synchronized_latest, processed_tracks):
    os.makedirs("data", exist_ok=True)

    with open("data/latest_2026.json", "w", encoding="utf-8") as f:
        json.dump(synchronized_latest, f, ensure_ascii=False, separators=(',', ':'))
    print(f"data/latest_2026.json cached ({len(synchronized_latest['latest'])} ships).")

    with open("data/routes_2026.json", "w", encoding="utf-8") as f:
        json.dump(processed_tracks, f, ensure_ascii=False, separators=(',', ':'))
    print(f"data/routes_2026.json cached ({len(processed_tracks)} tracks).")

def post_to_gas(new_ships):
    if not new_ships: return
    print(f"Posting {len(new_ships)} freshly detected ships to GAS...")
    try:
        res = requests.post(GAS_URL, json={"ships": new_ships}, timeout=60)
        print(f"GAS response status: {res.status_code}")
    except Exception as e:
        print(f"Error posting to GAS: {e}")

# ==============================================================================
# メインエントリポイント
# ==============================================================================
def main():
    session = requests.Session()

    # 1. 基準データ取得
    target_mmsis, existing_tracks, base_latest = fetch_base_data_from_gas(session)
    if not target_mmsis:
        print("No target MMSIs loaded. Exiting.")
        sys.exit(0)

    # 2. 180秒間AISデータ収集
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    new_ships = loop.run_until_complete(collect_ais_data(target_mmsis, duration_sec=180))

    # 3. 取得データを直接先端として軌跡・セグメントを事前計算（完全同期）
    synchronized_latest, processed_tracks = process_and_synchronize_tracks(
        new_ships, existing_tracks, base_latest
    )

    # 4. 同期した静的JSONファイルを保存
    save_outputs(synchronized_latest, processed_tracks)

    # 5. GASのスプレッドシートへバックアップ保存
    post_to_gas(new_ships)
    print("All processes successfully completed!")

if __name__ == "__main__":
    main()
