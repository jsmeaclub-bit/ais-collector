import os
import sys
import json
import math
import heapq
import asyncio
import datetime
import websockets
import requests

try:
    import searoute as sr
    import numpy as np
    import cv2
    from scipy.interpolate import splprep, splev
    from shapely.geometry import Point, LineString, Polygon, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely", "scipy", "numpy", "opencv-python-headless"])
    import searoute as sr
    import numpy as np
    import cv2
    from scipy.interpolate import splprep, splev
    from shapely.geometry import Point, LineString, Polygon, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 東京湾 高解像度ポリゴン（運河を精密に開通させた障害物定義）
# ==============================================================================
# 本土と人工島を分けて定義し、間に「運河（水路）」を確保する
TOKYO_BAY_POLYGONS = [
    # 西側本土（横浜〜川崎〜東京の内陸）
    [(139.50, 35.20), (139.65, 35.20), (139.65, 35.40), (139.50, 35.40)],
    [(139.50, 35.40), (139.67, 35.40), (139.67, 35.46), (139.50, 35.46)],
    [(139.50, 35.46), (139.72, 35.46), (139.72, 35.52), (139.50, 35.52)],
    [(139.50, 35.52), (139.75, 35.52), (139.75, 35.70), (139.50, 35.70)],
    
    # 人工島群（本土との隙間が運河になる）
    [(139.68, 35.44), (139.70, 35.44), (139.70, 35.46), (139.68, 35.46)],       # 大黒ふ頭
    [(139.70, 35.47), (139.74, 35.47), (139.74, 35.495), (139.70, 35.495)],     # 扇島
    [(139.74, 35.495), (139.78, 35.495), (139.78, 35.51), (139.74, 35.51)],     # 東扇島
    [(139.77, 35.515), (139.81, 35.515), (139.81, 35.535), (139.77, 35.535)],   # 浮島
    [(139.74, 35.535), (139.84, 35.535), (139.84, 35.58), (139.74, 35.58)],     # 羽田空港
    
    # 東側本土（千葉・房総半島）
    [(139.95, 35.50), (140.20, 35.50), (140.20, 35.70), (139.95, 35.70)],
    [(139.90, 35.30), (140.20, 35.30), (140.20, 35.50), (139.90, 35.50)],
    [(139.80, 35.10), (140.20, 35.10), (140.20, 35.30), (139.80, 35.30)]
]

# Shapely交差判定用
LAND_OBSTACLES = unary_union([Polygon(poly) for poly in TOKYO_BAY_POLYGONS])

# ==============================================================================
# 2. 高解像度 画像処理ベース迂回エンジン (1000x1000px メッシュ)
# ==============================================================================
class HighResRasterRouter:
    def __init__(self):
        self.min_lat, self.max_lat = 35.20, 35.70
        self.min_lon, self.max_lon = 139.50, 140.20
        self.w, self.h = 1000, 1000 # 1px = 約40m（運河を完璧に捉える高解像度）
        self.mask = np.ones((self.h, self.w), dtype=np.uint8) * 255
        
        for obs in TOKYO_BAY_POLYGONS:
            pts = [self.latlon_to_pixel(lat, lon) for lon, lat in obs]
            cv2.fillPoly(self.mask, [np.array(pts, np.int32)], 0)

        # 陸地からの距離計算（水路の中央を強力に優先するコストマップ）
        dist = cv2.distanceTransform(self.mask, cv2.DIST_L2, 3)
        self.cost_map = np.where(self.mask > 128, 1.0 + 100.0 / (dist + 1.0), np.inf)

    def latlon_to_pixel(self, lat, lon):
        x = int((lon - self.min_lon) / (self.max_lon - self.min_lon) * (self.w - 1))
        y = int((self.max_lat - lat) / (self.max_lat - self.min_lat) * (self.h - 1))
        return max(0, min(self.w - 1, x)), max(0, min(self.h - 1, y))

    def pixel_to_latlon(self, x, y):
        lon = self.min_lon + (x / (self.w - 1)) * (self.max_lon - self.min_lon)
        lat = self.max_lat - (y / (self.h - 1)) * (self.max_lat - self.min_lat)
        return round(lat, 5), round(lon, 5)

    def _snap_to_sea(self, x, y, radius=30):
        """
        ★最重要処理: GPSが陸地(着岸中など)にある場合、半径1.2km以内の「最も近い海」に
        強制的に引きずり込み、陸スタートによるエラーを排除する。
        """
        if self.mask[y, x] > 128: return x, y
        best_x, best_y, min_d = x, y, float('inf')
        for dy in range(-radius, radius+1):
            for dx in range(-radius, radius+1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.w and 0 <= ny < self.h and self.mask[ny, nx] > 128:
                    d = dx*dx + dy*dy
                    if d < min_d: min_d, best_x, best_y = d, nx, ny
        return best_x, best_y

    def find_route(self, lat1, lon1, lat2, lon2):
        if not (self.min_lat <= lat1 <= self.max_lat and self.min_lon <= lon1 <= self.max_lon): return None
        if not (self.min_lat <= lat2 <= self.max_lat and self.min_lon <= lon2 <= self.max_lon): return None
        
        sx, sy = self._snap_to_sea(*self.latlon_to_pixel(lat1, lon1))
        gx, gy = self._snap_to_sea(*self.latlon_to_pixel(lat2, lon2))
        
        queue = [(0, sx, sy, [])]
        visited = {}
        
        while queue:
            cost, x, y, path = heapq.heappop(queue)
            if (x, y) in visited and visited[(x, y)] <= cost: continue
            visited[(x, y)] = cost
            new_path = path + [(x, y)]
            
            if abs(x - gx) <= 2 and abs(y - gy) <= 2:
                route = [self.pixel_to_latlon(px, py) for px, py in new_path[::5]]
                return [[lat1, lon1]] + route + [[lat2, lon2]]
                
            for dx, dy in [(-1,0), (1,0), (0,-1), (0,1), (-1,-1), (1,1), (-1,1), (1,-1)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.w and 0 <= ny < self.h:
                    step_cost = self.cost_map[ny, nx]
                    if not np.isinf(step_cost):
                        # 探索速度を上げるためヒューリスティック比重を高めに設定
                        heuristic = math.hypot(gx - nx, gy - ny) * 2.5
                        heapq.heappush(queue, (cost + step_cost + heuristic, nx, ny, new_path))
        return None

tokyo_router = HighResRasterRouter()

# ==============================================================================
# 3. 軌跡描画判定プロセス（スプライン vs 画像処理）
# ==============================================================================
def b_spline_core(unique_pts, s_val, num_points):
    pts_arr = np.array(unique_pts)
    x, y = pts_arr[:, 0], pts_arr[:, 1]
    k = min(3, len(unique_pts) - 1)
    tck, u = splprep([x, y], s=s_val, k=k)
    u_new = np.linspace(0, 1, num_points)
    x_new, y_new = splev(u_new, tck)
    x_new[0], y_new[0] = x[0], y[0]
    x_new[-1], y_new[-1] = x[-1], y[-1]
    return [[round(x_new[i], 5), round(y_new[i], 5)] for i in range(num_points)]

def chaikin_smooth(points, iterations=1):
    """運河用のマイルドな平滑化（陸にめり込まない）"""
    if not points or len(points) < 3: return points
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

def draw_and_check_route(points, num_points=70):
    """
    【スマート切り替えロジック】
    外洋ではBスプライン曲線を適用し、陸地(東京湾)をクロスした瞬間に
    Bスプラインを破棄して「画像処理ルート＋角を落とすだけのChaikin」へ移行する。
    """
    if len(points) < 3: return resample_path_linear(points, num_points)
    
    unique_pts = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - unique_pts[-1][0], p[1] - unique_pts[-1][1]) > 1e-4:
            unique_pts.append(p)
    if len(unique_pts) < 3: return resample_path_linear(unique_pts, num_points)

    try:
        # 1. まず自然なBスプライン曲線を描いてみる
        curve = b_spline_core(unique_pts, s_val=0.002, num_points=num_points)
        curve_line = LineString(curve)
        
        # 2. 陸地（ポリゴン）をクロスしていないかチェック
        if not curve_line.intersects(LAND_OBSTACLES):
            return curve # 陸を避けていれば曲線確定

        # 3. 陸地をクロス（食い込み）した場合、運河専用の高解像度A*ルーターを使用
        start, end = unique_pts[0], unique_pts[-1]
        safe_pixel_route = tokyo_router.find_route(start[0], start[1], end[0], end[1])
        
        if safe_pixel_route and len(safe_pixel_route) >= 3:
            # ★重要: 運河ルートにはBスプラインをかけず、マイルドなChaikinで角を少し丸めるだけに留める
            canal_route = chaikin_smooth(safe_pixel_route, iterations=1)
            return resample_path_linear(canal_route, num_points=num_points)
            
    except Exception:
        pass
        
    return resample_path_linear(unique_pts, num_points)

def resample_path_linear(points, num_points=70):
    if not points or len(points) < 2: return points
    dists = [0.0]
    for i in range(1, len(points)):
        dists.append(dists[-1] + math.hypot(points[i][0] - points[i-1][0], points[i][1] - points[i-1][1]))
    if dists[-1] <= 1e-6: return points
    resampled, step, target, idx = [points[0]], dists[-1] / (num_points - 1), dists[-1] / (num_points - 1), 1
    while len(resampled) < num_points - 1:
        while idx < len(dists) and dists[idx] < target: idx += 1
        if idx >= len(dists): break
        d0, d1, p0, p1 = dists[idx-1], dists[idx], points[idx-1], points[idx]
        if d1 > d0:
            ratio = (target - d0) / (d1 - d0)
            resampled.append([round(p0[0] + ratio * (p1[0] - p0[0]), 5), round(p0[1] + ratio * (p1[1] - p0[1]), 5)])
        target += step
    resampled.append(points[-1])
    return resampled

# ==============================================================================
# 4. ベース航路計算
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    if math.hypot(lat1 - lat2, lon1 - lon2) < 0.05:
        return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2: return coords
    except: pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

def unwrap_coordinates(coords):
    if not coords or len(coords) < 2: return coords
    unwrapped, offset = [coords[0]], 0.0
    for i in range(1, len(coords)):
        lat, lon = coords[i][0], coords[i][1]
        raw_lon = lon + offset
        diff = raw_lon - unwrapped[-1][1]
        if diff > 180.0: offset -= 360.0; raw_lon -= 360.0
        elif diff < -180.0: offset += 360.0; raw_lon += 360.0
        unwrapped.append([lat, round(raw_lon, 5)])
    return unwrapped

def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1, phi2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    while dl > math.pi: dl -= 2 * math.pi
    while dl < -math.pi: dl += 2 * math.pi
    y, x = math.sin(dl) * math.cos(phi2), math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 1)

# ==============================================================================
# 5. 実行フロー
# ==============================================================================
def fetch_base_data_from_gas(session):
    target_mmsis, existing_tracks, base_latest = set(), {}, {}
    try:
        if (res := session.get(f"{GAS_URL}?api=mmsi", timeout=30)).status_code == 200: target_mmsis = set(str(m) for m in res.json())
        if (res := session.get(f"{GAS_URL}?api=ship_tracks", timeout=60)).status_code == 200:
            for i in res.json(): existing_tracks[str(i.get("mmsi"))] = {"name": i.get("name", "Unknown"), "cat": i.get("cat", "日本支配船"), "points": i.get("points", [])}
        if (res := session.get(f"{GAS_URL}?api=latest", timeout=60)).status_code == 200: base_latest = res.json()
    except: pass
    return target_mmsis, existing_tracks, base_latest

async def collect_ais_data(target_mmsis, duration_sec=180):
    ships_data = {}
    try:
        async with websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
            await ws.send(json.dumps({"APIKey": API_KEY, "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}))
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < duration_sec:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    mmsi = str(msg.get("MetaData", {}).get("MMSI", ""))
                    if mmsi not in target_mmsis: continue
                    if mmsi not in ships_data: ships_data[mmsi] = {"mmsi": mmsi, "name": msg.get("MetaData", {}).get("ShipName", "").strip(), "lat": None, "lng": None, "speed": 0.0, "destination": "-", "eta": "-"}
                    if msg.get("MessageType") == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                        ships_data[mmsi]["lat"], ships_data[mmsi]["lng"], ships_data[mmsi]["speed"] = pos.get("Latitude"), pos.get("Longitude"), pos.get("Sog", 0.0)
                except: continue
    except: pass
    return [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]

def process_and_synchronize_tracks(new_ships, existing_tracks, base_latest):
    latest_map = {str(s["mmsi"]): s for s in base_latest.get("latest", [])}
    for s in new_ships:
        if (m := str(s["mmsi"])) in latest_map: latest_map[m].update({"lat": s["lat"], "lng": s["lng"], "speed": s["speed"]})
        else: latest_map[m] = {"mmsi": m, "name": s["name"] or m, "category": "日本支配船", "lat": s["lat"], "lng": s["lng"], "speed": s["speed"], "destination": s["destination"], "eta": s["eta"], "etaPassed": False, "owner": "-", "type": "-", "survey": "-"}

    processed_tracks = []
    for mmsi, l_ship in latest_map.items():
        if (lat := l_ship.get("lat")) is None or (lng := l_ship.get("lng")) is None: continue
        pts = existing_tracks[mmsi]["points"][:] if mmsi in existing_tracks else []
        if not pts or (pts[-1][0] != lat or pts[-1][1] != lng): pts.append([lat, lng])

        cleaned_pts = [p for i, p in enumerate(pts) if -90 <= p[0] <= 90 and -180 <= p[1] <= 180 and (i == 0 or i == len(pts)-1 or math.hypot(p[0] - pts[i-1][0], p[1] - pts[i-1][1]) > 0.005)]
        if len(cleaned_pts) < 2: continue

        full_track = []
        for i in range(len(cleaned_pts) - 1):
            full_track.extend(calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])[1:] if full_track else calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1]))

        # ★ 交差判定付き・動的フォールバック描画エンジン呼び出し
        final_track = draw_and_check_route(unwrap_coordinates(full_track), num_points=70)
        final_track[-1] = [lat, lng]

        fade_segments = [{"coords": final_track[i:min(i + max(1, (len(final_track) - 1) // 14) + 1, len(final_track))], "weight": round(1.0 + (2.8 * ((min(i + max(1, (len(final_track) - 1) // 14) + 1, len(final_track)) / len(final_track)) ** 1.2)), 2), "opacity": round(0.06 + (0.89 * ((min(i + max(1, (len(final_track) - 1) // 14) + 1, len(final_track)) / len(final_track)) ** 1.5)), 3)} for i in range(0, len(final_track) - 1, max(1, (len(final_track) - 1) // 14))]
        heading = calculate_bearing(final_track[-2][0], final_track[-2][1], final_track[-1][0], final_track[-1][1]) if len(final_track) >= 2 else 0.0
        processed_tracks.append({"mmsi": mmsi, "name": l_ship.get("name", "Unknown"), "cat": l_ship.get("category", "日本支配船"), "coords": final_track, "segments": fade_segments, "heading": heading})

    return {"masterCounts": base_latest.get("masterCounts", {"total": len(latest_map), "flag": 0, "ctrl": len(latest_map)}), "latest": list(latest_map.values())}, processed_tracks

def generate_dynamic_coverage(session):
    try:
        if (res := session.get(f"{GAS_URL}?api=coverage_points", timeout=60)).status_code == 200 and res.json():
            with open("data/coverage_2026.json", "w", encoding="utf-8") as f: json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"description": "地上AIS受信実績圏"}, "geometry": mapping(unary_union([Point(pt[1], pt[0]).buffer(0.85, resolution=8) for pt in res.json()]))}]}, f, ensure_ascii=False, separators=(',', ':'))
    except: pass

def main():
    os.makedirs("data", exist_ok=True)
    session = requests.Session()
    target_mmsis, existing_tracks, base_latest = fetch_base_data_from_gas(session)
    if not target_mmsis: sys.exit(0)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    new_ships = loop.run_until_complete(collect_ais_data(target_mmsis, duration_sec=180))
    synchronized_latest, processed_tracks = process_and_synchronize_tracks(new_ships, existing_tracks, base_latest)
    with open("data/latest_2026.json", "w", encoding="utf-8") as f: json.dump(synchronized_latest, f, ensure_ascii=False, separators=(',', ':'))
    with open("data/routes_2026.json", "w", encoding="utf-8") as f: json.dump(processed_tracks, f, ensure_ascii=False, separators=(',', ':'))
    generate_dynamic_coverage(session)
    if new_ships:
        try: requests.post(GAS_URL, json={"ships": new_ships}, timeout=60)
        except: pass

if __name__ == "__main__": main()
