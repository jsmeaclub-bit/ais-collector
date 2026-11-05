import os
import json
import math
import heapq
import requests
import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

# ==============================================================================
# 1. 海上航路データベース（海の道路ネットワーク定義）
# ==============================================================================
# 主要水路・海峡・内海の中心線ノード（緯度, 経度）
MARITIME_NODES = {
    # --- 瀬戸内海（伊予灘・斎灘・来島・燧灘・備讃・播磨・明石） ---
    "IYO_WEST":     (33.650, 132.200),
    "IYO_MID":      (33.800, 132.500),
    "AKI_NADA":     (34.120, 132.700),
    "SAI_WEST":     (34.050, 132.800),
    "SAI_MID":      (34.090, 132.880),
    "NOMA_OFF":     (34.135, 132.930), # 波方半島西沖
    "OGE_SETO":     (34.168, 132.970), # 大下瀬戸中央（波方半島の北側海面）
    "KURUSHIMA_W":  (34.138, 133.002), # 来島西口アプローチ
    "KURUSHIMA_BRG":(34.118, 133.015), # 来島海峡大橋下（中水道水面中央）
    "KURUSHIMA_E":  (34.092, 133.045), # 来島東口
    "HUCHI_W":      (34.110, 133.100), # 燧灘西部
    "HUCHI_MID":    (34.200, 133.350), # 燧灘中央
    "BISAN_W":      (34.330, 133.650), # 備讃瀬戸西
    "BISAN_MID":    (34.380, 133.850), # 備讃瀬戸中央（瀬戸大橋下）
    "BISAN_E":      (34.420, 134.150), # 備讃瀬戸東
    "HARIMA_MID":   (34.500, 134.600), # 播磨灘中央
    "AKASHI_STR":   (34.610, 135.010), # 明石海峡中央
    "OSAKA_BAY":    (34.550, 135.250), # 大阪湾中央
    "KII_STR":      (34.100, 134.900), # 紀伊水道
    
    # --- 東京湾・浦賀水道 ---
    "TYO_ENT":      (34.950, 139.750), # 浦賀水道南口
    "URAGA_CH":     (35.220, 139.730), # 浦賀水道航路中央
    "TYO_MID":      (35.450, 139.850), # 東京湾中央
    "TYO_INN_W":    (35.580, 139.800), # 東京・川崎沖
    "TYO_INN_E":    (35.580, 139.980), # 千葉沖

    # --- ベトナム南部（ブンタウ沖 〜 チーバイ川・内水路港） ---
    "VN_OFFSHORE":  (10.250, 107.030), # ブンタウ沖合パイロットステーション
    "VN_ENTRANCE":  (10.380, 107.030), # チーバイ川河口進入点
    "VN_CMIT_S":    (10.490, 107.020), # カイメップ下流
    "VN_CMIT_N":    (10.550, 107.015), # カイメップ主要岸壁水面
    "VN_THIVAI_UP": (10.600, 107.005), # フーミー工業港水面
}

# ノード間を結ぶ安全な航路エッジ（双方向接続）
MARITIME_EDGES = [
    # 瀬戸内海幹線
    ("IYO_WEST", "IYO_MID"),
    ("IYO_MID", "SAI_WEST"),
    ("IYO_MID", "AKI_NADA"),
    ("AKI_NADA", "SAI_MID"),
    ("SAI_WEST", "SAI_MID"),
    ("SAI_MID", "NOMA_OFF"),
    ("NOMA_OFF", "OGE_SETO"),
    ("OGE_SETO", "KURUSHIMA_W"),
    ("KURUSHIMA_W", "KURUSHIMA_BRG"),
    ("KURUSHIMA_BRG", "KURUSHIMA_E"),
    ("KURUSHIMA_E", "HUCHI_W"),
    ("HUCHI_W", "HUCHI_MID"),
    ("HUCHI_MID", "BISAN_W"),
    ("BISAN_W", "BISAN_MID"),
    ("BISAN_MID", "BISAN_E"),
    ("BISAN_E", "HARIMA_MID"),
    ("HARIMA_MID", "AKASHI_STR"),
    ("AKASHI_STR", "OSAKA_BAY"),
    ("HARIMA_MID", "KII_STR"),
    ("OSAKA_BAY", "KII_STR"),

    # 東京湾幹線
    ("TYO_ENT", "URAGA_CH"),
    ("URAGA_CH", "TYO_MID"),
    ("TYO_MID", "TYO_INN_W"),
    ("TYO_MID", "TYO_INN_E"),

    # ベトナム内水路
    ("VN_OFFSHORE", "VN_ENTRANCE"),
    ("VN_ENTRANCE", "VN_CMIT_S"),
    ("VN_CMIT_S", "VN_CMIT_N"),
    ("VN_CMIT_N", "VN_THIVAI_UP"),
]

# グラフ構造の初期化
GRAPH = {node: [] for node in MARITIME_NODES}
for u, v in MARITIME_EDGES:
    p1, p2 = MARITIME_NODES[u], MARITIME_NODES[v]
    # ユークリッド距離をコストとする
    dist = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
    GRAPH[u].append((v, dist))
    GRAPH[v].append((u, dist))

# ==============================================================================
# 2. グラフ探索・最短経路計算（ダイクストラ法）
# ==============================================================================
def find_nearest_node(lat, lon, max_dist_deg=0.45):
    """指定座標から最も近い航路ノードを探す（一定距離以上離れている場合はNone）"""
    nearest = None
    min_dist = float("inf")
    for name, (n_lat, n_lon) in MARITIME_NODES.items():
        d = math.hypot(lat - n_lat, lon - n_lon)
        if d < min_dist:
            min_dist = d
            nearest = name
    return nearest if min_dist <= max_dist_deg else None

def dijkstra_shortest_path(start_node, end_node):
    """ダイクストラ法により水路ネットワーク上の最短ノード列を算出"""
    if start_node == end_node:
        return [MARITIME_NODES[start_node]]

    queue = [(0.0, start_node, [])]
    visited = {}

    while queue:
        cost, curr, path = heapq.heappop(queue)
        if curr in visited and visited[curr] <= cost:
            continue
        visited[curr] = cost
        new_path = path + [curr]

        if curr == end_node:
            return [MARITIME_NODES[n] for n in new_path]

        for neighbor, edge_cost in GRAPH.get(curr, []):
            if neighbor not in visited:
                heapq.heappush(queue, (cost + edge_cost, neighbor, new_path))
    return None

def resolve_graph_route(lat1, lon1, lat2, lon2):
    """始点・終点を航路ネットワークにスナップして完全水面経路を引く"""
    n1 = find_nearest_node(lat1, lon1)
    n2 = find_nearest_node(lat2, lon2)

    # 両端が航路ネットワークのカバー圏内にある場合
    if n1 and n2:
        path_nodes = dijkstra_shortest_path(n1, n2)
        if path_nodes:
            # 始点 -> スナップ航路網 -> 終点 をシームレスに結合
            route = [[lat1, lon1]]
            for p in path_nodes:
                if not (abs(route[-1][0] - p[0]) < 1e-4 and abs(route[-1][1] - p[1]) < 1e-4):
                    route.append(list(p))
            if not (abs(route[-1][0] - lat2) < 1e-4 and abs(route[-1][1] - lon2) < 1e-4):
                route.append([lat2, lon2])
            return route
    return None

# ==============================================================================
# 3. 外洋ライブラリ（searoute）とのハイブリッド航路解決
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    # ① 内蔵航路ネットワークを最優先で照合（海峡・半島・河川港を完全防御）
    graph_route = resolve_graph_route(lat1, lon1, lat2, lon2)
    if graph_route and len(graph_route) >= 2:
        return graph_route

    # ② 外洋エリアは searoute ライブラリによる陸地自動回避
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2:
                return coords
    except Exception:
        pass

    # ③ フォールバック
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

# ==============================================================================
# 4. 幾何平滑化（Chaikin Curve Smoothing）
# ==============================================================================
def chaikin_smooth(points, iterations=4):
    """カドを取り、本物の船の航跡のような滑らかな曲線（円弧）を生成"""
    if not points or len(points) < 3:
        if len(points) == 2:
            p0, p1 = points[0], points[1]
            points = [p0, [round((p0[0]+p1[0])/2, 5), round((p0[1]+p1[1])/2, 5)], p1]
        else:
            return points

    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0 = current[i]; p1 = current[i + 1]
            next_pts.append([round(0.75*p0[0] + 0.25*p1[0], 5), round(0.75*p0[1] + 0.25*p1[1], 5)])
            next_pts.append([round(0.25*p0[0] + 0.75*p1[0], 5), round(0.25*p0[1] + 0.75*p1[1], 5)])
        next_pts.append(current[-1])
        current = next_pts
    return current

# ==============================================================================
# 5. メイン処理（GASからデータ取得 -> 航路計算 -> JSON出力）
# ==============================================================================
def main():
    os.makedirs("data", exist_ok=True)
    target_url = f"{GAS_URL}?api=routes"
    print(f"Fetching routes from: {target_url}")

    session = requests.Session()
    res = session.get(target_url, allow_redirects=True, timeout=60)
    if res.status_code != 200:
        raise ValueError(f"HTTPエラー: status {res.status_code}")

    raw_routes = res.json()
    valid_routes = []
    for row in raw_routes:
        try:
            s_lat, s_lng = row.get("Start_Lat"), row.get("Start_Lng")
            e_lat, e_lng = row.get("End_Lat"), row.get("End_Lng")
            if None in (s_lat, s_lng, e_lat, e_lng):
                continue
            lat1, lon1, lat2, lon2 = float(s_lat), float(s_lng), float(e_lat), float(e_lng)
            if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180):
                continue
            valid_routes.append({
                "cat": str(row.get("Category") or "日本支配船"),
                "count": int(row.get("Count") or 1),
                "p1": [lat1, lon1],
                "p2": [lat2, lon2]
            })
        except Exception:
            continue

    total_items = len(valid_routes)
    processed_routes = []

    for idx, item in enumerate(valid_routes):
        # 航路ネットワーク照合 + 最短経路探索
        raw_coords = calculate_sea_route(item["p1"][0], item["p1"][1], item["p2"][0], item["p2"][1])
        
        # 曲線平滑化
        smooth_coords = chaikin_smooth(raw_coords, iterations=4)

        # 1年間の時系列グラデーション（古い航跡＝薄く、直近＝濃く太く）
        time_ratio = (idx + 1) / total_items if total_items > 1 else 1.0
        glow_opacity = round(0.15 + 0.75 * math.pow(time_ratio, 1.3), 3)
        core_opacity = round(0.25 + 0.75 * math.pow(time_ratio, 1.1), 3)

        base_weight = min(16.0, max(3.2, 1.8 + math.log1p(item["count"]) * 2.3))
        seg_weight = round(base_weight * (0.7 + 0.3 * time_ratio), 2)

        processed_routes.append({
            "cat": item["cat"],
            "count": item["count"],
            "coords": smooth_coords,
            "glow_op": glow_opacity,
            "core_op": core_opacity,
            "weight": seg_weight,
            "status": "直近の航行実績" if time_ratio > 0.8 else "過去の航行実績"
        })

    output_path = "data/routes_2026.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))

    print(f"前処理完了: {len(processed_routes)} 件の航路を正常に出力しました。")

if __name__ == "__main__":
    main()
