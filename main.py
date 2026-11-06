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
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute"])
    import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# ==============================================================================
# 1. 航路計算・等間隔リサンプリング（陸地めり込み防止）
# ==============================================================================
def calculate_sea_route(lat1, lon1, lat2, lon2):
    """searouteを全世界に強制適用し、陸地回避を優先する。"""
    if math.hypot(lat1 - lat2, lon1 - lon2) < 0.02:
        return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km", append_orig_dest=True)
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2:
                return coords
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

def resample_path(points, num_points=70):
    """
    ★最重要修正箇所★
    角を削って陸地にめり込む原因となっていた「曲線の平滑化」を廃止。
    代わりに、安全な頂点をなぞったまま、フェード描画用に等間隔の点を打ち直す。
    """
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
        while idx < len(dists) and dists[idx] < target:
            idx += 1
        if idx >= len(dists): break
            
        d0, d1 = dists[idx-1], dists[idx]
        p0, p1 = points[idx-1], points[idx]
        
        if d1 > d0:
            ratio = (target - d0) / (d1 - d0)
            n_lat = p0[0] + ratio * (p1[0] - p0[0])
            n_lon = p0[1] + ratio * (p1[1] - p0[1])
            resampled.append([round(n_lat, 5), round(n_lon, 5)])
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
# 2. 基礎データ取得
# ==============================================================================
def fetch_base_data_from_gas(session):
    print("Fetching target MMSIs and historical tracks from GAS...")
    target_mmsis = set()
    existing_tracks = {}
    base_latest = {}

    try:
        res_mmsi = session.get(f"{GAS_URL}?api=mmsi", timeout=30)
        if res_mmsi.status_code == 200:
            target_mmsis = set(str(m) for m in res_mmsi.json())

        res_tracks = session.get(f"{GAS_URL}?api=ship_tracks", timeout=60)
        if res_tracks.status_code == 200:
            for item in res_tracks.json():
                existing_tracks[str(item.get("mmsi"))] = {
                    "name": item.get("name", "Unknown"),
                    "cat": item.get("cat", "日本支配船"),
                    "points": item.get("points", [])
                }

        res_latest = session.get(f"{GAS_URL}?api=latest", timeout=60)
        if res_latest.status_code == 200:
            base_latest = res_latest.json()
    except Exception as e:
        print(f"Error fetching GAS data: {e}")

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
# 4. 完全同期計算（東京湾特例迂回 ＋ リサンプリング）
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

        # 直線による陸地横断を防ぐため、ポイントの間引き距離を短くする（0.002度=約200m）
        cleaned_pts = []
        for i, p in enumerate(pts):
            p_lat, p_lon = p[0], p[1]
            if not (-90 <= p_lat <= 90 and -180 <= p_lon <= 180): continue
            if not cleaned_pts or i == len(pts) - 1 or math.hypot(p_lat - cleaned_pts[-1][0], p_lon - cleaned_pts[-1][1]) > 0.002:
                cleaned_pts.append([p_lat, p_lon])

        if len(cleaned_pts) < 2: continue

        full_track = []
        for i in range(len(cleaned_pts) - 1):
            seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], cleaned_pts[i+1][0], cleaned_pts[i+1][1])
            
            # ★東京湾特例処理: 羽田・川崎の陸地を横切る線が発生した場合、東側の海上に強制迂回させる
            for p_seg in seg:
                if 35.48 < p_seg[0] < 35.58 and 139.71 < p_seg[1] < 139.81:
                    seg = calculate_sea_route(cleaned_pts[i][0], cleaned_pts[i][1], 35.530, 139.840) + \
                          calculate_sea_route(35.530, 139.840, cleaned_pts[i+1][0], cleaned_pts[i+1][1])
                    break

            full_track.extend(seg if not full_track else seg[1:])

        unwrapped_track = unwrap_coordinates(full_track)
        
        # 曲線平滑化（Chaikin）を行わず、リサンプリングのみでフェード用の点を生成する
        resampled_track = resample_path(unwrapped_track, num_points=70)
        resampled_track[-1] = [lat, lng]

        total_pts = len(resampled_track)
        step = max(1, (total_pts - 1) // 14)
        fade_segments = []

        for i in range(0, total_pts - 1, step):
            end_idx = min(i + step + 1, total_pts)
            sub_coords = resampled_track[i:end_idx]
            if len(sub_coords) < 2: continue
            
            t_val = end_idx / total_pts
            fade_segments.append({
                "coords": sub_coords,
                "weight": round(1.0 + (2.8 * (t_val ** 1.2)), 2),
                "opacity": round(0.06 + (0.89 * (t_val ** 1.5)), 3)
            })

        heading = 0.0
        if len(resampled_track) >= 2:
            last_p = resampled_track[-1]
            for prev_p in reversed(resampled_track[:-1]):
                if math.hypot(last_p[0] - prev_p[0], last_p[1] - prev_p[1]) > 0.0005:
                    heading = calculate_bearing(prev_p[0], prev_p[1], last_p[0], last_p[1])
                    break

        processed_tracks.append({"mmsi": mmsi, "name": l_ship.get("name", "Unknown"), "cat": l_ship.get("category", "日本支配船"), "coords": resampled_track, "segments": fade_segments, "heading": heading})

    return synchronized_latest_data, processed_tracks

# ==============================================================================
# 5. メイン実行フロー
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

    if new_ships:
        try: requests.post(GAS_URL, json={"ships": new_ships}, timeout=60)
        except Exception: pass

if __name__ == "__main__":
    main()
