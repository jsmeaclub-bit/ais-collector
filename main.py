import os
import sys
import json
import asyncio
import datetime
import websockets
import requests

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")
API_KEY = os.environ.get("AISSTREAM_API_KEY")

if not GAS_URL or not API_KEY:
    print("Error: Missing GAS_WEBHOOK_URL or AISSTREAM_API_KEY")
    sys.exit(1)

# 1. 監視対象MMSIリストをGASから取得
def fetch_target_mmsis():
    print("Fetching target MMSI list from GAS...")
    url = f"{GAS_URL}?api=mmsi"
    try:
        res = requests.get(url, timeout=30)
        if res.status_code == 200:
            mmsi_list = res.json()
            print(f"Loaded {len(mmsi_list)} target MMSIs.")
            return set(str(m) for m in mmsi_list)
        else:
            print(f"Failed to fetch MMSI: status {res.status_code}")
            return set()
    except Exception as e:
        print(f"Error fetching MMSI: {e}")
        return set()

# 2. AISStream から位置(Msg 1,2,3)と静態(Msg 5: Destination/ETA)を受信
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
            print("WebSocket connected. Stream active...")
            start_time = asyncio.get_event_loop().time()

            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= duration_sec:
                    break

                try:
                    raw_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    msg = json.loads(raw_msg)
                    msg_type = msg.get("MessageType")
                    metadata = msg.get("MetaData", {})
                    mmsi = str(metadata.get("MMSI", ""))

                    if mmsi not in target_mmsis:
                        continue

                    if mmsi not in ships_data:
                        ships_data[mmsi] = {
                            "mmsi": mmsi,
                            "name": metadata.get("ShipName", "").strip(),
                            "lat": None,
                            "lng": None,
                            "speed": 0.0,
                            "timestamp": metadata.get("time_utc", ""),
                            "destination": "-",
                            "eta": "-"
                        }

                    # 位置メッセージ (Message 1, 2, 3)
                    if msg_type == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                        ships_data[mmsi]["lat"] = pos.get("Latitude")
                        ships_data[mmsi]["lng"] = pos.get("Longitude")
                        ships_data[mmsi]["speed"] = pos.get("Sog", 0.0)
                        if metadata.get("time_utc"):
                            ships_data[mmsi]["timestamp"] = metadata.get("time_utc")

                    # 静態メッセージ (Message 5: 仕向港・ETA)
                    elif msg_type == "ShipStaticData":
                        static = msg.get("Message", {}).get("ShipStaticData", {})
                        dest = static.get("Destination", "").strip()
                        if dest:
                            ships_data[mmsi]["destination"] = dest
                        
                        eta_dict = static.get("Eta", {})
                        if eta_dict:
                            mo = eta_dict.get("Month", 0)
                            day = eta_dict.get("Day", 0)
                            hr = eta_dict.get("Hour", 0)
                            mn = eta_dict.get("Minute", 0)
                            if 1 <= mo <= 12 and 1 <= day <= 31 and hr < 24 and mn < 60:
                                current_year = datetime.datetime.utcnow().year
                                ships_data[mmsi]["eta"] = f"{current_year}/{mo:02d}/{day:02d} {hr:02d}:{mn:02d} UTC"

                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue

    except Exception as e:
        print(f"WebSocket connection error: {e}")

    valid_ships = [s for s in ships_data.values() if s["lat"] is not None and s["lng"] is not None]
    return valid_ships

# 3. GAS へ送信
def post_to_gas(ships):
    print(f"Total target ships detected: {len(ships)}")
    if not ships:
        return
    print("Posting detected ships with Destination/ETA to GAS...")
    try:
        res = requests.post(GAS_URL, json={"ships": ships}, timeout=60)
        print(f"GAS response status: {res.status_code}")
        print(f"GAS response: {res.text}")
    except Exception as e:
        print(f"Error posting to GAS: {e}")

def main():
    target_mmsis = fetch_target_mmsis()
    if not target_mmsis:
        print("No target MMSIs loaded. Exiting.")
        sys.exit(0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ships = loop.run_until_complete(collect_ais_data(target_mmsis, duration_sec=180))
    post_to_gas(ships)

if __name__ == "__main__":
    main()
