import os
import json
import math
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

# --- 設定 ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
CURRENT_YEAR = datetime.now(timezone.utc).year
LATEST_FILE = os.path.join("data", f"latest_{CURRENT_YEAR}.json")

# 答え合わせに使う「主要港の座標マスタ」
# 緯度(lat), 経度(lng), 判定半径(海里: 1海里は約1.85km)
PORT_GEO_MASTER = [
    {"unloc": "CN SHA", "name": "Shanghai", "country": "CN", "lat": 31.00, "lng": 122.00, "radius_nm": 45}, # 洋山・長江河口を含む広域
    {"unloc": "CN NGB", "name": "Ningbo", "country": "CN", "lat": 29.85, "lng": 122.05, "radius_nm": 30},
    {"unloc": "SG SIN", "name": "Singapore", "country": "SG", "lat": 1.25, "lng": 103.80, "radius_nm": 20},
    {"unloc": "AU PHE", "name": "Port Hedland", "country": "AU", "lat": -20.30, "lng": 118.57, "radius_nm": 15},
    {"unloc": "US LAX", "name": "Los Angeles", "country": "US", "lat": 33.73, "lng": -118.23, "radius_nm": 20},
    {"unloc": "JP YOK", "name": "Yokohama", "country": "JP", "lat": 35.45, "lng": 139.65, "radius_nm": 10},
    # 必要に応じてここに港の座標を追加していくことで学習対象の港が増えます
]

def get_spreadsheet_client():
    if not GCP_SA_KEY: return None
    sa_info = json.loads(GCP_SA_KEY)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(sa_info, scopes=scopes))

def get_distance_nm(lat1, lon1, lat2, lon2):
    # 2点間の距離を海里(Nautical Miles)で計算（ハバサイン公式）
    R = 3440.065 # 地球の半径(海里)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def main():
    if not os.path.exists(LATEST_FILE):
        print("最新データが見つかりません。")
        return

    client = get_spreadsheet_client()
    if not client:
        print("Google認証情報がありません。")
        return

    # スプレッドシートの辞書を取得
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Port_Dictionary")
    existing_records = sheet.col_values(1) # A列（変換元）をすべて取得
    existing_keys = {str(k).strip().upper() for k in existing_records if k} # 重複判定用セット

    with open(LATEST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        live_ships = data.get("latest", [])

    new_rows = []
    
    for ship in live_ships:
        lat = ship.get("lat")
        lng = ship.get("lng")
        speed = ship.get("speed", 100)
        dest_str = str(ship.get("destination", "")).strip().upper()

        # 目的地が空欄、または1.0ノット以上（航行中）の船は除外
        if not dest_str or dest_str in ("-", "", "None") or speed > 1.0:
            continue

        # 既に辞書に登録済みの文字列ならスキップ
        if dest_str in existing_keys:
            continue

        # 停泊中の位置が、登録された主要港の範囲内にあるかチェック
        for port in PORT_GEO_MASTER:
            dist = get_distance_nm(lat, lng, port["lat"], port["lng"])
            if dist <= port["radius_nm"]:
                # マッチした！新しい表記を学習してリストに追加
                new_row = [
                    dest_str,                # A: 変換元(AIS表記)
                    port["country"],         # B: 国コード
                    port["unloc"],           # C: UN/LOCODE
                    port["name"],            # D: 寄港地名(標準)
                    "自動学習(位置情報より)"   # E: 備考
                ]
                new_rows.append(new_row)
                existing_keys.add(dest_str) # 同一処理内での重複を防ぐ
                print(f"自動学習: '{dest_str}' は {port['name']} (距離: {round(dist, 1)}海里) と判定しました。")
                break # 1つの港にマッチしたら次の船へ

    # スプレッドシートに一括追記
    if new_rows:
        sheet.append_rows(new_rows)
        print(f"{len(new_rows)} 件の新しい辞書データをスプレッドシートに追加しました。")
    else:
        print("新しく学習する辞書データはありませんでした。")

if __name__ == "__main__":
    main()
