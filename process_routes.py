import os
import json
import math
import requests
import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

# ▼ 1. 来島海峡・大下瀬戸・斎灘の精密安全ルーティング（直線を完全排除） ▼
def resolve_safe_sea_route(lat1, lon1, lat2, lon2):
    # 瀬戸内海西部〜来島海峡周辺エリア
    if (34.00 <= lat1 <= 34.35 and 34.00 <= lat2 <= 34.35 and
        132.75 <= lon1 <= 133.30 and 132.75 <= lon2 <= 133.30):
        
        # 水路中央の滑らかな通過点
        WP_SAINADA = [34.085, 132.880] # 斎灘
        WP_OGE     = [34.165, 132.960] # 大下瀬戸西口
        WP_BRIDGE  = [34.120, 133.012] # 来島海峡中水道（大橋下）
        WP_EAST_IN = [34.095, 133.045] # 東口
        WP_HUCHI   = [34.115, 133.100] # 燧灘

        # 西側（斎灘方面）と東側（燧灘・今治方面）を跨ぐ場合
        if lon1 < 132.96 and lon2 > 132.98:
            return [[lat1, lon1], WP_SAINADA, WP_OGE, WP_BRIDGE, WP_EAST_IN, WP_HUCHI, [[lat2, lon2]][0]]
        elif lon1 > 132.98 and lon2 < 132.96:
            return [[lat1, lon1], WP_HUCHI, WP_EAST_IN, WP_BRIDGE, WP_OGE, WP_SAINADA, [[lat2, lon2]][0]]
        
        # 大下瀬戸周辺での短い区間移動でも直線化を防ぐ中間点
        mid_lat = (lat1 + lat2) / 2
        mid_lon = (lon1 + lon2) / 2
        # 半島に寄らないよう少し沖合へ補正
        return [[lat1, lon1], [round(mid_lat + 0.008, 5), round(mid_lon, 5)], [lat2, lon2]]

    return [[lat1, lon1], [lat2, lon2]]

def calculate_sea_route(lat1, lon1, lat2, lon2):
    safe_pts = resolve_safe_sea_route(lat1, lon1, lat2, lon2)
    if len(safe_pts) > 2:
        return safe_pts

    try:
        route = sr.searoute([lon1, lat1], [lon2, lat2], units="km")
        if route and "geometry" in route and "coordinates" in route["geometry"]:
            coords = [[round(pt[1], 5), round(pt[0], 5)] for pt in route["geometry"]["coordinates"]]
            if len(coords) >= 2:
                return coords
    except Exception:
        pass
    return [[round(lat1, 5), round(lon1, 5)], [round(lat2, 5), round(lon2, 5)]]

# ▼ 2. カドを消して美しい円弧にする Chaikin 曲線平滑化（反復4回） ▼
def chaikin_smooth(points, iterations=4):
    if not points or len(points) < 3:
        # 2点の場合は中間点を挿入して曲線化可能にする
        if len(points) == 2:
            p0, p1 = points[0], points[1]
            points = [p0, [(p0[0]+p1[0])/2, (p0[1]+p1[1])/2], p1]
        else:
            return points

    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0 = current[i]
            p1 = current[i + 1]
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
    valid_routes = []
    for row in raw_routes:
        try:
            s_lat, s_lng = row.get("Start_Lat"), row.get("Start_Lng")
            e_lat, e_lng = row.get("End_Lat"), row.get("End_Lng")
            if None in (s_lat, s_lng, e_lat, e_lng): continue

            lat1, lon1, lat2, lon2 = float(s_lat), float(s_lng), float(e_lat), float(e_lng)
            if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180):
                continue

            valid_routes.append({
                "cat": str(row.get("Category") or "日本支配船"),
                "count": int(row.get("Count") or 1),
                "p1": [lat1, lon1], "p2": [lat2, lon2]
            })
        except Exception:
            continue

    total_items = len(valid_routes)
    processed_routes = []

    for idx, item in enumerate(valid_routes):
        lat1, lon1 = item["p1"]
        lat2, lon2 = item["p2"]
        raw_coords = calculate_sea_route(lat1, lon1, lat2, lon2)
        smooth_coords = chaikin_smooth(raw_coords, iterations=4)

        time_ratio = (idx + 1) / total_items if total_items > 1 else 1.0
        glow_opacity = round(0.15 + 0.75 * math.pow(time_ratio, 1.3), 3)
        core_opacity = round(0.25 + 0.75 * math.pow(time_ratio, 1.1), 3)

        base_weight = min(16.0, max(3.2, 1.8 + math.log1p(item["count"]) * 2.3))
        seg_weight = round(base_weight * (0.7 + 0.3 * time_ratio), 2)

        processed_routes.append({
            "cat": item["cat"],
            "count": item["count"],
            "coords": smooth_coords,
            "glow_op": glow_opacity,
            "core_op": core_opacity,
            "weight": seg_weight,
            "status": "直近の航行実績" if time_ratio > 0.8 else "過去の航行実績"
        })

    output_path = "data/routes_2026.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))
        
    print(f"前処理完了: {len(processed_routes)} 件の航路を出力しました。")

if __name__ == "__main__":
    main()
