import os
import sys
import json
import math
import asyncio
import datetime
import websockets
import requests

try:
    import searoute as sr
    import numpy as np
    from scipy.interpolate import splprep, splev
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely", "scipy", "numpy"])
    import searoute as sr
    import numpy as np
    from scipy.interpolate import splprep, splev
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 美しい船舶旋回カーブの生成（Bスプライン曲線）
# ==============================================================================
def b_spline_smooth(points, num_points=70):
    """
    複雑な画像処理による陸地回避を廃止し、
    自然な船の旋回（大きな弧を描く動き）のみを数式で適用する。
    """
    if not points or len(points) < 3:
        return resample_path_linear(points, num_points)
    
    # 座標の重複や極端な近接点を除去（スプライン計算エラー防止）
    unique_pts = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - unique_pts[-1][0], p[1] - unique_pts[-1][1]) > 1e-4:
            unique_pts.append(p)
            
    if len(unique_pts) < 3:
        return resample_path_linear(unique_pts, num_points)
        
    pts_arr = np.array(unique_pts)
    x = pts_arr[:, 0]
    y = pts_arr[:, 1]
    
    try:
        # k=3(3次曲線)で船の自然な動きを再現
        k = min(3, len(unique_pts) - 1)
        tck, u = splprep([x, y], s=0.002, k=k)
        u_new = np.linspace(0, 1, num_points)
        x_new, y_new = splev(u_new, tck)
        
        # 始点と終点は必ず実座標に完全一致させる
        x_new[0], y_new[0] = x[0], y[0]
        x_new[-1], y_new[-1] = x[-1], y[-1]
        
        return [[round(x_new[i], 5), round(y_new[i], 5)] for i in range(num_points)]
    except Exception as e:
        print("Spline fallback:", e)
        return resample_path_linear(unique_pts, num_points)

def resample_path_linear(points, num_points=70):
    """直線的なフォールバック用等間隔リサンプリング"""
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
# 2. ベース航路計算
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    """外洋ルーティングエンジンのみを使用"""
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
# 3. 実行フロー (新データ項目対応)
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
                    elif msg.get("MessageType") == "ShipStaticData":
                        static = msg.get("Message", {}).get("ShipStaticData", {})
                        dest = static.get("Destination", "").strip()
                        if dest: ships_data[mmsi]["destination"] = dest
                        eta = static.get("Eta", {})
                        if eta and 1 <= eta.get("Month", 0) <= 12 and 1 <= eta.get("Day", 0) <= 31:
                            ships_data[mmsi]["eta"] = f"{datetime.datetime.utcnow().year}/{eta['Month']:02d}/{eta['Day']:02d} {eta.get('Hour',0):02d}:{eta.get('Minute',0):02d} UTC"
                except: continue
    except: pass
    return [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]

def process_and_synchronize_tracks(new_ships, existing_tracks, base_latest):
    latest_map = {str(s["mmsi"]): s for s in base_latest.get("latest", [])}
    for s in new_ships:
        if (m := str(s["mmsi"])) in latest_map: 
            latest_map[m].update({"lat": s["lat"], "lng": s["lng"], "speed": s["speed"]})
            if s["destination"] != "-": latest_map[m]["destination"] = s["destination"]
            if s["eta"] != "-": latest_map[m]["eta"] = s["eta"]
        else: 
            latest_map[m] = {
                "mmsi": m, "name": s["name"] or m, "category": "日本支配船", 
                "lat": s["lat"], "lng": s["lng"], "speed": s["speed"], 
                "destination": s["destination"], "eta": s["eta"], "etaPassed": False, 
                "imo": "-", "flag": "-", "owner": "-", "operator": "-", 
                "type": "-", "gt": "-", "survey": "-"
            }

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

        # ★ スプライン曲線のみを適用し、画像処理での回避は行わない
        final_track = b_spline_smooth(unwrap_coordinates(full_track), num_points=70)
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
