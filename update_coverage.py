import os
import sys
import json
import time
import math
import asyncio
import subprocess
import websockets
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

# 必要パッケージの確認・自動インストール
for pkg in ["shapely", "websockets"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

COVERAGE_FILE = os.path.join(DATA_DIR, "coverage_2026.json")
POINTS_CACHE_FILE = os.path.join(DATA_DIR, "coverage_points.json")

# 受信時間（環境変数から取得、デフォルトは5分＝300秒）
LISTEN_DURATION = int(os.environ.get("COVERAGE_LISTEN_SECONDS", 300))

# 沿岸50海里のバッファ半径（1海里=1.852km、緯度1度≒111km -> 50海里 ≒ 0.83度）
BUFFER_DEGREE = 0.83
GRID_RESOLUTION = 0.1  # 重複排除用グリッド（約10km刻み）


async def collect_global_ais_points(duration_sec):
    """全世界の全船舶から位置データを受信"""
    if not AISSTREAM_API_KEY:
        print("[ERROR] AISSTREAM_API_KEY が設定されていません。")
        return []

    print(f"[INFO] 全世界のAISデータを受信開始（受信時間: {duration_sec} 秒）...")
    url = "wss://stream.aisstream.io/v0/stream"
    subscribe_msg = {
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport"]
    }

    new_points = set()
    start_time = time.time()
    msg_count = 0

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=10**7) as ws:
            await ws.send(json.dumps(subscribe_msg))
            while time.time() - start_time < duration_sec:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(msg)
                    msg_count += 1

                    meta = data.get("MetaData", {})
                    lat = meta.get("latitude")
                    lng = meta.get("longitude")

                    if lat is not None and lng is not None:
                        # 異常値の排除
                        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
                            # 0.1度単位でグリッド化して一意化
                            grid_pt = (round(lat / GRID_RESOLUTION) * GRID_RESOLUTION,
                                       round(lng / GRID_RESOLUTION) * GRID_RESOLUTION)
                            new_points.add((round(grid_pt[0], 2), round(grid_pt[1], 2)))

                    if msg_count % 1000 == 0:
                        elapsed = int(time.time() - start_time)
                        print(f"       受信中: {msg_count} メッセージ | キャプチャ地点数: {len(new_points)} ({elapsed}/{duration_sec}秒)")

                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue

    except Exception as e:
        print(f"[ERROR] WebSocket接続エラー: {e}")

    print(f"[INFO] 受信完了: 合計 {msg_count} 件のAIS信号から {len(new_points)} の新地点を抽出しました。")
    return list(new_points)


def load_cached_points():
    """過去の累積受信ポイントを読み込み"""
    if os.path.exists(POINTS_CACHE_FILE):
        try:
            with open(POINTS_CACHE_FILE, "r", encoding="utf-8") as f:
                pts = json.load(f)
                return set((round(p[0], 2), round(p[1], 2)) for p in pts)
        except Exception:
            pass
    return set()


def build_and_save_coverage_geojson(all_points):
    """全ポイントから50海里バッファを生成し、GeoJSONとして保存"""
    print(f"[INFO] 合計 {len(all_points)} 地点から地上AIS受信圏ポリゴンを生成中...")
    if not all_points:
        return

    # 各点から円バッファを生成（解像度は計算負荷と精度のバランスで8分割）
    circles = [Point(lng, lat).buffer(BUFFER_DEGREE, resolution=8) for lat, lng in all_points]

    # ポリゴンを1つに結合
    print("       ポリゴン結合（unary_union）を実行中...")
    merged_geom = unary_union(circles)

    # Web描画用に頂点数をリダクション（ファイルサイズを圧縮）
    simplified_geom = merged_geom.simplify(0.03, preserve_topology=True)

    geojson_output = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "description": "Ground AIS Reception Coverage (Approx. 50NM)",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                    "points_count": len(all_points)
                },
                "geometry": mapping(simplified_geom)
            }
        ]
    }

    # GeoJSON の書き出し
    with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(geojson_output, f, ensure_ascii=False)
    print(f"[SUCCESS] {COVERAGE_FILE} を更新しました（サイズ: {os.path.getsize(COVERAGE_FILE) / 1024:.1f} KB）")

    # ポイントキャッシュの更新
    with open(POINTS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(all_points), f, ensure_ascii=False)
    print(f"[SUCCESS] {POINTS_CACHE_FILE} を更新しました。")


def main():
    cached_points = load_cached_points()
    print(f"[INFO] 過去の累積受信地点数: {len(cached_points)}")

    new_points = asyncio.run(collect_global_ais_points(LISTEN_DURATION))
    
    # 過去の実績と新規実績を統合
    all_points = cached_points.union(new_points)
    build_and_save_coverage_geojson(all_points)


if __name__ == "__main__":
    main()
