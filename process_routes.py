import os
import sys
import json
import math
import heapq
import requests
import searoute as sr

# shapely の自動インポート（未インストールの場合は自動セットアップ）
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
# 1. 狭水路航路データベース（ダイクストラ法）
# ==============================================================================
MARITIME_NODES = {
    "IYO_WEST":     (33.650, 132.200),
    "IYO_MID":      (33.800, 132.500),
    "AKI_NADA":     (34.120, 132.700),
    "SAI_WEST":     (34.050, 132.800),
    "SAI_MID":      (34.090, 132.880),
    "NOMA_OFF":     (34.135, 132.930),
    "OGE_SETO":     (34.168, 132.970),
    "KURUSHIMA_W":  (34.138, 133.002),
    "KURUSHIMA_BRG":(34.118, 133.015),
    "KURUSHIMA_E":  (34.092, 133.045),
    "HUCHI_W":      (34.110, 133.100),
    "HUCHI_MID":    (34.200, 133.350),
    "BISAN_W":      (34.330, 133.650),
    "BISAN_MID":    (34.380, 133.850),
    "BISAN_E":      (34.420, 134.150),
    "HARIMA_MID":   (34.500, 134.600),
    "AKASHI_STR":   (34.610, 135.010),
    "OSAKA_BAY":    (34.550, 135.250),
    "KII_STR":      (34.100, 134.900),
    "TYO_ENT":      (34.950, 139.750),
    "URAGA_CH":     (35.220, 139.730),
    "TYO_MID":      (35.450, 139.850),
    "TYO_INN_W":    (35.580, 139.800),
    "TYO_INN_E":    (35.580, 139.980),
    "VN_OFFSHORE":  (10.250, 107.030),
    "VN_ENTRANCE":  (10.380, 107.030),
    "VN_CMIT_S":    (10.490, 107.020),
    "VN_CMIT_N":    (10.550, 107.015),
    "VN_THIVAI_UP": (10.600, 107.005),
}

MARITIME_EDGES = [
    ("IYO_WEST", "IYO_MID"), ("IYO_MID", "SAI_WEST"), ("IYO_MID", "AKI_NADA"),
    ("AKI_NADA", "SAI_MID"), ("SAI_WEST", "SAI_MID"), ("SAI_MID", "NOMA_OFF"),
    ("NOMA_OFF", "OGE_SETO"), ("OGE_SETO", "KURUSHIMA_W"), ("KURUSHIMA_W", "KURUSHIMA_BRG"),
    ("KURUSHIMA_BRG", "KURUSHIMA_E"), ("KURUSHIMA_E", "HUCHI_W"), ("HUCHI_W", "HUCHI_MID"),
    ("HUCHI_MID", "BISAN_W"), ("BISAN_W", "BISAN_MID"), ("BISAN_MID", "BISAN_E"),
    ("BISAN_E", "HARIMA_MID"), ("HARIMA_MID", "AKASHI_STR"), ("AKASHI_STR", "OSAKA_BAY"),
    ("HARIMA_MID", "KII_STR"), ("OSAKA_BAY", "KII_STR"),
    ("TYO_ENT", "URAGA_CH"), ("URAGA_CH", "TYO_MID"), ("TYO_MID", "TYO_INN_W"), ("TYO_MID", "TYO_INN_E"),
    ("VN_OFFSHORE", "VN_ENTRANCE"), ("VN_ENTRANCE", "VN_CMIT_S"), ("VN_CMIT_S", "VN_CMIT_N"), ("VN_CMIT_N", "VN_THIVAI_UP"),
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
        if curr == end_node:
            return [MARITIME_NODES[n] for n in new_path]
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
    if graph_route and len(graph_route) >= 2: return graph_route
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2: return coords
    except Exception: pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

def chaikin_smooth(points, iterations=4):
    if not points or len(points) < 3:
        if len(points) == 2:
            p0, p1 = points[0], points[1]
            points = [p0, [round((p0[0]+p1[0])/2, 5), round((p0[1]+p1[1])/2, 5)], p1]
        else: return points
    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0, p1 = current[i], current[i + 1]
            next_pts.append([round(0.75*p0[0] + 0.25*p1[0], 5), round(0.75*p0[1] + 0.25*p1[1], 5)])
            next_pts.append([round(0.25*p0[0] + 0.75*p1[0], 5), round(0.25*p0[1] + 0.75*p1[1], 5)])
        next_pts.append(current[-1])
        current = next_pts
    return current

# ==============================================================================
# 2. 実績座標からの動的地上AISカバレッジ（50海里バッファ結合）生成
# ==============================================================================
def generate_dynamic_coverage(session):
    print("Generating dynamic AIS coverage from historical points...")
    url = f"{GAS_URL}?api=coverage_points"
    try:
        res = session.get(url, allow_redirects=True, timeout=60)
        if res.status_code != 200:
            print("Failed to fetch coverage points:", res.status_code)
            return

        points_data = res.json()
        if not points_data or len(points_data) == 0:
            print("No coverage points available.")
            return

        print(f"Loaded {len(points_data)} unique historical detection points.")

        # 各検知点から半径約50海里（約0.83度）の円形バッファを作成
        # 解像度8（32角形の円近似）で高速結合
        buffers = []
        for pt in points_data:
            lat, lon = pt[0], pt[1]
            p = Point(lon, lat)
            buffers.append(p.buffer(0.85, resolution=8))

        # 全バッファをマージ結合して1つの外郭ポリゴン群に合成
        merged = unary_union(buffers)
        geojson_geom = mapping(merged)

        feature = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {
                    "description": "地上AIS受信実績圏（検知実績から半径約50海里を自動展開）"
                },
                "geometry": geojson_geom
            }]
        }

        with open("data/coverage_2026.json", "w", encoding="utf-8") as f:
            json.dump(feature, f, ensure_ascii=False, separators=(',', ':'))

        print("Successfully generated data/coverage_2026.json")

    except Exception as e:
        print("Error in dynamic coverage generation:", e)

# ==============================================================================
# 3. メイン処理
# ==============================================================================
def main():
    os.makedirs("data", exist_ok=True)
    session = requests.Session()

    # A. 航路帯の計算と出力
    target_url = f"{GAS_URL}?api=routes"
    res = session.get(target_url, allow_redirects=True, timeout=60)
    if res.status_code == 200:
        raw_routes = res.json()
        valid_routes = []
        for row in raw_routes:
            try:
                s_lat, s_lng = row.get("Start_Lat"), row.get("Start_Lng")
                e_lat, e_lng = row.get("End_Lat"), row.get("End_Lng")
                if None in (s_lat, s_lng, e_lat, e_lng): continue
                lat1, lon1, lat2, lon2 = float(s_lat), float(s_lng), float(e_lat), float(e_lng)
                if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180): continue
                valid_routes.append({
                    "cat": str(row.get("Category") or "日本支配船"),
                    "count": int(row.get("Count") or 1),
                    "p1": [lat1, lon1], "p2": [lat2, lon2]
                })
            except: continue

        total_items = len(valid_routes)
        processed_routes = []
        for idx, item in enumerate(valid_routes):
            raw_coords = calculate_sea_route(item["p1"][0], item["p1"][1], item["p2"][0], item["p2"][1])
            smooth_coords = chaikin_smooth(raw_coords, iterations=4)
            time_ratio = (idx + 1) / total_items if total_items > 1 else 1.0
            glow_opacity = round(0.15 + 0.75 * math.pow(time_ratio, 1.3), 3)
            core_opacity = round(0.25 + 0.75 * math.pow(time_ratio, 1.1), 3)
            base_weight = min(16.0, max(3.2, 1.8 + math.log1p(item["count"]) * 2.3))
            seg_weight = round(base_weight * (0.7 + 0.3 * time_ratio), 2)

            processed_routes.append({
                "cat": item["cat"], "count": item["count"], "coords": smooth_coords,
                "glow_op": glow_opacity, "core_op": core_opacity, "weight": seg_weight,
                "status": "直近の航行実績" if time_ratio > 0.8 else "過去の航行実績"
            })

        with open("data/routes_2026.json", "w", encoding="utf-8") as f:
            json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))
        print(f"Successfully generated data/routes_2026.json ({len(processed_routes)} routes)")

    # B. 実績連動型動的カバレッジの生成
    generate_dynamic_coverage(session)

if __name__ == "__main__":
    main()
