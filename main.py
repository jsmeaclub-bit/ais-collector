import asyncio
import json
import os
import requests
import websockets

# =============================================================
# 1. 環境設定
# =============================================================
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL")

# =============================================================
# 2. AISStream からデータを受信（約3分間）
# =============================================================
collected_vessels = {}


async def collect_ais():
  # 監視海域: シンガポール・マラッカ海峡周辺
  bounding_boxes = [[[1.0, 103.0], [1.5, 104.5]]]

  subscribe_message = {
      "APIKey": AISSTREAM_API_KEY,
      "BoundingBoxes": bounding_boxes,
      "FilterMessageTypes": ["PositionReport"],
  }

  uri = "wss://stream.aisstream.io/v0/stream"

  try:
    async with websockets.connect(uri) as websocket:
      await websocket.send(json.dumps(subscribe_message))
      print("■ AISStream に接続しました。データ受信中...")

      # 180秒（3分間）データを蓄積
      end_time = asyncio.get_event_loop().time() + 180

      while asyncio.get_event_loop().time() < end_time:
        try:
          message_json = await asyncio.wait_for(websocket.recv(), timeout=5.0)
          msg = json.loads(message_json)

          if msg.get("MessageType") == "PositionReport":
            pos = msg["Message"]["PositionReport"]
            mmsi = msg["MetaData"]["MMSI"]
            ship_name = msg["MetaData"].get("ShipName", "Unknown")

            collected_vessels[mmsi] = {
                "MMSI": mmsi,
                "ShipName": ship_name.strip(),
                "Latitude": pos["Latitude"],
                "Longitude": pos["Longitude"],
                "Speed": pos["Sog"],
            }
        except asyncio.TimeoutError:
          continue

  except Exception as e:
    print(f"受信終了またはエラー: {e}")


# =============================================================
# 3. GASのWebアプリURLへデータを送信
# =============================================================
def send_to_gas():
  if not collected_vessels:
    print("受信データが0件のため送信をスキップします。")
    return

  payload = list(collected_vessels.values())
  print(f"■ GASへ送信中...（対象隻数: {len(payload)} 隻）")

  try:
    response = requests.post(GAS_WEBAPP_URL, json=payload, timeout=30)
    print(f"■ 送信結果: {response.status_code}")
    print(f"■ 応答内容: {response.text}")
  except Exception as e:
    print(f"GAS送信エラー: {e}")


# =============================================================
# 4. 実行
# =============================================================
if __name__ == "__main__":
  asyncio.run(collect_ais())
  send_to_gas()
