import os
import json
import requests
import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

def calculate_sea_route(lat1, lon1, lat2, lon2):
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            return [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
    except Exception:
        pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

def main():
    os.makedirs("data", exist_ok=True)
    
    target_url = f"{GAS_URL}?api=routes"
    print(f"Fetching routes from: {target_url}")
    
    session = requests.Session()
    res = session.get(target_url, allow_redirects=True, timeout=60)
    
    if res.status_code != 200:
        raise ValueError(f"HTTPエラー: status {res.status_code}")

    raw_routes = res.json()
    print(f"取得したレコード総数: {len(raw_routes)}")
    
    processed_routes = []
    skipped_count = 0

    for row in raw_routes:
        try:
            # ▼ 空欄・欠損値（None）のスキップ判定 ▼
            s_lat = row.get("Start_Lat")
            s_lng = row.get("Start_Lng")
            e_lat = row.get("End_Lat")
            e_lng = row.get("End_Lng")

            if s_lat is None or s_lng is None or e_lat is None or e_lng is None:
                skipped_count += 1
                continue

            lat1, lon1 = float(s_lat), float(s_lng)
            lat2, lon2 = float(e_lat), float(e_lng)

            # 緯度経度の異常値（範囲外）チェック
            if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180):
                skipped_count += 1
                continue

            count = int(row.get("Count") or 1)
            cat = str(row.get("Category") or "日本支配船")

            coords = calculate_sea_route(lat1, lon1, lat2, lon2)
            processed_routes.append({
                "cat": cat,
                "count": count,
                "coords": coords
            })
        except Exception:
            skipped_count += 1
            continue

    output_path = "data/routes_2026.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))
        
    print(f"処理完了: {len(processed_routes)} 件の航路を出力 (空欄・無効スキップ: {skipped_count} 件)")

if __name__ == "__main__":
    main()
