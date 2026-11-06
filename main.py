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
    import cv2
    import numpy as np
    import searoute as sr
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely", "opencv-python-headless", "numpy"])
    import cv2
    import numpy as np
    import searoute as sr
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 画像処理ベースの海路探索エンジン (A* + 距離変換コストマップ)
# ==============================================================================
class RasterSeaRouter:
    def __init__(self):
        # 東京湾のカバー範囲
        self.min_lat, self.max_lat = 35.15, 35.75
        self.min_lon, self.max_lon = 139.55, 140.15
        self.w, self.h = 600, 600  # 600x600ピクセルグリッド（約100m/pxの高解像度）
        
        # マスク画像自動生成（羽田・川崎・横浜の防波堤を黒塗り）
        self.mask = self._generate_fallback_mask()

        # 画像処理: 陸地(黒=0)からの距離を計算し、水路の中央ほど安全(コスト低)にする
        dist = cv2.distanceTransform(self.mask, cv2.DIST_L2, 3)
        self.cost_map = np.where(self.mask > 128, 1.0 + 20.0 / (dist + 1.0), np.inf)

    def _generate_fallback_mask(self):
        img = np.ones((self.h, self.w), dtype=np.uint8) * 255
        obstacles = [
            [(139.735, 35.520), (139.835, 35.520), (139.835, 35.575), (139.735, 35.575)], # 羽田
            [(139.710, 35.480), (139.790, 35.480), (139.790, 35.515), (139.710, 35.515)], # 川崎
            [(139.650, 35.410), (139.700, 35.410), (139.700, 35.460), (139.650, 35.460)], # 横浜
            [(139.810, 35.200), (140.100, 35.200), (140.100, 35.550), (139.810, 35.550)], # 房総
        ]
        for obs in obstacles:
            pts = [self.latlon_to_pixel(lat, lon) for lon, lat in obs]
            cv2.fillPoly(img, [np.array(pts, np.int32)], 0)
        return img

    def latlon_to_pixel(self, lat, lon):
        x = int((lon - self.min_lon) / (self.max_lon - self.min_lon) * (self.w - 1))
        y = int((self.max_lat - lat) / (self.max_lat - self.min_lat) * (self.h - 1))
        return max(0, min(self.w - 1, x)), max(0, min(self.h - 1, y))

    def pixel_to_latlon(self, x, y):
        lon = self.min_lon + (x / (self.w - 1)) * (self.max_lon - self.min_lon)
        lat = self.max_lat - (y / (self.h - 1)) * (self.max_lat - self.min_lat)
        return round(lat, 5), round(lon, 5)

    def _snap_to_sea(self, x, y, radius=15):
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
                        heuristic = math.hypot(gx - nx, gy - ny) * 1.5
                        move_cost = step_cost * (1.414 if dx != 0 and dy != 0 else 1.0)
                        heapq.heappush(queue, (cost + move_cost + heuristic, nx, ny, new_path))
        return None

tokyo_router = RasterSeaRouter()

# ==============================================================================
# 2. 航路計算（画像処理優先）と曲線化パイプライン
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    if math.hypot(lat1 - lat2, lon1 - lon2) < 0.02:
        return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

    # ★ 画像処理による陸地回避（東京湾内の場合）
    img_route = tokyo_router.find_route(lat1, lon1, lat2, lon2)
    if img_route and len(img_route) >= 2:
        return img_route

    # それ以外の世界海域は searoute を使用
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2: return coords
    except Exception:
        pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

def unwrap_coordinates(coords):
    if not coords or len(coords) < 2: return coords
    unwrapped = [coords[0]]
    prev_lon = coords[0][1]
    offset = 0.0
    for i in range(1, len(coords)):
        lat, lon = coords[i][0], coords[i][1]
        raw_lon = lon + offset
        diff = raw_lon - prev_lon
        if diff > 180.0: offset -= 360.0; raw_lon -= 360.0
        elif diff < -180.0: offset += 360.0; raw_lon += 360.0
        unwrapped.append([lat, round(raw_lon, 5)])
        prev_lon = raw_lon
    return unwrapped

def chaikin_smooth(points, iterations=3):
    """
    画像処理で見つけたピクセルルートを「美しい曲線」にする処理。
    テンション比率（0.85:0.15）により、陸にめり込むことなく角を丸める。
    """
    if not points or len(points) < 3: return points
    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0, p1 = current[i], current[i + 1]
            next_pts.append([round(0.85*p0[0] + 0.15*p1[0], 5), round(0.85*p0[1] + 0.15*p1[1], 5)])
            next_pts.append([round(0.15*p0[0] + 0.85*p1[0], 5), round(0.15*p0[1] + 0.85*p1[1], 5)])
        next_pts.append(current[-1])
        current = next_pts
    return current

def resample_path(points, num_points=70):
    if not points or len(points) < 2: return points
    dists = [0.0]
    for i in range(1, len(points)):
        d = math.hypot(points[i][0] - points[i-1][0], points[i][1] - points[i-1][1])
        dists.append(dists[-1] + d)
    total_dist = dists[-1]
    if total_dist <= 1e-6: return points
    
    resampled = [points[0]]
    step = total_dist / (num_points - 1)
    target = step
    idx = 1
    
    while len(resampled) < num_points - 1:
        while idx < len(dists) and dists[idx] < target: idx += 1
        if idx >= len(dists): break
        d0, d1 = dists[idx-1], dists[idx]
        p0, p1 = points[idx-1], points[idx]
        if d1 > d0:
            ratio = (target - d0) / (d1 - d0)
            resampled.append([round(p0[0] + ratio * (p1[0] - p0[0]), 5), round(p0[1] + ratio * (p1[1] - p0[1]), 5)])
        target += step
    resampled.append(points[-1])
    return resampled

def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    while dl > math.pi: dl -= 2 * math.pi
    while dl < -math.pi: dl += 2 * math.pi
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 1)

# ==============================================================================
# 3. 基礎データ取得と同期処理
# ==============================================================================
def fetch_base_data_from_gas(session):
    print("Fetching target MMSIs and historical tracks from GAS...")
    target_mmsis, existing_tracks, base_latest = set(), {}, {}
    try:
        res_mmsi = session.get(f"{GAS_URL}?api=mmsi", timeout=30)
        if res_mmsi.status_code == 200: target_mmsis = set(str(m) for m in res_mmsi.json())
        res_tracks = session.get(f"{GAS_URL}?api=ship_tracks", timeout=60)
        if res_tracks.status_code == 200:
            for item in res_tracks.json(): existing_tracks[str(item.get("mmsi"))] = {"name": item.get("name", "Unknown"), "cat": item.get("cat", "日本支配船"), "points": item.get("points", [])}
        res_latest = session.get(f"{GAS_URL}?api=latest", timeout=60)
        if res_latest.status_code == 200: base_latest = res_latest.json()
    except Exception as e: print(f"Error fetching GAS data: {e}")
    return target_mmsis, existing_tracks, base_latest

async def collect_ais_data(target_mmsis, duration_sec=180):
    uri = "wss://stream.aisstream.io/v0/stream"
    subscribe_message = {"APIKey": API_KEY, "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]], "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    ships_data = {}
    print(f"Connecting to AISStream.io. Collecting for {duration_sec} seconds...")
    try:
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < duration_sec:
                try:
                    msg = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
                    metadata = msg.get("MetaData", {})
                    mmsi = str(metadata.get("MMSI", ""))
                    if mmsi not in target_mmsis: continue
                    if mmsi not in ships_data: ships_data[mmsi] = {"mmsi": mmsi, "name": metadata.get("ShipName", "").strip(), "lat": None, "lng": None, "speed": 0.0, "destination": "-", "eta": "-"}
                    if msg.get("MessageType") == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                        ships_data[mmsi]["lat"], ships_data[mmsi]["lng"], ships_data[mmsi]["speed"] = pos.get("Latitude"), pos.get("Longitude"), pos.get("Sog", 0.0)
                    elif msg.get("MessageType") == "ShipStaticData":
                        static = msg.get("Message", {}).get("ShipStaticData", {})
                        dest = static.get("Destination", "").strip()
                        if dest: ships_data[mmsi]["destination"] = dest
                        eta = static.get("Eta", {})
                        if eta and 1 <= eta.get("Month", 0) <= 12 and 1 <= eta.get("Day", 0) <= 31:
                            ships_data[mmsi]["eta"] = f"{datetime.datetime.utcnow().year}/{eta['Month']:02d}/{eta['Day']:02d} {eta.get('Hour',0):02d}:{eta.get('Minute',0):02d} UTC"
                except: continue
    except Exception as e: print(f"WebSocket error: {e}")
    return [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]

def process_and_synchronize_tracks(new_ships, existing_tracks, base_latest):
    latest_map = {str(s["mmsi"]): s for s in base_latest.get("latest", [])}

    for s in new_ships:
        mmsi = str(s["mmsi"])
        if mmsi in latest_map:
            latest_map[mmsi].update({"lat": s["lat"], "lng": s["lng"], "speed": s["speed"]})
            if s["destination"] != "-": latest_map[mmsi]["destination"] = s["destination"]
            if s["eta"] != "-": latest_map[mmsi]["eta"] = s["eta"]
        else:
            latest_map[mmsi] = {"mmsi": mmsi, "name": s["name"] or mmsi, "category": "日本支配船", "lat": s["lat"], "lng": s["lng"], "speed": s["speed"], "destination": s["destination"], "eta": s["eta"], "etaPassed": False, "owner": "-", "type": "-", "survey": "-"}

    synchronized_latest_data = {
        "masterCounts": base_latest.get("masterCounts", {"total": len(latest_map), "flag": 0, "ctrl": len(latest_map)}),
        "latest": list(latest_map.values())
    }

    processed_tracks = []
    for mmsi, l_ship in latest_map.items():
        lat, lng = l_ship.get("lat"), l_ship.get("lng")
        if lat is None or lng is None: continue

        pts = existing_tracks[mmsi]["points"][:] if mmsi in existing_tracks else []
        if not pts or (pts[-1][0] != lat or pts[-1][1] != lng): pts.append([lat, lng])

        # 画像処理のために、間引き距離を少し細かめに設定
        cleaned_pts = []
        for i, p in enumerate(pts):
            p_lat, p_lon = p[0], p[1]
            if not (-90 <= p_lat <= 90 and -180 <= p_lon <= 180): continue
            if not cleaned_pts or i == len(pts) - 1 or math.hypot(p_lat - cleaned_pts[-1][0], p_lon - cleaned_pts[-1][1]) > 0.003:
                cleaned_pts.append([p_lat, p_lon])

        if len(cleaned_pts) < 2: continue

        full_track = []
        for i in range(len(cleaned_pts) - 1):
            seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
            full_track.extend(seg if not full_track else seg[1:])

        # ★ 画像処理の経路に対して、曲線をかける（めり込まないテンション比率）
        unwrapped = unwrap_coordinates(full_track)
        smoothed = chaikin_smooth(unwrapped, iterations=3)
        final_track = resample_path(smoothed, num_points=70)
        final_track[-1] = [lat, lng] # 先端アンカー固定

        total_pts = len(final_track)
        step = max(1, (total_pts - 1) // 14)
        fade_segments = []

        for i in range(0, total_pts - 1, step):
            end_idx = min(i + step + 1, total_pts)
            sub_coords = final_track[i:end_idx]
            if len(sub_coords) < 2: continue
            
            t_val = end_idx / total_pts
            fade_segments.append({
                "coords": sub_coords,
                "weight": round(1.0 + (2.8 * (t_val ** 1.2)), 2),
                "opacity": round(0.06 + (0.89 * (t_val ** 1.5)), 3)
            })

        heading = 0.0
        if len(final_track) >= 2:
            last_p = final_track[-1]
            for prev_p in reversed(final_track[:-1]):
                if math.hypot(last_p[0] - prev_p[0], last_p[1] - prev_p[1]) > 0.0005:
                    heading = calculate_bearing(prev_p[0], prev_p[1], last_p[0], last_p[1])
                    break

        processed_tracks.append({"mmsi": mmsi, "name": l_ship.get("name", "Unknown"), "cat": l_ship.get("category", "日本支配船"), "coords": final_track, "segments": fade_segments, "heading": heading})

    return synchronized_latest_data, processed_tracks

def generate_dynamic_coverage(session):
    print("Generating dynamic AIS coverage...")
    try:
        res = session.get(f"{GAS_URL}?api=coverage_points", allow_redirects=True, timeout=60)
        points_data = res.json() if res.status_code == 200 else []
        if not points_data: return
        buffers = [Point(pt[1], pt[0]).buffer(0.85, resolution=8) for pt in points_data]
        with open("data/coverage_2026.json", "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"description": "地上AIS受信実績圏"}, "geometry": mapping(unary_union(buffers))}]}, f, ensure_ascii=False, separators=(',', ':'))
    except Exception as e: print(f"Error in dynamic coverage: {e}")

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
        except Exception: pass

if __name__ == "__main__":
    main()
