import os
import json
import math
import requests
import searoute as sr

GAS_URL = os.environ.get("GAS_WEBHOOK_URL")

if not GAS_URL:
    raise ValueError("エラー: 環境変数 GAS_WEBHOOK_URL が設定されていません。")

# ▼ 1. 来島海峡・大下瀬戸・斎灘の精密安全ルーティング（陸地貫通の完全防止） ▼
def resolve_safe_sea_route(lat1, lon1, lat2, lon2):
    # 判定エリアを少し広げ、大下瀬戸へ斜めに進入する船も確実にキャッチ
    if (33.95 <= lat1 <= 34.40 and 33.95 <= lat2 <= 34.40 and
        132.70 <= lon1 <= 133.35 and 132.70 <= lon2 <= 133.35):
        
        WP_SAINADA = [34.085, 132.880]
        WP_OGE     = [34.165, 132.960]
        WP_BRIDGE  = [34.120, 133.012]
        WP_EAST_IN = [34.095, 133.045]
        WP_HUCHI   = [34.115, 133.100]

        is_west1 = lon1 < 132.96
        is_west2 = lon2 < 132.96
        is_east1 = lon1 > 132.98
        is_east2 = lon2 > 132.98

        # 西側（斎灘・安芸灘）と東側（燧灘）を大きく跨ぐ場合
        if is_west1 and is_east2:
            return [[lat1, lon1], WP_SAINADA, WP_OGE, WP_BRIDGE, WP_EAST_IN, WP_HUCHI, [lat2, lon2]]
        elif is_east1 and is_west2:
            return [[lat1, lon1], WP_HUCHI, WP_EAST_IN, WP_BRIDGE, WP_OGE, WP_SAINADA, [lat2, lon2]]
        
        # 同じ西側エリア内での移動（大下瀬戸周辺のみなど）でも島を避ける
        if is_west1 and is_west2 and abs(lon1 - lon2) > 0.05:
            mid_lat = (lat1 + lat2) / 2
            mid_lon = (lon1 + lon2) / 2
            # 島に刺さらないよう少し北側の海峡中央へ補正
            return [[lat1, lon1], [round(mid_lat + 0.015, 5), round(mid_lon, 5)], [lat2, lon2]]

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

def chaikin_smooth(points, iterations=4):
    if not points or len(points) < 3:
        if len(points) == 2:
            p0, p1 = points[0], points[1]
            points = [p0, [(p0[0]+p1[0])/2, (p0[1]+p1[1])/2], p1]
        else:
            return points
    current = points
    for _ in range(iterations):
        next_pts = [current[0]]
        for i in range(len(current) - 1):
            p0 = current[i]; p1 = current[i + 1]
            next_pts.append([round(0.75*p0[0] + 0.25*p1[0], 5), round(0.75*p0[1] + 0.25*p1[1], 5)])
            next_pts.append([round(0.25*p0[0] + 0.75*p1[0], 5), round(0.25*p0[1] + 0.75*p1[1], 5)])
        next_pts.append(current[-1])
        current = next_pts
    return current

def main():
    os.makedirs("data", exist_ok=True)
    target_url = f"{GAS_URL}?api=routes"
    res = requests.Session().get(target_url, allow_redirects=True, timeout=60)
    if res.status_code != 200: raise ValueError(f"HTTPエラー {res.status_code}")

    valid_routes = []
    for row in res.json():
        try:
            s_lat, s_lng = row.get("Start_Lat"), row.get("Start_Lng")
            e_lat, e_lng = row.get("End_Lat"), row.get("End_Lng")
            if None in (s_lat, s_lng, e_lat, e_lng): continue
            lat1, lon1, lat2, lon2 = float(s_lat), float(s_lng), float(e_lat), float(e_lng)
            if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180 and -90 <= lat2 <= 90 and -180 <= lon2 <= 180): continue
            valid_routes.append({
                "cat": str(row.get("Category") or "日本支配船"),
                "count": int(row.get("Count") or 1),
                "p1": [lat1, lon1], "p2": [lat2, lon2]
            })
        except: continue

    total_items = len(valid_routes)
    processed_routes = []
    for idx, item in enumerate(valid_routes):
        raw_coords = calculate_sea_route(item["p1"][0], item["p1"][1], item["p2"][0], item["p2"][1])
        smooth_coords = chaikin_smooth(raw_coords, iterations=4)
        time_ratio = (idx + 1) / total_items if total_items > 1 else 1.0
        base_weight = min(16.0, max(3.2, 1.8 + math.log1p(item["count"]) * 2.3))

        processed_routes.append({
            "cat": item["cat"], "count": item["count"], "coords": smooth_coords,
            "glow_op": round(0.15 + 0.75 * math.pow(time_ratio, 1.3), 3),
            "core_op": round(0.25 + 0.75 * math.pow(time_ratio, 1.1), 3),
            "weight": round(base_weight * (0.7 + 0.3 * time_ratio), 2),
            "status": "直近の航行実績" if time_ratio > 0.8 else "過去の航行実績"
        })

    with open("data/routes_2026.json", "w", encoding="utf-8") as f:
        json.dump(processed_routes, f, ensure_ascii=False, separators=(',', ':'))

if __name__ == "__main__":
    main()
