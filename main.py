import os
import json
import time
import math
import asyncio
import websockets
import requests
from datetime import datetime, timezone
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

# ==========================================
# 設定・環境変数
# ==========================================
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GAS_WEBHOOK_URL = os.environ.get("GAS_WEBHOOK_URL")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

LATEST_FILE = os.path.join(DATA_DIR, "latest_2026.json")
ROUTES_FILE = os.path.join(DATA_DIR, "routes_2026.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage_2026.json")
POINTS_CACHE_FILE = os.path.join(DATA_DIR, "coverage_points.json")

LISTEN_DURATION_SEC = 120  # 受信待機時間（秒）

# ==========================================
# 1. マスターデータの取得 (GAS Webhook経由)
# ==========================================
def get_master_data():
    if not GAS_WEBHOOK_URL:
        print("[WARN] GAS_WEBHOOK_URL is not set.")
        return {}, {}
    try:
        resp = requests.get(GAS_WEBHOOK_URL, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            master_map = {str(item["mmsi"]): item for item in data.get("master", []) if item.get("mmsi")}
            master_counts = data.get("masterCounts", {})
            print(f"[INFO] Loaded {len(master_map)} target vessels from Japan_Master.")
            return master_map, master_counts
    except Exception as e:
        print(f"[ERROR] Failed to fetch master data: {e}")
    return {}, {}

# ==========================================
# 2. AISStream.io からデータ受信
# ==========================================
async def collect_ais_data(target_mmsis):
    received_ships = {}
    if not AISSTREAM_API_KEY:
        print("[ERROR] AISSTREAM_API_KEY is not set.")
        return received_ships

    url = "wss://stream.aisstream.io/v0/stream"
    subscribe_msg = {
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"]
    }

    print(f"[INFO] Connecting to AISStream.io for {LISTEN_DURATION_SEC} seconds...")
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(subscribe_msg))
            start_time = time.time()

            while time.time() - start_time < LISTEN_DURATION_SEC:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    mmsi = str(data.get("MetaData", {}).get("MMSI", ""))

                    if mmsi not in target_mmsis:
                        continue

                    msg_type = data.get("MessageType")
                    meta = data.get("MetaData", {})

                    if mmsi not in received_ships:
                        received_ships[mmsi] = {
                            "mmsi": mmsi,
                            "name": meta.get("ShipName", "").strip(),
                            "lat": meta.get("latitude"),
                            "lng": meta.get("longitude"),
                            "speed": 0.0,
                            "heading": 0.0,
                            "destination": "",
                            "eta": "",
                            "timestamp": meta.get("time_utc")
                        }

                    ship = received_ships[mmsi]
                    if msg_type == "PositionReport":
                        pos = data.get("Message", {}).get("PositionReport", {})
                        ship["lat"] = pos.get("Latitude", ship["lat"])
                        ship["lng"] = pos.get("Longitude", ship["lng"])
                        ship["speed"] = round(pos.get("Sog", 0.0), 1)
                        cog = pos.get("Cog", 0.0)
                        hdg = pos.get("TrueHeading", 511)
                        ship["heading"] = round(hdg if hdg != 511 else cog, 1)

                    elif msg_type == "ShipStaticData":
                        static = data.get("Message", {}).get("ShipStaticData", {})
                        dest = static.get("Destination", "").strip()
                        if dest: ship["destination"] = dest
                        eta = static.get("Eta", {})
                        if eta and eta.get("Month"):
                            ship["eta"] = f"{eta.get('Month'):02d}-{eta.get('Day'):02d} {eta.get('Hour'):02d}:{eta.get('Minute'):02d}"

                except asyncio.TimeoutError:
                    continue
                except Exception as ex:
                    print(f"[WARN] Error parsing message: {ex}")
                    continue

    except Exception as e:
        print(f"[ERROR] WebSocket connection error: {e}")

    print(f"[INFO] Collected live data for {len(received_ships)} target vessels.")
    return received_ships

# ==========================================
# 3. ★ 最新地点を取り込んだAIS受信実績圏（50海里）の即時更新
# ==========================================
def update_coverage_geojson(live_points):
    """
    過去の受信地点キャッシュと今回の最新位置を合算し、
    周囲50海里（約92.6km）のバッファGeoJSON（coverage_2026.json）を即座に再生成
    """
    all_points = set()

    # 既存の地点キャッシュを読み込み
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                all_points = {tuple(p) for p in cached}
        except Exception:
            all_points = set()

    # 今回受信した最新位置を丸めて追加（0.05度単位で重複排除し肥大化を防止）
    for lat, lng in live_points:
        if lat is not None and lng is not None:
            all_points.add((round(lat, 2), round(lng, 2)))

    # 更新された地点キャッシュを保存
    with open(POINTS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump([list(p) for p in all_points], f)

    if not all_points:
        return

    # 半径50海里（約0.833度）の円ポリゴンを生成（緯度に応じた経度補正付き）
    radius_deg = 50.0 / 60.0
    polygons = []

    for lat, lng in all_points:
        cos_lat = math.cos(math.radians(lat))
        if cos_lat < 0.1: cos_lat = 0.1
        
        # 16角形で円を近似
        pts = []
        for i in range(16):
            angle = 2 * math.pi * i / 16
            dx = (radius_deg / cos_lat) * math.cos(angle)
            dy = radius_deg * math.sin(angle)
            pts.append((lng + dx, lat + dy))
        polygons.append(Polygon(pts))

    # 全ポリゴンをマージして単一の領域に統合
    merged_polygon = unary_union(polygons)
    # 容量削減のため頂点を適正化（ブラウザ描画の高速化）
    simplified_geom = merged_polygon.simplify(0.02, preserve_topology=True)

    geojson_data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "AIS Ground Station Coverage (50nm)",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                },
                "geometry": mapping(simplified_geom)
            }
        ]
    }

    with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(geojson_data, f, ensure_ascii=False)
    print(f"[INFO] coverage_2026.json updated successfully with {len(all_points)} ground-coverage points.")

# ==========================================
# 4. メイン実行フロー
# ==========================================
def main():
    master_map, master_counts = get_master_data()
    target_mmsis = set(master_map.keys())

    # 1. AISストリーム受信
    live_ships = asyncio.run(collect_ais_data(target_mmsis))

    # 2. 既存の最新データとマージ
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                for s in old_data.get("latest", []):
                    existing_latest[str(s["mmsi"])] = s
        except Exception:
            existing_latest = {}

    live_points = []
    for mmsi, ship in live_ships.items():
        m_info = master_map.get(mmsi, {})
        merged_ship = {
            "mmsi": mmsi,
            "name": m_info.get("name") or ship["name"] or f"MMSI:{mmsi}",
            "imo": m_info.get("imo", "-"),
            "category": m_info.get("category", "日本船社運航船"),
            "flag": m_info.get("flag", "-"),
            "type": m_info.get("type", "-"),
            "gt": m_info.get("gt", "-"),
            "owner": m_info.get("owner", "-"),
            "operator": m_info.get("operator", "-"),
            "survey": m_info.get("survey", "-"),
            "lat": ship["lat"],
            "lng": ship["lng"],
            "speed": ship["speed"],
            "heading": ship["heading"],
            "destination": ship["destination"] or existing_latest.get(mmsi, {}).get("destination", "-"),
            "eta": ship["eta"] or existing_latest.get(mmsi, {}).get("eta", "-"),
            "timestamp": ship["timestamp"]
        }
        existing_latest[mmsi] = merged_ship
        if ship["lat"] is not None and ship["lng"] is not None:
            live_points.append((ship["lat"], ship["lng"]))

    # 3. latest_2026.json の保存
    latest_output = {
        "masterCounts": master_counts,
        "latest": list(existing_latest.values())
    }
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(latest_output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] latest_2026.json saved. Total monitored in latest: {len(existing_latest)}")

    # 4. ★ AIS実績エリア（coverage_2026.json）の即時更新
    update_coverage_geojson(live_points)

    # 5. GAS側へも最新サマリーを同期（Webhook POST）
    if GAS_WEBHOOK_URL and live_ships:
        try:
            payload = {
                "action": "batch_update",
                "received_count": len(live_ships),
                "total_monitored": len(existing_latest),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            requests.post(GAS_WEBHOOK_URL, json=payload, timeout=15)
        except Exception as e:
            print(f"[WARN] Failed to notify GAS: {e}")

if __name__ == "__main__":
    main()
