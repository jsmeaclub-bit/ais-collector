import os
import sys
import json
import asyncio
import websockets
import requests
import time
from datetime import datetime, timezone, timedelta

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
AISSTREAM_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL:
    print("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")
    sys.exit(1)

if not AISSTREAM_KEY:
    print("エラー: 環境変数 AISSTREAM_API_KEY が設定されていません。")
    sys.exit(1)

# 受信待機時間：3分間（180秒）
COLLECTION_DURATION_SEC = 180

def fetch_target_mmsi():
    """GASから監視対象船（約5,000隻）のMMSIリストを取得"""
    print("Fetching target MMSI list from GAS...")
    target_url = f"{GAS_URL}?api=mmsi"

    session = requests.Session()
    try:
        res = session.get(target_url, allow_redirects=True, timeout=60)
        res.raise_for_status()

        text = res.text.strip()
        if text.startswith("<"):
            print("Failed to load MMSI list: GAS returned HTML.")
            return set()

        data = res.json()
        if isinstance(data, list):
            mmsi_set = set(str(m).strip() for m in data if str(m).strip())
        else:
            mmsi_set = set()

        print(f"Loaded {len(mmsi_set)} target MMSIs.")
        return mmsi_set

    except Exception as e:
        print(f"Failed to load MMSI list: {e}")
        return set()

async def collect_ais_stream(target_mmsi):
    """AISStream.io から電波を受信し、5,347隻の対象船のみを抽出"""
    detected_ships = {}
    ws_url = "wss://stream.aisstream.io/v0/stream"

    # ★ サーバー切断を防ぐため、FiltersShipMMSI は送らず全世界指定（軽量）にする
    sub_message = {
        "APIKey": AISSTREAM_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]]
    }

    start_time = time.time()
    print(f"Connecting to AISStream.io. Collecting for {COLLECTION_DURATION_SEC} seconds...")

    while True:
        elapsed = time.time() - start_time
        remaining = COLLECTION_DURATION_SEC - elapsed
        if remaining <= 0:
            break

        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=10 * 1024 * 1024 # 受信バッファ拡張
            ) as ws:
                await ws.send(json.dumps(sub_message))
                print(f"WebSocket connected. Stream active (Remaining: {int(remaining)}s)...")

                while True:
                    if time.time() - start_time >= COLLECTION_DURATION_SEC:
                        break

                    try:
                        msg_text = await asyncio.wait_for(ws.recv(), timeout=8.0)
                        msg = json.loads(msg_text)

                        meta = msg.get("MetaData", {})
                        mmsi = str(meta.get("MMSI", "")).strip()

                        # ★ 5,347隻の対象船に該当するかメモリ上で判定
                        if not mmsi or mmsi not in target_mmsi:
                            continue

                        lat = meta.get("latitude")
                        lng = meta.get("longitude")
                        if lat is None or lng is None or (lat == 0 and lng == 0):
                            continue

                        # 速力・船名の抽出
                        msg_type = msg.get("MessageType")
                        msg_body = msg.get("Message", {})
                        speed = 0.0

                        if msg_type == "PositionReport" and "PositionReport" in msg_body:
                            pos = msg_body["PositionReport"]
                            lat = pos.get("Latitude", lat)
                            lng = pos.get("Longitude", lng)
                            speed = pos.get("Sog", 0.0)
                        elif msg_type == "StandardClassBPositionReport" and "StandardClassBPositionReport" in msg_body:
                            pos = msg_body["StandardClassBPositionReport"]
                            lat = pos.get("Latitude", lat)
                            lng = pos.get("Longitude", lng)
                            speed = pos.get("Sog", 0.0)

                        ship_name = str(meta.get("ShipName", "Unknown")).strip()

                        # 最新位置の更新
                        detected_ships[mmsi] = {
                            "mmsi": mmsi,
                            "name": ship_name,
                            "lat": round(float(lat), 5),
                            "lng": round(float(lng), 5),
                            "speed": round(float(speed), 1),
                            "timestamp": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
                        }

                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        print("WebSocket temporarily closed. Reconnecting...")
                        break

        except Exception as e:
            print(f"Connection error: {e}. Retrying in 3 seconds...")
            await asyncio.sleep(3)

    return detected_ships

def post_traffic_to_gas(ships_dict):
    """検知した船舶データをGASへ送信（スプレッドシート更新）"""
    if not ships_dict:
        print("No target ships detected in this run. Skipping GAS update.")
        return

    ships_list = list(ships_dict.values())
    print(f"Total target ships detected: {len(ships_list)}")
    print("Posting detected ships to GAS...")

    payload = {
        "action": "update_traffic",
        "ships": ships_list
    }

    try:
        session = requests.Session()
        res = session.post(GAS_URL, json=payload, allow_redirects=True, timeout=60)
        print(f"GAS response status: {res.status_code}")
        print(f"GAS response: {res.text[:150]}")
    except Exception as e:
        print(f"Failed to post to GAS: {e}")

def main():
    target_mmsi = fetch_target_mmsi()
    if not target_mmsi:
        print("No target MMSI found. Exiting.")
        sys.exit(0)

    detected = asyncio.run(collect_ais_stream(target_mmsi))
    post_traffic_to_gas(detected)

if __name__ == "__main__":
    main()
