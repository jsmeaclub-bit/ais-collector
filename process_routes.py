import os
import sys
import json
import math
import heapq
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
if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

# ==============================================================================
# 1. 沿岸狭水路ネットワーク
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

# ==============================================================================
# 2. 海洋航路計算と高精度スムージング（曲線生成）
# ==============================================================================
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
    """データ間引きを行わず、全ポイントを高品質な曲線へ変換"""
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
# 3. 動的カバレッジ生成 ＆ 最新船舶データキャッシュ生成
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

def cache_latest_summary(session):
    print("Caching latest ships summary...")
    url = f"{GAS_URL}?api=latest"
    try:
        res = session.get(url, allow_redirects=True, timeout=60)
        if res.status_code == 200:
            data = res.json()
            with open("data/latest_2026.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            print("data/latest_2026.json cached successfully.")
    except Exception as e:
        print("Note: latest cache skipped:", e)

# ==============================================================================
# 4. メイン処理（バックエンド側での高負荷タスク集中）
# ==============================================================================
def main():
    os.makedirs("data", exist_ok=True)
    session = requests.Session()

    tracks_url = f"{GAS_URL}?api=ship_tracks"
    print(f"Fetching ship tracks from: {tracks_url}")
    res = session.get(tracks_url, allow_redirects=True, timeout=90)
    
    processed_tracks = []
    if res.status_code == 200:
        ships_tracks = res.json()
        for ship in ships_tracks:
            mmsi = ship.get("mmsi")
            name = ship.get("name", "Unknown")
            cat = ship.get("cat", "日本支配船")
            raw_pts = ship.get("points", [])

            cleaned_pts = []
            for p in raw_pts:
                lat, lon = p[0], p[1]
                if not (-90 <= lat <= 90 and -180 <= lon <= 180): continue
                if not cleaned_pts:
                    cleaned_pts.append([lat, lon])
                else:
                    if math.hypot(lat - cleaned_pts[-1][0], lon - cleaned_pts[-1][1]) > 0.001:
                        cleaned_pts.append([lat, lon])

            if len(cleaned_pts) < 2: continue

            full_track = []
            for i in range(len(cleaned_pts) - 1):
                seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
                full_track.extend(seg if not full_track else seg[1:])

            unwrapped_track = unwrap_coordinates(full_track)
            
            # ★ 1. 直線間引きを撤廃し、高品質な曲線を生成
            curved_track = chaikin_smooth(unwrapped_track, iterations=3)

            if len(curved_track) < 2:
                continue

            # ★ 2. ブラウザの負担を消すため、透過度・線幅・セグメント分割をPythonで事前計算
            total_pts = len(curved_track)
            num_segments = 14
            step = max(1, (total_pts - 1) // num_segments)
            fade_segments = []

            for i in range(0, total_pts - 1, step):
                end_index = min(i + step + 1, total_pts)
                sub_coords = curved_track[i:end_index]
                if len(sub_coords) < 2:
                    continue
                
                t_val = end_index / total_pts
                weight = round(1.0 + (2.8 * (t_val ** 1.2)), 2)
                opacity = round(0.06 + (0.89 * (t_val ** 1.5)), 3)
                
                fade_segments.append({
                    "coords": sub_coords,
                    "weight": weight,
                    "opacity": opacity
                })

            # 進行方位角（Heading）の事前計算
            heading = 0.0
            if len(curved_track) >= 2:
                last_p = curved_track[-1]
                for prev_p in reversed(curved_track[:-1]):
                    if math.hypot(last_p[0] - prev_p[0], last_p[1] - prev_p[1]) > 0.0005:
                        heading = calculate_bearing(prev_p[0], prev_p[1], last_p[0], last_p[1])
                        break

            processed_tracks.append({
                "mmsi": mmsi,
                "name": name,
                "cat": cat,
                "coords": curved_track,        # 背景の薄い線用
                "segments": fade_segments,     # 描画用（計算済グラデーション配列）
                "heading": heading,
                "last_point": curved_track[-1]
            })

    with open("data/routes_2026.json", "w", encoding="utf-8") as f:
        json.dump(processed_tracks, f, ensure_ascii=False, separators=(',', ':'))
    print(f"routes_2026.json: {len(processed_tracks)} 隻のフル計算済みデータを出力しました。")

    generate_dynamic_coverage(session)
    cache_latest_summary(session)

if __name__ == "__main__":
    main()
