import os
import json
import math
import requests
import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

# ▼ 1. 来島海峡などの狭水路・難所安全迂回ルーティング ▼
def resolve_safe_sea_route(lat1, lon1, lat2, lon2):
    # 来島海峡・大下瀬戸エリアの陸地貫通を防止
    if (34.00 <= lat1 <= 34.30 and 34.00 <= lat2 <= 34.30 and
        132.80 <= lon1 <= 133.25 and 132.80 <= lon2 <= 133.25):
        
        wp_list = [
            [34.155, 132.935],
            [34.135, 132.975],
            [34.118, 133.010],
            [34.096, 133.045],
            [34.112, 133.090]
        ]
        is_west1 = lon1 < 132.98
        is_west2 = lon2 < 132.98
        is_east1 = lon1 > 133.04
        is_east2 = lon2 > 133.04

        if is_west1 and is_east2:
            return [[lat1, lon1]] + wp_list + [[lat2, lon2]]
        elif is_east1 and is_west2:
            return [[lat1, lon1]] + list(reversed(wp_list)) + [[lat2, lon2]]

    return [[lat1, lon1], [lat2, lon2]]

# ▼ 2. 陸地を自動回避する海上ルート計算 ▼
def calculate_sea_route(lat1, lon1, lat2, lon2):
    # 局所的な狭水路の事前判定
    safe_pts = resolve_safe_sea_route(lat1, lon1, lat2, lon2)
    if len(safe_pts) > 2:
        return safe_pts

    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            return [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
    except Exception:
        pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

# ▼ 3. カドを取り滑らかなカーブを生成する Chaikin 曲線平滑化 ▼
def chaikin_smooth(points, iterations=3):
    if not points or len(points) < 3:
        return points
    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0 = current[i]
            p1 = current[i + 1]
            # 25% と 75% の内分点でカドを丸める（陸地に飛び出さない安全カーブ）
            next_pts.append([
                round(0.75 * p0[0] + 0.25 * p1[0], 5),
                round(0.75 * p0[1] + 0.25 * p1[1], 5)
            ])
            next_pts.append([
                round(0.25 * p0[0] + 0.75 * p1[0], 5),
                round(0.25 * p0[1] + 0.75 * p1[1], 5)
            ])
        next_pts.append(current[-1])
        current = next_pts
    return current

def main():
    os.makedirs("data", exist_ok=True)
    
    target_url = f"{GAS_URL}?api=routes"
    print(f"Fetching routes from: {target_url}")
    
    session = requests.Session()
    res = session.get(target_url, allow_redirects=True, timeout=60)
    
    if res.status_code != 200:
        raise ValueError(f"HTTPエラー: status {res.status_code}")

    raw_routes = res.json()
    print(f"取得した航路レコード総数: {len(raw_routes)}")
    
    valid_routes = []
    for row in raw_routes:
        try:
            s_lat = row.get("Start_Lat")
            s_lng = row.get("Start_Lng")
            e_lat = row.get("End_Lat")
            e_lng = row.get("End_Lng")

            if s_lat is None or s_lng is None or e_lat is None or e_lng is None:
                continue

            lat1, lon1 = float(s_lat), float(s_lng)
            lat2, lon2 = float(e_lat), float(e_lng)

            if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180):
                continue

            count = int(row.get("Count") or 1)
            cat = str(row.get("Category") or "日本支配船")

            valid_routes.append({
                "cat": cat,
                "count": count,
                "p1": [lat1, lon1],
                "p2": [lat2, lon2]
            })
        except Exception:
            continue

    total_items = len(valid_routes)
    processed_routes = []

    for idx, item in enumerate(valid_routes):
        lat1, lon1 = item["p1"]
        lat2, lon2 = item["p2"]
        count = item["count"]
        cat = item["cat"]

        # 海上ルート計算
        raw_coords = calculate_sea_route(lat1, lon1, lat2, lon2)
        
        # 曲線平滑化（折れ線のカドを消して美しいカーブへ）
        smooth_coords = chaikin_smooth(raw_coords, iterations=3)

        # 計測期間全体における時間進行比率（0: 過去 ➔ 1: 最新）
        time_ratio = (idx + 1) / total_items if total_items > 1 else 1.0

        # グラデーション・スタイルの事前計算
        glow_opacity = round(0.15 + 0.75 * math.pow(time_ratio, 1.3), 3)
        core_opacity = round(0.25 + 0.75 * math.pow(time_ratio, 1.1), 3)

        base_weight = min(16.0, max(3.2, 1.8 + math.log1p(count) * 2.3))
        seg_weight = round(base_weight * (0.7 + 0.3 * time_ratio), 2)

        processed_routes.append({
            "cat": cat,
            "count": count,
            "coords": smooth_coords,
            "glow_op": glow_opacity,
            "core_op": core_opacity,
            "weight": seg_weight,
            "status": "直近の航行実績" if time_ratio > 0.8 else "過去の航行実績"
        })

    output_path = "data/routes_2026.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))
        
    print(f"前処理完了: {len(processed_routes)} 件の航路（曲線・グラデーション事前計算済み）を {output_path} に出力しました。")

if __name__ == "__main__":
    main()
