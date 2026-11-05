import os
import json
import requests
import searoute as sr

# 登録済みシークレット（GAS_WEBHOOK_URL）からURLを取得
GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

def calculate_sea_route(lat1, lon1, lat2, lon2):
    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route:
            return [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
    except Exception:
        pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

def main():
    os.makedirs("data", exist_ok=True)
    
    # GASから 2026年の航路データを取得
    target_url = f"{GAS_URL}?api=routes"
    print(f"Fetching routes from: {target_url}")
    
    res = requests.get(target_url, allow_redirects=True, timeout=30)
    res.raise_for_status()
    raw_routes = res.json()
    
    processed_routes = []
    for row in raw_routes:
        lat1, lon1 = float(row["Start_Lat"]), float(row["Start_Lng"])
        lat2, lon2 = float(row["End_Lat"]), float(row["End_Lng"])
        count = int(row.get("Count", 1))
        cat = str(row.get("Category", "日本支配船"))

        coords = calculate_sea_route(lat1, lon1, lat2, lon2)
        processed_routes.append({
            "cat": cat,
            "count": count,
            "coords": coords
        })

    output_path = "data/routes_2026.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))
        
    print(f"Successfully processed {len(processed_routes)} routes into {output_path}.")

if __name__ == "__main__":
    main()
