import asyncio
import json
import os
import gspread
from google.oauth2.service_account import Credentials
import websockets

# =============================================================
# 1. 環境設定と認証
# =============================================================
# GitHubのSecrets（金庫）から自動で読み込まれます
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY_JSON = os.environ.get("GCP_SA_KEY")

# スプレッドシート名（お使いのシート名に合わせて変更してください）
SPREADSHEET_NAME = "外航商船_航路トラフィック管理"
WORKSHEET_NAME = "Route_Traffic"

# =============================================================
# 2. AISStream から一定時間データを受信
# =============================================================
collected_vessels = {}


async def collect_ais():
  # 監視する海域の四角い枠（例: シンガポール・マラッカ海峡周辺）
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

      # 3分間（180秒）データを収集して終了
      end_time = asyncio.get_event_loop().time() + 180

      while asyncio.get_event_loop().time() < end_time:
        try:
          message_json = await asyncio.wait_for(websocket.recv(), timeout=5.0)
          msg = json.loads(message_json)

          if msg.get("MessageType") == "PositionReport":
            pos = msg["Message"]["PositionReport"]
            mmsi = msg["MetaData"]["MMSI"]
            ship_name = msg["MetaData"].get("ShipName", "Unknown")

            # 最新の位置情報を記録
            collected_vessels[mmsi] = {
                "ShipName": ship_name.strip(),
                "Lat": pos["Latitude"],
                "Lon": pos["Longitude"],
                "Speed": pos["Sog"],
            }
        except asyncio.TimeoutError:
          continue

  except Exception as e:
    print(f"接続エラーまたは完了: {e}")


# =============================================================
# 3. Googleスプレッドシートへ書き出し
# =============================================================
def update_spreadsheet():
  if not collected_vessels:
    print("受信データが0件のため更新をスキップします。")
    return

  # サービスアカウント情報でGoogle認証
  sa_info = json.loads(GCP_SA_KEY_JSON)
  scopes = ["https://www.googleapis.com/auth/spreadsheets"]
  credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
  gc = gspread.authorize(credentials)

  sh = gc.open(SPREADSHEET_NAME)
  worksheet = sh.worksheet(WORKSHEET_NAME)

  # シートに書き込む行データを作成
  rows = [["MMSI", "ShipName", "Latitude", "Longitude", "Speed(kt)"]]
  for mmsi, info in collected_vessels.items():
    rows.append([mmsi, info["ShipName"], info["Lat"], info["Lon"], info["Speed"]])

  # スプレッドシートをクリアして全件上書き
  worksheet.clear()
  worksheet.update("A1", rows)
  print(f"■ スプレッドシートを更新しました（{len(collected_vessels)}隻）。")


# =============================================================
# 4. メイン実行
# =============================================================
if __name__ == "__main__":
  # 非同期でAISを受信
  asyncio.run(collect_ais())
  # スプレッドシートを更新
  update_spreadsheet()
