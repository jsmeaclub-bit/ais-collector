import os
import json
import time
import asyncio
import websockets
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
POINTS_FILE = os.path.join(DATA_DIR, "coverage_points.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage_2026.json")

# 収集時間（手動・自動実行ともに5分間）
LISTEN_DURATION = 300  

def get_under_covered_bboxes(points, grid_size=30, max_boxes=12):
    """
    過去の受信ポイント（coverage_points.json）をグリッド分析し、
    データが最も少ない「地図の過疎エリア」を特定して重点BoundingBoxを生成する。
    """
    grid_counts = {}
    lat_steps = int(180 / grid_size)
    lng_steps = int(360 / grid_size)
    
    # 地球全体をグリッド分割（初期値0）
    for i in range(lat_steps):
        for j in range(lng_steps):
            grid_counts[(i, j)] = 0
            
    # 既存ポイントの分布密度をカウント
    for pt in points:
        lat, lng = pt[0], pt[1]
        lat = max(-90, min(89.9999, lat))
        lng = max(-180, min(179.9999, lng))
        
        i = int((lat + 90) / grid_size)
        j = int((lng + 180) / grid_size)
        grid_counts[(i, j)] += 1
        
    # ポイントが少ない（過疎化している）順にソート
    sorted_grids = sorted(grid_counts.items(), key=lambda x: x[1])
    
    bboxes = []
    # データが少ない上位エリアを抽出
    for (i, j), count in sorted_grids[:max_boxes]:
        min_lat = -90 + i * grid_size
        max_lat = min_lat + grid_size
        min_lng = -180 + j * grid_size
        max_lng = min_lng + grid_size
        bboxes.append([[min_lat, min_lng], [max_lat, max_lng]])
        
    # 船が少なく電波が拾いにくい大洋を固定で追加保護（南太平洋、インド洋など）
    bboxes.append([[-60, 40], [-20, 100]])   # インド洋南部
    bboxes.append([[-60, -160], [-20, -80]]) # 南太平洋
    bboxes.append([[-40, -40], [10, 20]])    # 大西洋南部・アフリカ西岸
    
    return bboxes

async def collect_coverage_data(bboxes):
    collected_points = []
    if not AISSTREAM_API_KEY:
        print("[ERROR] AISSTREAM_API_KEY is not set.")
        return collected_points
        
    url = "wss://stream.aisstream.io/v0/stream"
    sub_msg = {
        "APIKey": AISSTREAM_API_KEY, 
        "BoundingBoxes": bboxes, 
        "FilterMessageTypes": ["PositionReport"]
    }

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps(sub_msg))
            print(f"Connected. Focusing on under-covered areas. Listening for {LISTEN_DURATION} seconds...")
            start = time.time()
            while time.time() - start < LISTEN_DURATION:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    
                    if data.get("MessageType") == "PositionReport":
                        pos = data.get("Message", {}).get("PositionReport", {})
                        lat = pos.get("Latitude")
                        lng = pos.get("Longitude")
                        
                        # 有効な座標のみ抽出
                        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
                            # 丸めて保存（小数点2桁 ≒ 約1km四方の解像度。ファイル肥大化を防止）
                            collected_points.append([round(lat, 2), round(lng, 2)])
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    continue
    except Exception as e: 
        print(f"[ERROR] WebSocket connection failed: {e}")
        
    return collected_points

def generate_coverage_polygon(points):
    """
    収集したポイント群から半径50海里の円を描き、それらを融合して滑らかなGeoJSONポリゴンを生成する
    """
    print(f"Generating polygon from {len(points)} unique points...")
    # 50海里 ≒ 約90km ≒ 0.83度
    BUFFER_DEG = 0.83
    
    circles = [Point(lng, lat).buffer(BUFFER_DEG) for lat, lng in points]
    merged_polygon = unary_union(circles)
    
    geojson_feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(merged_polygon),
                "properties": {
                    "name": "Global AIS Coverage",
                    "buffer_nm": 50
                }
            }
        ]
    }
    return geojson_feature

def main():
    # 1. 過去に構築したポイントデータの読み込み
    existing_points = []
    if os.path.exists(POINTS_FILE):
        try:
            with open(POINTS_FILE, "r", encoding="utf-8") as f:
                existing_points = json.load(f)
        except Exception as e:
            print(f"Error loading {POINTS_FILE}: {e}")
            
    print(f"Loaded {len(existing_points)} existing coverage points.")

    # 2. 地図の少ないエリアを特定して重点 BoundingBox を作成
    bboxes = get_under_covered_bboxes(existing_points, grid_size=30, max_boxes=12)
    print(f"Target Bounding Boxes for under-covered areas generated.")

    # 3. 重点エリアのAISストリームを収集
    new_points = asyncio.run(collect_coverage_data(bboxes))
    print(f"Collected {len(new_points)} new points.")

    # 4. 重複を排除してマージ
    unique_points = set(tuple(pt) for pt in existing_points)
    for pt in new_points:
        unique_points.add(tuple(pt))
        
    merged_points = [list(pt) for pt in unique_points]
    print(f"Total unique coverage points: {len(merged_points)}")

    # 5. ポイントデータ（学習記憶）の保存
    with open(POINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged_points, f, ensure_ascii=False)

    # 6. ポリゴンを描画して緑色マップ (GeoJSON) を更新
    if merged_points:
        coverage_geojson = generate_coverage_polygon(merged_points)
        with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(coverage_geojson, f, ensure_ascii=False)
        print("Coverage map successfully updated.")
    else:
        print("No points to generate coverage map.")

if __name__ == "__main__":
    main()
