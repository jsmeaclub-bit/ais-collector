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
    {"unloc": "CN SHA", "name": "Shanghai", "country": "CN", "lat": 31.00, "lng": 122.00, "radius_nm": 45},
    {"unloc": "CN NGB", "name": "Ningbo", "country": "CN", "lat": 29.85, "lng": 122.05, "radius_nm": 30},
    {"unloc": "SG SIN", "name": "Singapore", "country": "SG", "lat": 1.25, "lng": 103.80, "radius_nm": 20},
    {"unloc": "AU PHE", "name": "Port Hedland", "country": "AU", "lat": -20.30, "lng": 118.57, "radius_nm": 15},
    {"unloc": "US LAX", "name": "Los Angeles", "country": "US", "lat": 33.73, "lng": -118.23, "radius_nm": 20},
    {"unloc": "JP YOK", "name": "Yokohama", "country": "JP", "lat": 35.45, "lng": 139.65, "radius_nm": 10},
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
    existing_records = sheet.col_values(1)
    existing_keys = {str(k).strip().upper() for k in existing_records if k}

    with open(LATEST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        live_ships = data.get("latest", [])

    new_rows = []
    
    for ship in live_ships:
        lat = ship.get("lat")
        lng = ship.get("lng")
        speed = ship.get("speed", 100)
        dest_str = str(ship.get("destination", "")).strip().upper()

        if not dest_str or dest_str in ("-", "", "NONE") or speed > 1.0:
            continue

        if dest_str in existing_keys:
            continue

        for port in PORT_GEO_MASTER:
            dist = get_distance_nm(lat, lng, port["lat"], port["lng"])
            if dist <= port["radius_nm"]:
                new_row = [
                    dest_str,
                    port["country"],
                    port["unloc"],
                    port["name"],
                    "自動学習(位置情報より)",
                    port["lat"],
                    port["lng"]
                ]
                new_rows.append(new_row)
                existing_keys.add(dest_str)
                print(f"自動学習: '{dest_str}' は {port['name']} (距離: {round(dist, 1)}海里) と判定しました。")
                break

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"{len(new_rows)} 件の新しい辞書データをスプレッドシートに追加しました。")
    else:
        print("新しく学習する辞書データはありませんでした。")

if __name__ == "__main__":
    main()
