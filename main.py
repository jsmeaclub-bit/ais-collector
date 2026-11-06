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
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely"])
    import searoute as sr
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 航路計算・平滑化
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    """
    全世界の陸地を避けるため、searouteライブラリを強制適用。
    海上の最短ルートを自動探索し、陸を横切る直線化を防止する。
    """
    # 距離が極端に近い（約5km未満）場合はそのまま直線化（港内での微小なブレ対応）
    if math.hypot(lat1 - lat2, lon1 - lon2) < 0.05:
        return [[round(lat1, 4), round(lon1, 4)], [round(lat2, 4), round(lon2, 4)]]

    try:
        # include_ports=True にすることで、港湾内の複雑な地形でもエラーになりにくい
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km", include_ports=True)
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 4), round(pt[0], 4)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2:
                return coords
    except Exception as e:
        print(f"SeaRoute error ({lat1},{lon1} -> {lat2},{lon2}): {e}")

    # searouteがどうしても失敗した場合のみ直線フォールバック
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
        if diff > 180.0: offset -= 360.0; raw_lon -= 360.0
        elif diff < -180.0: offset += 360.0; raw_lon += 360.0
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
            p0, p1 = current[i], current[i + 1]
            next_pts.append([round(0.75*p0[0] + 0.25*p1[0], 4), round(0.75*p0[1] + 0.25*p1[1], 4)])
            next_pts.append([round(0.25*p0[0] + 0.75*p1[0], 4), round(0.25*p0[1] + 0.75*p1[1], 4)])
        next_pts.append(current[-1])
        current = next_pts
    return current

def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    while dl > math.pi: dl -= 2 * math.pi
    while dl < -math.pi: dl += 2 * math.pi
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 1)

# ==============================================================================
# 2. 基礎データ取得
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

    existing_tracks = {}
    res_tracks = session.get(tracks_url, timeout=60)
    if res_tracks.status_code == 200:
        for item in res_tracks.json():
            existing_tracks[str(item.get("mmsi"))] = {
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
# 3. AIS受信 (180秒)
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
            start_time = asyncio.get_event_loop().time()

            while True:
                if asyncio.get_event_loop().time() - start_time >= duration_sec: break
                try:
                    msg = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
                    metadata = msg.get("MetaData", {})
                    mmsi = str(metadata.get("MMSI", ""))
                    if mmsi not in target_mmsis: continue

                    if mmsi not in ships_data:
                        ships_data[mmsi] = {"mmsi": mmsi, "name": metadata.get("ShipName", "").strip(), "lat": None, "lng": None, "speed": 0.0, "destination": "-", "eta": "-"}

                    if msg.get("MessageType") == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                        ships_data[mmsi]["lat"] = pos.get("Latitude")
                        ships_data[mmsi]["lng"] = pos.get("Longitude")
                        ships_data[mmsi]["speed"] = pos.get("Sog", 0.0)

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

# ==============================================================================
# 4. 完全同期幾何計算
# ==============================================================================
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

        # 過剰な間引きを抑え、地形に沿ったポイントを保持する
        cleaned_pts = []
        for i, p in enumerate(pts):
            p_lat, p_lon = p[0], p[1]
            if not (-90 <= p_lat <= 90 and -180 <= p_lon <= 180): continue
            if not cleaned_pts or i == len(pts) - 1 or math.hypot(p_lat - cleaned_pts[-1][0], p_lon - cleaned_pts[-1][1]) > 0.005:
                cleaned_pts.append([p_lat, p_lon])

        if len(cleaned_pts) < 2: continue

        full_track = []
        for i in range(len(cleaned_pts) - 1):
            seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
            full_track.extend(seg if not full_track else seg[1:])

        curved_track = chaikin_smooth(unwrap_coordinates(full_track), iterations=3)
        curved_track[-1] = [lat, lng] # 先端アンカー固定

        total_pts = len(curved_track)
        step = max(1, (total_pts - 1) // 14)
        fade_segments = []

        for i in range(0, total_pts - 1, step):
            end_idx = min(i + step + 1, total_pts)
            sub_coords = curved_track[i:end_idx]
            if len(sub_coords) < 2: continue
            
            t_val = end_idx / total_pts
            fade_segments.append({
                "coords": sub_coords,
                "weight": round(1.0 + (2.8 * (t_val ** 1.2)), 2),
                "opacity": round(0.06 + (0.89 * (t_val ** 1.5)), 3)
            })

        heading = 0.0
        if len(curved_track) >= 2:
            last_p = curved_track[-1]
            for prev_p in reversed(curved_track[:-1]):
                if math.hypot(last_p[0] - prev_p[0], last_p[1] - prev_p[1]) > 0.0005:
                    heading = calculate_bearing(prev_p[0], prev_p[1], last_p[0], last_p[1])
                    break

        processed_tracks.append({"mmsi": mmsi, "name": l_ship.get("name", "Unknown"), "cat": l_ship.get("category", "日本支配船"), "coords": curved_track, "segments": fade_segments, "heading": heading})

    return synchronized_latest_data, processed_tracks

# ==============================================================================
# 5. カバレッジ生成
# ==============================================================================
def generate_dynamic_coverage(session):
    print("Generating dynamic AIS coverage...")
    try:
        res = session.get(f"{GAS_URL}?api=coverage_points", allow_redirects=True, timeout=60)
        points_data = res.json() if res.status_code == 200 else []
        if not points_data: return

        buffers = [Point(pt[1], pt[0]).buffer(0.85, resolution=8) for pt in points_data]
        merged = unary_union(buffers)
        with open("data/coverage_2026.json", "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"description": "地上AIS受信実績圏"}, "geometry": mapping(merged)}]}, f, ensure_ascii=False, separators=(',', ':'))
    except Exception as e: print(f"Error in dynamic coverage: {e}")

# ==============================================================================
# 6. メイン実行フロー
# ==============================================================================
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
