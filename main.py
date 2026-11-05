import os
import json
import asyncio
import websockets
import requests
from datetime import datetime

# 環境変数からキーとURLを取得
AIS_KEY = os.environ.get("AISSTREAM_API_KEY")
GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

# --- 1. GASから監視対象のMMSIリストを取得 ---
print("Fetching target MMSI list from GAS...")
mmsi_url = GAS_URL + "?action=get_mmsi"
target_mmsis = set()
try:
    res = requests.get(mmsi_url, allow_redirects=True, timeout=30)
    if res.status_code == 200:
        target_mmsis = set(res.json())
        print(f"Target MMSIs loaded: {len(target_mmsis)} vessels.")
    else:
        print(f"Error fetching MMSI: HTTP {res.status_code}")
except Exception as e:
    print(f"Failed to load MMSI list: {e}")

async def collect_ais():
# BoundingBoxを主要海域に絞って分割指定（通信パンク回避）
    subscribe_message = {
        "APIKey": AIS_KEY,
        "BoundingBoxes": [
            [[-10, 95], [45, 145]],   # 東アジア〜東南アジア (日本, 台湾, シンガポール, マラッカ海峡)
            [[10, -100], [50, -50]],  # 北米東海岸・メキシコ湾・パナマ運河
            [[30, -10], [60, 30]],    # 欧州・地中海・ジブラルタル海峡
            [[10, 30], [30, 60]]      # 中東・紅海・スエズ運河・ペルシャ湾
        ],
        "FilterMessageTypes": ["PositionReport"]
    }
    
    target_vessels = {}
    timeout_seconds = 180  # 3分間ストリームを受信
    
    print("Connecting to AIS Stream (Global)...")
    try:
        async with websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
            await ws.send(json.dumps(subscribe_message))
            start_time = datetime.now()
            
            while (datetime.now() - start_time).seconds < timeout_seconds:
                try:
                    # 1秒ごとにメッセージを待機
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message)
                    
                    if data.get("MessageType") == "PositionReport":
                        report = data["Message"]["PositionReport"]
                        mmsi = str(report.get("UserID", ""))
                        
                        # --- 3. 圧縮判定: リストにあるMMSIだけを辞書に上書き保存（重複排除） ---
                        if mmsi in target_mmsis:
                            target_vessels[mmsi] = {
                                "MMSI": mmsi,
                                "Latitude": report.get("Latitude", 0),
                                "Longitude": report.get("Longitude", 0),
                                "Speed": report.get("Sog", 0),
                                "ShipName": "-" # 名前はGAS側でマスタから補完
                            }
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"Parse error: {e}")
                    continue
                    
    except Exception as e:
        print(f"WebSocket Connection error: {e}")

    # --- 4. GASへ圧縮済みデータをPOST ---
    payload = list(target_vessels.values())
    print(f"Collection finished. Sending {len(payload)} matched vessels to GAS.")
    
    if payload:
        try:
            post_res = requests.post(GAS_URL, json=payload, headers={'Content-Type': 'application/json'}, allow_redirects=True)
            print(f"GAS Response: {post_res.status_code}")
        except Exception as e:
            print(f"Failed to post to GAS: {e}")
    else:
        print("No target vessels found in this batch.")

if __name__ == "__main__":
    if not target_mmsis:
        print("No target MMSI found. Exiting to prevent full global processing.")
    else:
        asyncio.run(collect_ais())
