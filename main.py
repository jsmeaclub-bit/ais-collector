import os
import json
import time
import math
import random
import asyncio
import websockets
import requests
from datetime import datetime, timezone
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 共通設定・環境変数
# ==========================================
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

LATEST_FILE = os.path.join(DATA_DIR, "latest_2026.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage_2026.json")
POINTS_CACHE_FILE = os.path.join(DATA_DIR, "coverage_points.json")


# ==========================================
# 1. マスターデータの取得 (Google Sheets API / サービスアカウント)
# ==========================================
def get_master_data():
    """
    サービスアカウント認証を用いて Google スプレッドシート（Japan_Master シート）から
    モニタリング対象船マスターを直接取得
    """
    if not GCP_SA_KEY or not SPREADSHEET_ID:
        print("[ERROR] GCP_SA_KEY or SPREADSHEET_ID is not configured in Secrets.")
        return {}, {}

    try:
        sa_info = json.loads(GCP_SA_KEY)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
        client = gspread.authorize(creds)

        # スプレッドシートを開き、Japan_Master シートの全レコードを取得
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.worksheet("Japan_Master")
        records = sheet.get_all_records()

        master_map = {}
        flag_cnt, ctrl_cnt = 0, 0

        for r in records:
            raw_mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
            if not raw_mmsi or raw_mmsi in ("nan", "None", "0"):
                continue

            category = str(r.get("Category", "日本船社運航船")).strip()
            if category == "日本籍船":
                flag_cnt += 1
            else:
                ctrl_cnt += 1

            master_map[raw_mmsi] = {
                "mmsi": raw_mmsi,
                "name": str(r.get("Name of Ship", "")).strip(),
                "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
                "category": category,
                "flag": str(r.get("Flag", "-")).strip(),
                "type": str(r.get("Ship Type", "-")).strip(),
                "gt": str(r.get("GT", "-")).strip(),
                "owner": str(r.get("Registered Owner", "-")).strip(),
                "operator": str(r.get("Operator", "-")).strip(),
                "survey": str(r.get("Special Survey", "-")).strip()
            }

        master_counts = {
            "total": len(master_map),
            "flag": flag_cnt,
            "ctrl": ctrl_cnt
        }
        print(f"[INFO] Successfully loaded {len(master_map)} target vessels from Google Spreadsheet.")
        return master_map, master_counts

    except Exception as e:
        print(f"[ERROR] Failed to fetch master from Google Spreadsheet: {e}")
        return {}, {}


# ==========================================
# 2. AISStream.io からデータ受信
# ==========================================
async def collect_ais_data(target_mmsis):
    """
    AISStream.io の WebSocket に接続し、対象 MMSI の船舶データを受信。
    ・手動実行（workflow_dispatch やローカル確認）: 2分（120秒）固定
    ・スケジュール実行（cron）: 2分（120秒）〜10分（600秒）の間でランダム
    """
    received_ships = {}
    if not AISSTREAM_API_KEY:
        print("[ERROR] AISSTREAM_API_KEY is not set.")
        return received_ships

    # 実行契機に応じた待機時間の切り替え
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if event_name == "schedule":
        listen_duration = random.randint(120, 600)
        mode_desc = "Scheduled run (randomized)"
    else:
        listen_duration = 120
        mode_desc = "Manual run (fixed 2m for quick testing)"

    mins, secs = divmod(listen_duration, 60)
    print(f"[INFO] Mode: {mode_desc}")
    print(f"[INFO] Connecting to AISStream.io for {listen_duration} seconds ({mins}m {secs}s)...")

    url = "wss://stream.aisstream.io/v0/stream"
    subscribe_msg = {
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"]
    }

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(subscribe_msg))
            start_time = time.time()

            while time.time() - start_time < listen_duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    mmsi = str(data.get("MetaData", {}).get("MMSI", "")).strip().split(".")[0]

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
                        if dest:
                            ship["destination"] = dest
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
# 3. AIS受信実績圏（50海里）の即時動的更新
# ==========================================
def update_coverage_geojson(all_points_list):
    """
    受信したすべての船舶位置（蓄積全隻）から
    半径50海里（約92.6km）のバッファポリゴンを生成し coverage_2026.json を更新
    """
    all_points = set()

    # 1. 過去地点のキャッシュ読み込み
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                all_points = {tuple(p) for p in cached}
        except Exception:
            all_points = set()

    # 2. 最新座標を追加
    for lat, lng in all_points_list:
        if lat is not None and lng is not None:
            all_points.add((round(lat, 2), round(lng, 2)))

    # キャッシュの保存
    with open(POINTS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump([list(p) for p in all_points], f)

    if not all_points:
        return

    # 3. 半径50海里（約0.833度）のバッファ円を作成
    radius_deg = 50.0 / 60.0
    polygons = []

    for lat, lng in all_points:
        cos_lat = math.cos(math.radians(lat))
        if cos_lat < 0.1:
            cos_lat = 0.1

        pts = []
        for i in range(16):
            angle = 2 * math.pi * i / 16
            dx = (radius_deg / cos_lat) * math.cos(angle)
            dy = radius_deg * math.sin(angle)
            pts.append((lng + dx, lat + dy))
        polygons.append(Polygon(pts))

    # 全ポリゴンを統合し、GeoJSONとして書き出し
    merged_polygon = unary_union(polygons)
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
# 4. メインルーチン
# ==========================================
def main():
    # 1. マスターデータの取得
    master_map, master_counts = get_master_data()
    target_mmsis = set(master_map.keys())

    if not target_mmsis:
        print("[WARN] No target MMSIs loaded. Terminating run.")
        return

    # 2. AISストリーム受信の実行
    live_ships = asyncio.run(collect_ais_data(target_mmsis))

    # 3. 既存の最新状態（latest_2026.json）の読み込み
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                for s in old_data.get("latest", []):
                    existing_latest[str(s["mmsi"])] = s
        except Exception:
            existing_latest = {}

    # 4. 最新位置情報とマスター属性のマージ
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

    # 5. latest_2026.json の書き出し
    latest_output = {
        "masterCounts": master_counts,
        "latest": list(existing_latest.values())
    }
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(latest_output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] latest_2026.json saved. Total monitored in latest: {len(existing_latest)} vessels.")

    # 6. 蓄積された全隻の有効座標から50海里圏を一括生成
    all_active_points = [
        (s["lat"], s["lng"])
        for s in existing_latest.values()
        if s.get("lat") is not None and s.get("lng") is not None
    ]
    update_coverage_geojson(all_active_points)


if __name__ == "__main__":
    main()
