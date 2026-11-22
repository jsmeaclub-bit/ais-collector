import os
import io
import json
import time
import math
import heapq
import hashlib
import statistics
import asyncio
import subprocess
import sys
import websockets
import gzip
import csv
from collections import defaultdict
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials

try:
    import searoute as sr
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute", "shapely"])
    import searoute as sr
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union

try:
    from global_land_mask import globe
    import numpy as np
    HAS_LAND_MASK = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "global-land-mask", "numpy"])
        from global_land_mask import globe
        import numpy as np
        HAS_LAND_MASK = True
    except Exception:
        HAS_LAND_MASK = False

try:
    import requests
    from PIL import Image
    HAS_TILE_VISION = True
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "Pillow"])
        import requests
        from PIL import Image
        HAS_TILE_VISION = True
    except Exception:
        HAS_TILE_VISION = False

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

CURRENT_YEAR = datetime.now(timezone.utc).year
LATEST_FILE = os.path.join(DATA_DIR, f"latest_{CURRENT_YEAR}.json")
ROUTES_FILE = os.path.join(DATA_DIR, f"routes_{CURRENT_YEAR}.json")
ROUTE_CACHE_FILE = os.path.join(DATA_DIR, "route_cache.json.gz")
COVERAGE_POINTS_FILE = os.path.join(DATA_DIR, "coverage_points.json")
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage.json")
EXPIRATION_SECONDS = 180 * 24 * 3600  

MAX_GENERAL_SPEED_KNOTS = 26.0
MIN_TIME_HRS_FOR_SPEED  = 1.0  
SPIKE_COS_THETA         = -0.25
SPIKE_LEG_MIN_DEG       = 0.15

TSUGARU_STRAIT     = [41.45, 140.55]
SOYA_STRAIT        = [45.65, 141.95]
SOYA_MAIN_GATE     = [45.72, 142.05]
ANIWA_BAY_GATE     = [46.15, 142.75]
JOHORE_EAST_GATE_N = [1.36, 104.10]  # チャンギ・テコン島北東の海上ゲート
JOHORE_EAST_GATE_S = [1.28, 104.08]  # チャンギ南東のシンガポール海峡ゲート

OFFSHORE_BUFFER_DEG  = 0.12  # 約13kmの離岸バリア（実測・直線補完側のガードレール）
PORT_AREA_RADIUS_DEG = 0.45  # 仕向地エリア半径（約50km圏内なら無理な往復補完をしない）

GLOBAL_SAFE_GRIDS = set()
GLOBAL_FINE_REAL_GRIDS = set()  # 画像認識A*用の高解像度実測グリッド（約1km単位）
JOHORE_REAL_POINTS = []
GLOBAL_MANUAL_ALGO_MAP = {}
GLOBAL_AUTO_ALGO_MAP = {}
GLOBAL_AVOID_NORTHERN = set()
MACRO_RESOLUTION = 2.0

# 高速化用メモリキャッシュ
_LAND_CACHE = {}
_BUFFER_CACHE = {}
_DEEP_INLAND_CACHE = {}
_TILE_CACHE = {}
_PERSISTENT_ROUTE_CACHE = {}
_ROUTE_CACHE_DIRTY = False


def wrap_lng(lng):
    """経度を [-180, 180] に正規化する"""
    if lng > 180.0 or lng < -180.0:
        lng = ((lng + 180.0) % 360.0) - 180.0
    return lng


def wrapped_lng_diff(lng1, lng2):
    """日付変更線（±180度）を考慮した最短経度差（[-180, 180)）"""
    return ((lng2 - lng1 + 180.0) % 360.0) - 180.0


def geo_dist_deg(p1, p2):
    """日付変更線を考慮した2点間の角度距離"""
    return math.hypot(p2[0] - p1[0], wrapped_lng_diff(p1[1], p2[1]))


def route_length_deg(coords):
    """日付変更線を考慮したルート全長"""
    if not coords or len(coords) < 2:
        return 0.0
    return sum(geo_dist_deg(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def unwrap_coords(coords):
    """日付変更線をまたぐ座標列を連続した経度値へ展開する（平滑化・細分化用）"""
    if not coords:
        return []
    unwrapped = [[coords[0][0], coords[0][1]]]
    for i in range(1, len(coords)):
        prev_lng = unwrapped[-1][1]
        dlng = wrapped_lng_diff(wrap_lng(prev_lng), wrap_lng(coords[i][1]))
        unwrapped.append([coords[i][0], prev_lng + dlng])
    return unwrapped


def split_coords_at_dateline(coords):
    """
    連続展開された（または±180度をまたぐ）座標列を、
    経度±180度の境界で正確に分割し、[-180, 180]に収まる複数セグメントのリストを返す。
    """
    if not coords or len(coords) < 2:
        return []

    unwrapped = unwrap_coords(coords)
    parts = []
    curr = [[round(unwrapped[0][0], 5), round(wrap_lng(unwrapped[0][1]), 5)]]

    for i in range(1, len(unwrapped)):
        a_lat, a_lng = unwrapped[i - 1]
        b_lat, b_lng = unwrapped[i]

        k_a = math.floor((a_lng + 180.0) / 360.0)
        k_b = math.floor((b_lng + 180.0) / 360.0)

        if k_a == k_b:
            curr.append([round(b_lat, 5), round(wrap_lng(b_lng), 5)])
        else:
            step = 1 if k_b > k_a else -1
            curr_k = k_a
            while curr_k != k_b:
                boundary_lng = (curr_k + 1) * 360.0 - 180.0 if step > 0 else curr_k * 360.0 - 180.0
                if abs(b_lng - a_lng) > 1e-9:
                    t = (boundary_lng - a_lng) / (b_lng - a_lng)
                    cross_lat = a_lat + (b_lat - a_lat) * t
                else:
                    cross_lat = (a_lat + b_lat) * 0.5

                exit_lng = 180.0 if step > 0 else -180.0
                enter_lng = -180.0 if step > 0 else 180.0

                curr.append([round(cross_lat, 5), exit_lng])
                if len(curr) >= 2:
                    parts.append(curr)
                curr = [[round(cross_lat, 5), enter_lng]]
                curr_k += step

            curr.append([round(b_lat, 5), round(wrap_lng(b_lng), 5)])

    if len(curr) >= 2:
        parts.append(curr)

    return parts


def latlng_to_pixel(lat, lng, zoom):
    """緯度経度をWebメルカトルのピクセル座標(x, y)に変換"""
    lat = max(-85.0511, min(85.0511, lat))
    sin_lat = math.sin(math.radians(lat))
    scale = 256 * (2 ** zoom)
    x = ((lng + 180.0) / 360.0) * scale
    y = (0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * scale
    return x, y


def pixel_to_latlng(px, py, zoom):
    """Webメルカトルのピクセル座標(x, y)を緯度経度に変換"""
    scale = 256 * (2 ** zoom)
    lng = (px / scale) * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * (py / scale)
    lat = math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))
    return round(lat, 5), round(wrap_lng(lng), 5)


def fetch_esri_tile(z, tx, ty):
    """Esri World_Light_Gray_Base タイルを取得（メモリキャッシュ付き）"""
    if not HAS_TILE_VISION:
        return None
    key = (z, tx, ty)
    if key in _TILE_CACHE:
        return _TILE_CACHE[key]
    url = f"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{ty}/{tx}"
    try:
        resp = requests.get(url, timeout=4.0, headers={"User-Agent": "AIS-Monitor/1.0"})
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            _TILE_CACHE[key] = img
            time.sleep(0.03)
            return img
    except Exception:
        pass
    _TILE_CACHE[key] = None
    return None


def get_tile_water_path(p1, p2):
    """
    Esri地図タイルの画像認識（水域抽出）とAIS実測値を組み合わせたA*探索。
    ヨーロッパ内陸の河川、米国東海岸の湾奥・河口港、ボルネオなどの小島・湾内エリアで、
    1kmメッシュの陸地判定では描けない水路沿いの正確な軌跡を自動生成する。
    """
    if not HAS_TILE_VISION:
        return None
    straight_dist = geo_dist_deg(p1, p2)
    if straight_dist > 1.2 or abs(p2[1] - p1[1]) > 10.0:
        return None

    zoom = 12 if straight_dist <= 0.35 else 11
    x1, y1 = latlng_to_pixel(p1[0], p1[1], zoom)
    x2, y2 = latlng_to_pixel(p2[0], p2[1], zoom)

    min_tx = int(min(x1, x2) // 256)
    max_tx = int(max(x1, x2) // 256)
    min_ty = int(min(y1, y2) // 256)
    max_ty = int(max(y1, y2) // 256)

    # タイル数が多すぎる場合は負荷防止のためスキップ
    if (max_tx - min_tx + 1) * (max_ty - min_ty + 1) > 25:
        zoom = 10
        x1, y1 = latlng_to_pixel(p1[0], p1[1], zoom)
        x2, y2 = latlng_to_pixel(p2[0], p2[1], zoom)
        min_tx = int(min(x1, x2) // 256)
        max_tx = int(max(x1, x2) // 256)
        min_ty = int(min(y1, y2) // 256)
        max_ty = int(max(y1, y2) // 256)
        if (max_tx - min_tx + 1) * (max_ty - min_ty + 1) > 25:
            return None

    w_tiles = max_tx - min_tx + 1
    h_tiles = max_ty - min_ty + 1
    canvas = Image.new("RGB", (w_tiles * 256, h_tiles * 256), (242, 242, 242))

    loaded_any = False
    for tx in range(min_tx, max_tx + 1):
        for ty in range(min_ty, max_ty + 1):
            tile_img = fetch_esri_tile(zoom, tx, ty)
            if tile_img is not None:
                canvas.paste(tile_img, ((tx - min_tx) * 256, (ty - min_ty) * 256))
                loaded_any = True

    if not loaded_any:
        return None

    # 4ピクセル単位に縮小して高速A*探索グリッドを構築
    cell_px = 4
    gw = canvas.width // cell_px
    gh = canvas.height // cell_px
    small = canvas.resize((gw, gh), Image.BILINEAR)
    px_data = small.load()

    origin_px = min_tx * 256
    origin_py = min_ty * 256

    sx = max(0, min(gw - 1, int((x1 - origin_px) // cell_px)))
    sy = max(0, min(gh - 1, int((y1 - origin_py) // cell_px)))
    ex = max(0, min(gw - 1, int((x2 - origin_px) // cell_px)))
    ey = max(0, min(gh - 1, int((y2 - origin_py) // cell_px)))

    if math.hypot(sx - ex, sy - ey) <= 1.5:
        return [list(p1), list(p2)]

    # 画像認識：Esri World_Light_Gray_Base の水域（海・川・運河）を抽出
    # 陸地は明るいグレー(R>=228)、水域はやや暗めのクールグレー(150<R<226, B>=R-2)
    water_grid = [[False] * gw for _ in range(gh)]
    label_grid = [[False] * gw for _ in range(gh)]
    for gy in range(gh):
        for gx in range(gw):
            r, g, b = px_data[gx, gy]
            if 145 <= r <= 226 and 145 <= g <= 228 and 145 <= b <= 232 and (b - r) >= -3:
                water_grid[gy][gx] = True
            elif r < 145:
                # 地図上の地名文字・橋・境界線など
                label_grid[gy][gx] = True

    # 水域に隣接する地名ラベル・橋を通行可能（川の途切れ防止）にする
    passable_bridge = [[False] * gw for _ in range(gh)]
    for gy in range(1, gh - 1):
        for gx in range(1, gw - 1):
            if label_grid[gy][gx]:
                if water_grid[gy-1][gx] or water_grid[gy+1][gx] or water_grid[gy][gx-1] or water_grid[gy][gx+1]:
                    passable_bridge[gy][gx] = True

    def heuristic(node):
        return math.hypot(node[0] - ex, node[1] - ey) * 0.9

    start_node = (sx, sy)
    end_node = (ex, ey)
    open_heap = [(heuristic(start_node), 0.0, start_node)]
    came_from = {}
    g_score = {start_node: 0.0}
    closed_set = set()

    iterations = 0
    max_iter = 30000
    while open_heap and iterations < max_iter:
        iterations += 1
        _, curr_g, current = heapq.heappop(open_heap)
        if current in closed_set:
            continue
        closed_set.add(current)

        if current == end_node:
            path = [list(p2)]
            curr_n = current
            while curr_n in came_from:
                curr_n = came_from[curr_n]
                if curr_n != start_node:
                    lat_v, lng_v = pixel_to_latlng(
                        origin_px + (curr_n[0] + 0.5) * cell_px,
                        origin_py + (curr_n[1] + 0.5) * cell_px,
                        zoom
                    )
                    path.append([lat_v, lng_v])
            path.append(list(p1))
            path.reverse()

            # 陸地を大きく突っ切っていないかチェック（A*コストが異常に高ければ棄却）
            if curr_g > math.hypot(sx - ex, sy - ey) * 35.0:
                return None

            smoothed = moving_average_smooth(path, window_size=5)
            # 点を適度に間引いて滑らかな軌跡にする
            if len(smoothed) > 40:
                step = max(1, len(smoothed) // 35)
                reduced = [smoothed[0]] + smoothed[step:-1:step] + [smoothed[-1]]
                return reduced
            return smoothed

        cx, cy = current
        for dx, dy in ((0,1),(1,0),(0,-1),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < gw and 0 <= ny < gh):
                continue
            neighbor = (nx, ny)
            if neighbor in closed_set:
                continue

            step_dist = 1.4142 if (dx != 0 and dy != 0) else 1.0

            if water_grid[ny][nx]:
                # 実測点がある水域はさらに低コストで最優先
                lat_n, lng_n = pixel_to_latlng(origin_px + (nx + 0.5) * cell_px, origin_py + (ny + 0.5) * cell_px, zoom)
                fgk = (int(round(lat_n * 100)), int(round(lng_n * 100)))
                cost = 0.7 if fgk in GLOBAL_FINE_REAL_GRIDS else 1.0
            elif passable_bridge[ny][nx]:
                cost = 2.5
            elif math.hypot(nx - sx, ny - sy) <= 3 or math.hypot(nx - ex, ny - ey) <= 3:
                # 始点・終点の港湾岸壁付近（最大3セル）は進入を許容
                cost = 5.0
            else:
                # 陸地は極めて高いペナルティ（迂回不能な狭小運河のみ突破）
                cost = 120.0

            tentative_g = curr_g + step_dist * cost
            if tentative_g < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                heapq.heappush(open_heap, (tentative_g + heuristic(neighbor), tentative_g, neighbor))

    return None


def get_spreadsheet_client():
    if not GCP_SA_KEY:
        return None
    sa_info = json.loads(GCP_SA_KEY)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(sa_info, scopes=scopes))


def get_config_signature():
    """
    手動設定やロジックバージョンが変更された場合にキャッシュを自動更新するための署名ハッシュ
    """
    sig_obj = {
        "v": 14,
        "manual": sorted(GLOBAL_MANUAL_ALGO_MAP.items()),
        "avoid": sorted(list(GLOBAL_AVOID_NORTHERN)),
        "buffer": OFFSHORE_BUFFER_DEG
    }
    raw = json.dumps(sig_obj, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_persistent_route_cache():
    global _PERSISTENT_ROUTE_CACHE
    _PERSISTENT_ROUTE_CACHE.clear()
    if not os.path.exists(ROUTE_CACHE_FILE):
        return
    try:
        with gzip.open(ROUTE_CACHE_FILE, "rt", encoding="utf-8") as f:
            payload = json.load(f)
            if payload.get("sig") == get_config_signature():
                _PERSISTENT_ROUTE_CACHE = payload.get("routes", {})
                print(f"[INFO] ルートキャッシュを読み込みました: {len(_PERSISTENT_ROUTE_CACHE)} 件", flush=True)
            else:
                print("[INFO] ルール更新を検知したため、ルートキャッシュをリフレッシュします。", flush=True)
    except Exception:
        _PERSISTENT_ROUTE_CACHE = {}


def save_persistent_route_cache():
    global _ROUTE_CACHE_DIRTY
    if not _ROUTE_CACHE_DIRTY:
        return
    try:
        if len(_PERSISTENT_ROUTE_CACHE) > 50000:
            keys_to_keep = list(_PERSISTENT_ROUTE_CACHE.keys())[-50000:]
            trimmed = {k: _PERSISTENT_ROUTE_CACHE[k] for k in keys_to_keep}
        else:
            trimmed = _PERSISTENT_ROUTE_CACHE

        payload = {
            "sig": get_config_signature(),
            "routes": trimmed
        }
        with gzip.open(ROUTE_CACHE_FILE, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        _ROUTE_CACHE_DIRTY = False
    except Exception:
        pass


def sync_and_load_algorithm_map(client, active_points=None):
    global GLOBAL_MANUAL_ALGO_MAP, GLOBAL_AUTO_ALGO_MAP, GLOBAL_AVOID_NORTHERN
    GLOBAL_MANUAL_ALGO_MAP.clear()
    GLOBAL_AUTO_ALGO_MAP.clear()
    GLOBAL_AVOID_NORTHERN.clear()
    
    if not client or not SPREADSHEET_ID:
        return
        
    try:
        wb = client.open_by_key(SPREADSHEET_ID)
        try:
            ws_algo = wb.worksheet("Algorithm_Map")
        except Exception:
            ws_algo = wb.add_worksheet("Algorithm_Map", 5000, 10)
            
        existing_data = ws_algo.get_all_records()
        
        if active_points:
            mesh_counts = defaultdict(int)
            for (lat, lng) in active_points:
                lat_b = math.floor(lat / MACRO_RESOLUTION) * MACRO_RESOLUTION
                lng_b = math.floor(lng / MACRO_RESOLUTION) * MACRO_RESOLUTION
                mesh_counts[(lat_b, lng_b)] += 1
                
            override_algo_dict = {}
            override_avoid_dict = {}
            remarks_dict = {}
            
            for row in existing_data:
                try:
                    k = (float(row.get("Lat_Block", 0)), float(row.get("Lng_Block", 0)))
                    algo_val = str(row.get("Manual_Algorithm", "")).strip()
                    if not algo_val and "Manual_Override" in row:
                        algo_val = str(row.get("Manual_Override", "")).strip()
                    if algo_val:
                        override_algo_dict[k] = algo_val
                        
                    avoid_val = str(row.get("Manual_Avoid_Northern", "")).strip().upper()
                    if avoid_val in ("TRUE", "FALSE"):
                        override_avoid_dict[k] = avoid_val
                        
                    remark_val = str(row.get("備考", "")).strip()
                    if remark_val:
                        remarks_dict[k] = remark_val
                except Exception:
                    pass
                    
            ws_algo.clear()
            headers = ["Lat_Block", "Lng_Block", "Point_Count", "Auto_Algorithm", "Auto_Avoid_Northern", "Manual_Algorithm", "Manual_Avoid_Northern", "備考"]
            rows = [headers]
            
            all_blocks = set(mesh_counts.keys()).union(set(override_algo_dict.keys())).union(set(override_avoid_dict.keys())).union(set(remarks_dict.keys()))
            
            for (lat_b, lng_b) in sorted(all_blocks, key=lambda x: (x[0], x[1])):
                count = mesh_counts.get((lat_b, lng_b), 0)
                auto_algo = "測定点ベース" if count >= 3 else "主要航路ベース"
                auto_avoid = "TRUE" if (42.0 <= lat_b <= 46.0 and 144.0 <= lng_b <= 148.0) else "FALSE"
                
                manual_algo = override_algo_dict.get((lat_b, lng_b), "")
                manual_avoid = override_avoid_dict.get((lat_b, lng_b), "")
                remark_text = remarks_dict.get((lat_b, lng_b), "")
                
                rows.append([lat_b, lng_b, count, auto_algo, auto_avoid, manual_algo, manual_avoid, remark_text])
                
            ws_algo.append_rows(rows)
            print(f"[INFO] Algorithm_Map シートを {len(rows)-1} エリアで更新しました。", flush=True)
            records = ws_algo.get_all_records()
        else:
            records = existing_data

        for r in records:
            try:
                lat_b = float(r.get("Lat_Block", 0))
                lng_b = float(r.get("Lng_Block", 0))
                k = (lat_b, lng_b)
                
                manual_algo = str(r.get("Manual_Algorithm", "")).strip()
                if not manual_algo and "Manual_Override" in r:
                    manual_algo = str(r.get("Manual_Override", "")).strip()
                if manual_algo in ("主要航路ベース", "測定点ベース"):
                    GLOBAL_MANUAL_ALGO_MAP[k] = manual_algo
                    
                auto_algo = str(r.get("Auto_Algorithm", "")).strip()
                if auto_algo in ("主要航路ベース", "測定点ベース"):
                    GLOBAL_AUTO_ALGO_MAP[k] = auto_algo
                
                avoid = str(r.get("Manual_Avoid_Northern", "")).strip().upper()
                if avoid not in ("TRUE", "FALSE"):
                    avoid = str(r.get("Auto_Avoid_Northern", "")).strip().upper()
                if avoid == "TRUE":
                    GLOBAL_AVOID_NORTHERN.add(k)
            except Exception:
                pass
                
        print(f"[INFO] Algorithm_Map 読み込み完了: 手動固定 {len(GLOBAL_MANUAL_ALGO_MAP)} 件 / 自動エリア {len(GLOBAL_AUTO_ALGO_MAP)} 件", flush=True)
    except Exception as e:
        print(f"[WARN] Algorithm_Map の処理中にエラーが発生しました: {e}", flush=True)


def is_johore_singapore_zone(p1, p2):
    in1 = (0.9 <= p1[0] <= 2.1 and 103.2 <= p1[1] <= 104.6)
    in2 = (0.9 <= p2[0] <= 2.1 and 103.2 <= p2[1] <= 104.6)
    return in1 and in2


def is_johore_strait_north(pt):
    """ジョホール海峡側（チャンギ半島の北側・北西側）かどうか"""
    return (1.35 <= pt[0] <= 1.65 and 103.65 <= pt[1] <= 104.06)


def is_changi_crossing(p1, p2):
    """ジョホール海峡側とシンガポール海峡・南側をまたぎ、チャンギ半島を陸切りする恐れがあるか"""
    p1_j = is_johore_strait_north(p1)
    p2_j = is_johore_strait_north(p2)
    p1_s = (p1[0] <= 1.34)
    p2_s = (p2[0] <= 1.34)
    return (p1_j and p2_s) or (p2_j and p1_s)


def is_sakhalin_point(pt):
    return (45.85 <= pt[0] <= 54.5 and 140.5 <= pt[1] <= 146.5)


def is_narrow_strait_zone(p1, p2):
    for pt in (p1, p2):
        lat, lng = pt[0], pt[1]
        if 1.5 <= lat <= 3.5 and 100.5 <= lng <= 103.0:
            return True
        if 33.5 <= lat <= 35.7 and 130.7 <= lng <= 140.1:
            return True
    return False


def is_archipelago_or_canal_point(lat, lng):
    if -12.0 <= lat <= 8.0 and 95.0 <= lng <= 145.0:
        return True
    if 30.0 <= lat <= 54.5 and 128.0 <= lng <= 147.0:
        return True
    if 12.0 <= lat <= 32.0 and 32.0 <= lng <= 44.0:
        return True
    if 8.5 <= lat <= 9.8 and -80.2 <= lng <= -79.2:
        return True
    return False


def get_manual_override_algorithm(p1, p2):
    k1 = (math.floor(p1[0] / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
          math.floor(p1[1] / MACRO_RESOLUTION) * MACRO_RESOLUTION)
    k2 = (math.floor(p2[0] / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
          math.floor(p2[1] / MACRO_RESOLUTION) * MACRO_RESOLUTION)
          
    m1 = GLOBAL_MANUAL_ALGO_MAP.get(k1)
    m2 = GLOBAL_MANUAL_ALGO_MAP.get(k2)
    
    if m1 == "主要航路ベース" or m2 == "主要航路ベース":
        return "主要航路ベース"
    if m1 == "測定点ベース" and m2 == "測定点ベース":
        return "測定点ベース"
    if m1 in ("主要航路ベース", "測定点ベース") and not m2:
        return m1
    if m2 in ("主要航路ベース", "測定点ベース") and not m1:
        return m2
    return None


def parse_gt(gt_val):
    try:
        clean = str(gt_val).replace(",", "").strip()
        return float(clean)
    except Exception:
        return 0.0


def calculate_bearing(p1, p2):
    lat1 = math.radians(p1[0])
    lat2 = math.radians(p2[0])
    diff_long = math.radians(wrapped_lng_diff(p1[1], p2[1]))
    x = math.sin(diff_long) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(diff_long))
    initial_bearing = math.degrees(math.atan2(x, y))
    return round((initial_bearing + 360) % 360, 1)


def get_master_data(client):
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Japan_Master")
    records = sheet.get_all_records()
    master_map = {}
    flag_cnt, ctrl_cnt = 0, 0
    type_counts_dict = defaultdict(int) 
    
    for r in records:
        mmsi = str(r.get("MMSI", "")).strip().split(".")[0]
        if not mmsi or mmsi in ("nan", "None", "0"):
            continue
        
        gt_num = parse_gt(r.get("GT", 0))
        if gt_num > 0 and gt_num < 2000:
            continue
            
        cat = str(r.get("Category", "日本船社運航船")).strip()
        if cat == "日本籍船":
            flag_cnt += 1
        else:
            ctrl_cnt += 1
        
        ship_type = str(r.get("Ship Type", "-")).strip()
        if not ship_type:
            ship_type = "-"
        type_counts_dict[ship_type] += 1 
        
        master_map[mmsi] = {
            "mmsi": mmsi,
            "name": str(r.get("Name of Ship", "")).strip(),
            "imo": str(r.get("IMO/LR/IHS No.", "-")).strip(),
            "category": cat,
            "flag": str(r.get("Flag", "-")).strip(),
            "type": ship_type,
            "gt": str(r.get("GT", "-")).strip(),
            "owner": str(r.get("Registered Owner", "-")).strip(),
            "operator": str(r.get("Operator", "-")).strip(),
            "survey": str(r.get("Special Survey", "-")).strip()
        }
        
    sorted_types = sorted(type_counts_dict.items(), key=lambda x: x[1], reverse=True)
    type_counts_list = [{"type": k, "count": v} for k, v in sorted_types]
    
    return master_map, {"total": len(master_map), "flag": flag_cnt, "ctrl": ctrl_cnt, "type_counts": type_counts_list}


def get_port_dictionary(client):
    port_dict = {}
    existing_ais_names = set()
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Port_Dictionary")
        records = sheet.get_all_records()
        for r in records:
            ais_name = str(r.get("変換元(AIS表記)", "")).strip().upper()
            if ais_name:
                existing_ais_names.add(ais_name)
                lat = r.get("Lat")
                lng = r.get("Lng")
                name = r.get("寄港地名(標準)")
                if lat not in ("", None) and lng not in ("", None):
                    try:
                        port_dict[ais_name] = {
                            "name": str(name) if name else ais_name,
                            "lat": float(lat),
                            "lng": float(lng),
                            "source": "manual"
                        }
                    except ValueError:
                        pass
    except Exception:
        pass
    return port_dict, existing_ais_names


def is_point_on_land(lat, lng):
    if not HAS_LAND_MASK:
        return False
    lng = wrap_lng(lng)
    key = (round(lat, 3), round(lng, 3))
    cached = _LAND_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
            res = bool(globe.is_land(lat, lng))
            _LAND_CACHE[key] = res
            return res
    except Exception:
        pass
    return False


def is_near_johore_real_point(lat, lng, radius_deg=0.008):
    if not (0.9 <= lat <= 2.1 and 103.2 <= lng <= 104.6):
        return False
    for r_lat, r_lng in JOHORE_REAL_POINTS:
        if abs(lat - r_lat) <= radius_deg and abs(lng - r_lng) <= radius_deg:
            if math.hypot(lat - r_lat, lng - r_lng) <= radius_deg:
                return True
    return False


def is_deep_inland(lat, lng, max_inland_deg=0.22):
    if not HAS_LAND_MASK:
        return False
    lng = wrap_lng(lng)
    key = (round(lat, 2), round(lng, 2))
    cached = _DEEP_INLAND_CACHE.get(key)
    if cached is not None:
        return cached

    if not is_point_on_land(lat, lng):
        _DEEP_INLAND_CACHE[key] = False
        return False

    for rad in (max_inland_deg * 0.5, max_inland_deg):
        d = rad * 0.7071
        for dy, dx in [(rad, 0), (-rad, 0), (0, rad), (0, -rad),
                       (d, d), (d, -d), (-d, d), (-d, -d)]:
            if not is_point_on_land(lat + dy, wrap_lng(lng + dx)):
                _DEEP_INLAND_CACHE[key] = False
                return False

    _DEEP_INLAND_CACHE[key] = True
    return True


def infer_port_areas_from_fleet(tracks_map, base_port_dict):
    effective_dict = dict(base_port_dict)
    dest_lats = defaultdict(list)
    dest_lngs = defaultdict(list)

    for mmsi, data_list in tracks_map.items():
        if not data_list:
            continue
        sorted_items = sorted(data_list, key=lambda x: (x[0], x[2]))
        prev_dest = ""
        prev_pt = None

        for ts, pt, idx, r_dest, r_spd in sorted_items:
            if is_deep_inland(pt[0], pt[1]):
                continue
            d_clean = str(r_dest).strip().upper()
            if d_clean in ("-", "NONE", ""):
                d_clean = prev_dest

            if d_clean and d_clean not in ("-", "NONE", ""):
                if r_spd is not None and r_spd <= 1.5:
                    dest_lats[d_clean].extend([pt[0], pt[0]])
                    dest_lngs[d_clean].extend([pt[1], pt[1]])

                if prev_dest and d_clean != prev_dest:
                    if prev_pt is not None:
                        dest_lats[prev_dest].append(prev_pt[0])
                        dest_lngs[prev_dest].append(prev_pt[1])
                    dest_lats[prev_dest].append(pt[0])
                    dest_lngs[prev_dest].append(pt[1])

            if d_clean:
                prev_dest = d_clean
            prev_pt = pt

    inferred_count = 0
    for dest_code, lats in dest_lats.items():
        if dest_code in effective_dict:
            continue
        if len(lats) >= 3:
            med_lat = round(statistics.median(lats), 4)
            med_lng = round(statistics.median(dest_lngs[dest_code]), 4)
            if is_deep_inland(med_lat, med_lng):
                continue
            effective_dict[dest_code] = {
                "name": f"{dest_code}(他船実績推定)",
                "lat": med_lat,
                "lng": med_lng,
                "source": "inferred"
            }
            inferred_count += 1

    if inferred_count > 0:
        print(f"[INFO] 他船実績から {inferred_count} 件の想定寄港エリアを補完しました。", flush=True)

    return effective_dict


def robust_parse_timestamp(ts_str):
    ts_str = str(ts_str).strip()
    if not ts_str:
        return datetime.min
    formats = [
        "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S"
    ]
    clean_ts = ts_str.split(".")[0].replace(" +0000 UTC", "").replace(" UTC", "").replace("Z", "").replace("T", " ")
    for fmt in formats:
        try:
            return datetime.strptime(clean_ts, fmt)
        except ValueError:
            pass
    return datetime.min


def generate_targeted_bboxes(master_map, existing_latest):
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    missing_or_old_ships = []

    for mmsi in master_map.keys():
        if mmsi not in existing_latest:
            continue 
            
        ship = existing_latest[mmsi]
        ts_str = ship.get("timestamp", "")
        last_time = robust_parse_timestamp(ts_str)
        if last_time == datetime.min:
            continue
        hours_offline = (now_utc - last_time).total_seconds() / 3600.0
        
        if hours_offline > 24:
            lat = ship.get("lat")
            lng = ship.get("lng")
            if lat is not None and lng is not None:
                expansion = min(20.0, hours_offline * 0.35) 
                missing_or_old_ships.append((lat, lng, expansion))
    
    targeted_bboxes = []
    missing_or_old_ships.sort(key=lambda x: x[2], reverse=True)
    
    for lat, lng, exp in missing_or_old_ships:
        min_lat = max(-90, lat - exp - 2)
        max_lat = min(90, lat + exp + 2)
        min_lng = max(-180, lng - exp - 2)
        max_lng = min(180, lng + exp + 2)
        targeted_bboxes.append([[min_lat, min_lng], [max_lat, max_lng]])
    
    base_bboxes = [
        [[-40, -20], [35, 60]],  
        [[-15, 60], [30, 140]],  
        [[-90, -180], [90, 180]] 
    ]
    
    return targeted_bboxes[:5] + base_bboxes


async def process_message_queue(queue, target_mmsis, received):
    while True:
        msg = await queue.get()
        try:
            data = json.loads(msg)
            mmsi = str(data.get("MetaData", {}).get("MMSI", "")).strip().split(".")[0]
            
            if mmsi in target_mmsis:
                if mmsi not in received:
                    received[mmsi] = {
                        "mmsi": mmsi, "lat": None, "lng": None, "speed": 0.0, "heading": 0.0,
                        "name": data.get("MetaData", {}).get("ShipName", "").strip(),
                        "destination": "", "eta": "", "timestamp": data.get("MetaData", {}).get("time_utc")
                    }
                
                msg_type = data.get("MessageType")
                if msg_type == "PositionReport":
                    pos = data.get("Message", {}).get("PositionReport", {})
                    lat_v = pos.get("Latitude")
                    lng_v = pos.get("Longitude")
                    if lat_v is not None and lng_v is not None and not is_deep_inland(lat_v, lng_v):
                        received[mmsi]["lat"] = lat_v
                        received[mmsi]["lng"] = lng_v
                        received[mmsi]["speed"] = round(pos.get("Sog", 0.0), 1)
                        if data.get("MetaData", {}).get("time_utc"):
                            received[mmsi]["timestamp"] = data.get("MetaData", {}).get("time_utc")
                elif msg_type == "ShipStaticData":
                    static = data.get("Message", {}).get("ShipStaticData", {})
                    if dest := static.get("Destination", "").strip():
                        received[mmsi]["destination"] = dest
                    eta = static.get("Eta", {})
                    if eta and eta.get("Month"):
                        received[mmsi]["eta"] = f"{eta.get('Month'):02d}-{eta.get('Day'):02d} {eta.get('Hour'):02d}:{eta.get('Minute'):02d}"
        except Exception:
            pass
        finally:
            queue.task_done()


async def collect_ais_data(target_mmsis, bboxes):
    received = {}
    if not AISSTREAM_API_KEY:
        return received
    
    duration = 360 if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else 30
    print(f"[INFO] AISデータ受信開始 ({duration}秒間)...", flush=True)
    url = "wss://stream.aisstream.io/v0/stream"
    sub_msg = {"APIKey": AISSTREAM_API_KEY, "BoundingBoxes": bboxes, "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    queue = asyncio.Queue()
    consumer_task = asyncio.create_task(process_message_queue(queue, target_mmsis, received))

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps(sub_msg))
            start = time.time()
            while time.time() - start < duration:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    await queue.put(msg)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
    except Exception: 
        pass
        
    await queue.join()
    consumer_task.cancel()
    print(f"[INFO] AISデータ受信完了: {len(received)} 隻受信", flush=True)
    return received


def is_point_on_land_with_buffer(lat, lng, buffer_deg=OFFSHORE_BUFFER_DEG):
    lng = wrap_lng(lng)
    key = (round(lat, 2), round(lng, 2))
    cached = _BUFFER_CACHE.get(key)
    if cached is not None:
        return cached

    if is_point_on_land(lat, lng):
        _BUFFER_CACHE[key] = True
        return True
    for r in (buffer_deg * 0.5, buffer_deg):
        d = r * 0.7071
        for dy, dx in [(r, 0), (-r, 0), (0, r), (0, -r),
                       (d, d), (d, -d), (-d, d), (-d, -d)]:
            if is_point_on_land(lat + dy, wrap_lng(lng + dx)):
                _BUFFER_CACHE[key] = True
                return True
    _BUFFER_CACHE[key] = False
    return False


def route_crosses_land(coords, use_buffer=True, allow_johore_real=False, is_searoute_native=False):
    if not HAS_LAND_MASK or len(coords) < 2:
        return False

    p_start = coords[0]
    p_end = coords[-1]
    start_on_coast_mesh = is_point_on_land(p_start[0], p_start[1])
    end_on_coast_mesh   = is_point_on_land(p_end[0], p_end[1])
    start_in_buffer     = is_point_on_land_with_buffer(p_start[0], p_start[1])
    end_in_buffer       = is_point_on_land_with_buffer(p_end[0], p_end[1])

    start_exempt_rad = 0.03 if start_on_coast_mesh else 0.012
    end_exempt_rad   = 0.03 if end_on_coast_mesh else 0.012

    consecutive_land_deg = 0.0

    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        dlng = wrapped_lng_diff(a[1], b[1])
        dist = math.hypot(b[0] - a[0], dlng)
        if dist == 0: continue
        
        step_size = 0.005
        steps = max(2, int(math.ceil(dist / step_size)))
        actual_step_deg = dist / float(steps)

        for s in range(1, steps):
            t = s / float(steps)
            lat = a[0] + (b[0] - a[0]) * t
            lng = wrap_lng(a[1] + dlng * t)
            
            in_start_port = geo_dist_deg([lat, lng], p_start) < start_exempt_rad
            in_end_port   = geo_dist_deg([lat, lng], p_end) < end_exempt_rad

            if not in_start_port and not in_end_port:
                if is_point_on_land(lat, lng):
                    if allow_johore_real and is_near_johore_real_point(lat, lng):
                        consecutive_land_deg = 0.0
                    elif is_searoute_native:
                        consecutive_land_deg += actual_step_deg
                        limit_deg = 0.15 if is_archipelago_or_canal_point(lat, lng) else 0.04
                        if consecutive_land_deg >= limit_deg:
                            return True
                    else:
                        return True
                else:
                    consecutive_land_deg = 0.0

            if use_buffer:
                if start_in_buffer and geo_dist_deg([lat, lng], p_start) < OFFSHORE_BUFFER_DEG * 1.2:
                    continue
                if end_in_buffer and geo_dist_deg([lat, lng], p_end) < OFFSHORE_BUFFER_DEG * 1.2:
                    continue
                if is_point_on_land_with_buffer(lat, lng):
                    return True
                
    return False


def intersects_northern_territories(coords):
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        dlng = wrapped_lng_diff(a[1], b[1])
        dist = math.hypot(b[0] - a[0], dlng)
        if dist == 0: continue
        
        steps = max(2, int(math.ceil(dist / 0.15)))
        for s in range(steps + 1):
            t = s / float(steps)
            lat = a[0] + (b[0] - a[0]) * t
            lng = wrap_lng(a[1] + dlng * t)
            
            if GLOBAL_AVOID_NORTHERN:
                k = (math.floor(lat / MACRO_RESOLUTION) * MACRO_RESOLUTION, 
                     math.floor(lng / MACRO_RESOLUTION) * MACRO_RESOLUTION)
                if k in GLOBAL_AVOID_NORTHERN:
                    return True
            else:
                if 43.2 <= lat <= 45.6 and 144.9 <= lng <= 149.0:
                    return True
    return False


def get_offshore_candidates(pt):
    candidates = []
    dirs = [(-1, 0), (0, 1), (0, -1), (1, 0), (-0.707, 0.707), (-0.707, -0.707), (0.707, 0.707), (0.707, -0.707)]
    for radius in (0.02, 0.05, 0.10, 0.20):
        for dy, dx in dirs:
            n_lat, n_lng = round(pt[0] + dy * radius, 4), round(wrap_lng(pt[1] + dx * radius), 4)
            if not is_point_on_land(n_lat, n_lng):
                candidates.append([n_lat, n_lng])
    return candidates


def get_port_approach_point(port_pos):
    if is_deep_inland(port_pos[0], port_pos[1]):
        return None
    if not is_point_on_land(port_pos[0], port_pos[1]):
        return port_pos
    cands = get_offshore_candidates(port_pos)
    if cands:
        return cands[0]
    return None


def get_searoute_path(p1, p2):
    straight_dist = geo_dist_deg(p1, p2)
    try:
        route = sr.searoute((p1[1], p1[0]), (p2[1], p2[0]))
        coords = route.get("geometry", {}).get("coordinates", [])
        if coords and len(coords) >= 2:
            res = [[round(c[1], 4), round(wrap_lng(c[0]), 4)] for c in coords]
            if len(res) > 2:
                return res
            if straight_dist <= 0.5 or not route_crosses_land([p1, p2], use_buffer=False):
                return res
    except Exception:
        pass

    cands1 = [p1] + get_offshore_candidates(p1)[:4]
    cands2 = [p2] + get_offshore_candidates(p2)[:4]
    for o1 in cands1:
        for o2 in cands2:
            if o1 == p1 and o2 == p2:
                continue
            try:
                alt_route = sr.searoute((o1[1], o1[0]), (o2[1], o2[0]))
                alt_coords = alt_route.get("geometry", {}).get("coordinates", [])
                if alt_coords and len(alt_coords) > 2:
                    return [p1] + [[round(c[1], 4), round(wrap_lng(c[0]), 4)] for c in alt_coords] + [p2]
            except Exception:
                pass
    return None


def get_changi_detour_path(p1, p2):
    p1_north = is_johore_strait_north(p1)
    gates = [JOHORE_EAST_GATE_N, JOHORE_EAST_GATE_S] if p1_north else [JOHORE_EAST_GATE_S, JOHORE_EAST_GATE_N]

    if is_johore_singapore_zone(p1, p2):
        leg1 = _run_johore_astar(p1, gates[0]) or [p1, gates[0]]
        leg2 = _run_johore_astar(gates[1], p2) or [gates[1], p2]
        return leg1 + [gates[1]] + leg2[1:]
    else:
        if p1_north:
            leg1 = _run_johore_astar(p1, gates[0]) or [p1, gates[0]]
            main_leg = query_valid_searoute(gates[1], p2)
            if main_leg:
                return leg1 + main_leg
        else:
            main_leg = query_valid_searoute(p1, gates[0])
            leg2 = _run_johore_astar(gates[1], p2) or [gates[1], p2]
            if main_leg:
                return main_leg + leg2
    return None


def get_johore_crowd_path(p1, p2):
    straight_dist = geo_dist_deg(p1, p2)
    if straight_dist > 1.5:
        return None

    if is_changi_crossing(p1, p2):
        detour = get_changi_detour_path(p1, p2)
        if detour:
            return detour

    return _run_johore_astar(p1, p2)


def _run_johore_astar(p1, p2):
    resolution = 0.012
    start_gk = (int(round(p1[0] / resolution)), int(round(p1[1] / resolution)))
    end_gk = (int(round(p2[0] / resolution)), int(round(p2[1] / resolution)))

    if math.hypot(start_gk[0] - end_gk[0], start_gk[1] - end_gk[1]) <= 1.5:
        if not route_crosses_land([p1, p2], use_buffer=False, allow_johore_real=True):
            return [p1, p2]
        return None

    def heuristic(node):
        return math.hypot(node[0] - end_gk[0], node[1] - end_gk[1]) * 1.0

    open_heap = [(heuristic(start_gk), 0.0, start_gk)]
    came_from = {}
    g_score = {start_gk: 0.0}
    closed_set = set()

    iterations = 0
    while open_heap and iterations < 15000:
        iterations += 1
        _, curr_g, current = heapq.heappop(open_heap)

        if current in closed_set:
            continue
        closed_set.add(current)

        if current == end_gk:
            path = [p2]
            curr_node = current
            while curr_node in came_from:
                curr_node = came_from[curr_node]
                if curr_node != start_gk:
                    path.append([round(curr_node[0] * resolution, 5), round(curr_node[1] * resolution, 5)])
            path.append(p1)
            path.reverse()

            smoothed_path = moving_average_smooth(path, window_size=5)
            pulled = string_pull_path(smoothed_path, max_lookahead=10, use_buffer=False, allow_johore_real=True)
            if not route_crosses_land(pulled, use_buffer=False, allow_johore_real=True):
                return pulled
            return path

        for dx, dy in ((0,1),(1,0),(0,-1),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            neighbor = (current[0] + dx, current[1] + dy)
            if neighbor in closed_set:
                continue
            dist = 1.4142 if (dx != 0 and dy != 0) else 1.0
            n_lat = neighbor[0] * resolution
            n_lng = neighbor[1] * resolution

            near_real = is_near_johore_real_point(n_lat, n_lng, radius_deg=0.008)
            if near_real:
                penalty = 1.0
            elif neighbor != end_gk and is_point_on_land(n_lat, n_lng):
                continue
            else:
                penalty = 3.5

            tentative_g = curr_g + (dist * penalty)
            if tentative_g < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                heapq.heappush(open_heap, (tentative_g + heuristic(neighbor), tentative_g, neighbor))

    return None


def get_sakhalin_main_route(p1, p2):
    p1_sak = is_sakhalin_point(p1)
    p2_sak = is_sakhalin_point(p2)
    if not p1_sak and not p2_sak:
        return None

    def build_sakhalin_leg(sak_pt):
        leg = [sak_pt]
        if sak_pt[0] >= 46.20 and 142.2 <= sak_pt[1] <= 143.6:
            leg.append(ANIWA_BAY_GATE)
        if geo_dist_deg(sak_pt, SOYA_MAIN_GATE) > 0.25:
            leg.append(SOYA_MAIN_GATE)
        return leg

    if p1_sak and not p2_sak:
        leg_to_soya = build_sakhalin_leg(p1)
        main_leg = query_valid_searoute(SOYA_MAIN_GATE, p2)
        if main_leg:
            main_pulled = string_pull_path(subdivide_long_segments(main_leg, 0.25), max_lookahead=40, use_buffer=True)
            combined = leg_to_soya + main_pulled[1:]
            if not intersects_northern_territories(combined):
                return combined
        cs_leg = get_crowd_sourced_path(SOYA_MAIN_GATE, p2, strict_buffer=False)
        if cs_leg:
            return leg_to_soya + cs_leg[1:]

    elif p2_sak and not p1_sak:
        leg_from_soya = list(reversed(build_sakhalin_leg(p2)))
        main_leg = query_valid_searoute(p1, SOYA_MAIN_GATE)
        if main_leg:
            main_pulled = string_pull_path(subdivide_long_segments(main_leg, 0.25), max_lookahead=40, use_buffer=True)
            combined = main_pulled + leg_from_soya[1:]
            if not intersects_northern_territories(combined):
                return combined
        cs_leg = get_crowd_sourced_path(p1, SOYA_MAIN_GATE, strict_buffer=False)
        if cs_leg:
            return cs_leg + leg_from_soya[1:]

    else:
        sr_res = query_valid_searoute(p1, p2)
        if sr_res:
            return sr_res
        if not route_crosses_land([p1, p2], use_buffer=False):
            return [p1, p2]
        return [p1, ANIWA_BAY_GATE, p2]

    return None


def get_local_strait_path(p1, p2):
    straight_dist = geo_dist_deg(p1, p2)
    if straight_dist > 3.5:
        return None

    resolution = max(0.008, min(0.025, round(straight_dist / 110.0, 4)))
    start_gk = (int(round(p1[0] / resolution)), int(round(p1[1] / resolution)))
    end_gk = (int(round(p2[0] / resolution)), int(round(p2[1] / resolution)))

    if math.hypot(start_gk[0] - end_gk[0], start_gk[1] - end_gk[1]) <= 1.5:
        if not route_crosses_land([p1, p2], use_buffer=False):
            return [p1, p2]
        return None

    def heuristic(node):
        return math.hypot(node[0] - end_gk[0], node[1] - end_gk[1]) * 1.05

    open_heap = [(heuristic(start_gk), 0.0, start_gk)]
    came_from = {}
    g_score = {start_gk: 0.0}
    closed_set = set()

    iterations = 0
    while open_heap and iterations < 20000:
        iterations += 1
        _, curr_g, current = heapq.heappop(open_heap)

        if current in closed_set:
            continue
        closed_set.add(current)

        if current == end_gk:
            path = [p2]
            curr_node = current
            while curr_node in came_from:
                curr_node = came_from[curr_node]
                if curr_node != start_gk:
                    path.append([round(curr_node[0] * resolution, 5), round(curr_node[1] * resolution, 5)])
            path.append(p1)
            path.reverse()

            smoothed_path = moving_average_smooth(path, window_size=5)
            pulled = string_pull_path(smoothed_path, max_lookahead=25, use_buffer=False)
            if not route_crosses_land(pulled, use_buffer=False):
                return pulled
            if not route_crosses_land(path, use_buffer=False):
                return path
            return None

        for dx, dy in ((0,1),(1,0),(0,-1),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            neighbor = (current[0] + dx, current[1] + dy)
            if neighbor in closed_set:
                continue
            dist = 1.4142 if (dx != 0 and dy != 0) else 1.0
            n_lat = neighbor[0] * resolution
            n_lng = neighbor[1] * resolution

            if neighbor != end_gk and is_point_on_land(n_lat, n_lng):
                continue

            if dx != 0 and dy != 0 and neighbor != end_gk:
                if is_point_on_land(current[0] * resolution, n_lng) or is_point_on_land(n_lat, current[1] * resolution):
                    continue

            shore_penalty = 1.0
            if neighbor != end_gk:
                chk = resolution * 0.8
                if is_point_on_land(n_lat + chk, n_lng) or is_point_on_land(n_lat - chk, n_lng) or \
                   is_point_on_land(n_lat, n_lng + chk) or is_point_on_land(n_lat, n_lng - chk):
                    shore_penalty = 1.6

            tentative_g = curr_g + (dist * shore_penalty)
            if tentative_g < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                heapq.heappush(open_heap, (tentative_g + heuristic(neighbor), tentative_g, neighbor))

    return None


def string_pull_path(path, max_lookahead=120, use_buffer=True, allow_johore_real=False):
    if len(path) <= 2:
        return path
    smoothed_path = [path[0]]
    current_idx = 0
    n = len(path)
    
    while current_idx < n - 1:
        furthest_visible_idx = current_idx + 1
        search_end = min(n - 1, current_idx + max_lookahead)
        span = search_end - current_idx
        
        if span <= 12:
            for check_idx in range(search_end, current_idx, -1):
                if not route_crosses_land([path[current_idx], path[check_idx]], use_buffer=use_buffer, allow_johore_real=allow_johore_real, is_searoute_native=False) and not intersects_northern_territories([path[current_idx], path[check_idx]]):
                    furthest_visible_idx = check_idx
                    break
        else:
            coarse_step = max(2, span // 10)
            best_coarse = current_idx + 1
            for check_idx in range(search_end, current_idx, -coarse_step):
                if not route_crosses_land([path[current_idx], path[check_idx]], use_buffer=use_buffer, allow_johore_real=allow_johore_real, is_searoute_native=False) and not intersects_northern_territories([path[current_idx], path[check_idx]]):
                    best_coarse = check_idx
                    break
            fine_end = min(search_end, best_coarse + coarse_step - 1)
            for check_idx in range(fine_end, best_coarse - 1, -1):
                if check_idx <= current_idx:
                    break
                if not route_crosses_land([path[current_idx], path[check_idx]], use_buffer=use_buffer, allow_johore_real=allow_johore_real, is_searoute_native=False) and not intersects_northern_territories([path[current_idx], path[check_idx]]):
                    furthest_visible_idx = check_idx
                    break

        smoothed_path.append(path[furthest_visible_idx])
        current_idx = furthest_visible_idx
    return smoothed_path


def moving_average_smooth(path, window_size=7):
    if len(path) < window_size:
        return path
    smoothed = [path[0]]
    for i in range(1, len(path)-1):
        start = max(0, i - window_size // 2)
        end = min(len(path), i + window_size // 2 + 1)
        window = path[start:end]
        avg_lat = sum(p[0] for p in window) / len(window)
        avg_lng = sum(p[1] for p in window) / len(window)
        smoothed.append([avg_lat, avg_lng])
    smoothed.append(path[-1])
    return smoothed


def get_crowd_sourced_path(p1, p2, strict_buffer=True, resolution=0.20):
    global GLOBAL_SAFE_GRIDS
    if abs(p2[1] - p1[1]) > 180.0:
        return None
    start_gk = (int(round(p1[0] / resolution)), int(round(p1[1] / resolution)))
    end_gk = (int(round(p2[0] / resolution)), int(round(p2[1] / resolution)))
    
    if math.hypot(start_gk[0]-end_gk[0], start_gk[1]-end_gk[1]) <= 1.5:
        return [p1, p2]
        
    def heuristic(node):
        return math.hypot(node[0]-end_gk[0], node[1]-end_gk[1]) * 1.05

    open_heap = [(heuristic(start_gk), 0.0, start_gk)]
    came_from = {}
    g_score = {start_gk: 0.0}
    closed_set = set()
    
    iterations = 0
    while open_heap and iterations < 12000:
        iterations += 1
        _, curr_g, current = heapq.heappop(open_heap)
        
        if current in closed_set:
            continue
        closed_set.add(current)
        
        if current == end_gk:
            path = [p2]
            curr_node = current
            while curr_node in came_from:
                curr_node = came_from[curr_node]
                if curr_node != start_gk:
                    path.append([round(curr_node[0]*resolution, 4), round(curr_node[1]*resolution, 4)])
            path.append(p1)
            path.reverse()
            
            if len(path) <= 2:
                return path
                
            smoothed_path = moving_average_smooth(path, window_size=7)
            return string_pull_path(smoothed_path, max_lookahead=80, use_buffer=strict_buffer)
            
        curr_in_buf = is_point_on_land_with_buffer(current[0]*resolution, current[1]*resolution)
        
        for dx, dy in ((0,1),(1,0),(0,-1),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            neighbor = (current[0]+dx, current[1]+dy)
            if neighbor in closed_set:
                continue
            dist = 1.4142 if (dx != 0 and dy != 0) else 1.0
            n_lat = neighbor[0] * resolution
            n_lng = neighbor[1] * resolution
            
            if neighbor != end_gk and is_point_on_land(n_lat, n_lng):
                penalty = 2000.0
            elif strict_buffer and neighbor != end_gk and is_point_on_land_with_buffer(n_lat, n_lng):
                penalty = 18.0 if curr_in_buf else 500.0
            elif neighbor in GLOBAL_SAFE_GRIDS:
                penalty = 1.0
            else:
                penalty = 1.25
                    
            tentative_g = curr_g + (dist * penalty)
            
            if tentative_g < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                heapq.heappush(open_heap, (tentative_g + heuristic(neighbor), tentative_g, neighbor))
                
    return None


def reroute_via_straits(p1, p2):
    best_alt = None
    best_alt_dist = float('inf')
    for via in [TSUGARU_STRAIT, SOYA_STRAIT]:
        r1 = get_searoute_path(p1, via)
        r2 = get_searoute_path(via, p2)
        if r1 and r2:
            alt_res = r1 + r2[1:]
            if not intersects_northern_territories(alt_res):
                d = route_length_deg(alt_res)
                if d < best_alt_dist:
                    best_alt_dist = d
                    best_alt = alt_res
    return best_alt


def query_valid_searoute(p1, p2):
    res = get_searoute_path(p1, p2)
    if not res:
        return None
    
    if geo_dist_deg(res[0], p1) > 0.001: res.insert(0, p1)
    if geo_dist_deg(res[-1], p2) > 0.001: res.append(p2)
    
    if intersects_northern_territories(res):
        alt_res = reroute_via_straits(p1, p2)
        if alt_res:
            return alt_res
        return None
        
    return res


def remove_d_spikes(pts, protect_turns=False):
    if len(pts) < 3 or protect_turns:
        return pts
    
    cleaned = [pts[0]]
    for i in range(1, len(pts) - 1):
        prev_p = cleaned[-1]
        curr_p = pts[i]
        next_p = pts[i + 1]
        
        v1x, v1y = curr_p[0] - prev_p[0], wrapped_lng_diff(prev_p[1], curr_p[1])
        v2x, v2y = next_p[0] - curr_p[0], wrapped_lng_diff(curr_p[1], next_p[1])
        
        norm1 = math.hypot(v1x, v1y)
        norm2 = math.hypot(v2x, v2y)
        
        if norm1 > 0 and norm2 > 0:
            cos_theta = (v1x * v2x + v1y * v2y) / (norm1 * norm2)
            if cos_theta < SPIKE_COS_THETA and norm1 > SPIKE_LEG_MIN_DEG and norm2 > SPIKE_LEG_MIN_DEG:
                if not route_crosses_land([prev_p, next_p], use_buffer=True):
                    continue
                
        cleaned.append(curr_p)
        
    cleaned.append(pts[-1])
    return cleaned


def subdivide_long_segments(coords, max_step_deg=1.5):
    if len(coords) < 2:
        return coords
    subdivided = [coords[0]]
    for i in range(1, len(coords)):
        p1 = subdivided[-1]
        p2 = coords[i]
        dlng = wrapped_lng_diff(p1[1], p2[1])
        dist = math.hypot(p2[0] - p1[0], dlng)
        if dist > max_step_deg:
            n = int(math.ceil(dist / max_step_deg))
            for k in range(1, n):
                t = k / float(n)
                subdivided.append([
                    round(p1[0] + (p2[0] - p1[0]) * t, 5),
                    round(wrap_lng(p1[1] + dlng * t), 5)
                ])
        subdivided.append(p2)
    return subdivided


def smooth_path_chaikin(coords, iterations=4, max_step_deg=1.5):
    if len(coords) < 3:
        return coords
        
    pts = [coords[0]]
    for c in coords[1:]:
        if abs(c[0] - pts[-1][0]) > 1e-6 or abs(wrapped_lng_diff(pts[-1][1], c[1])) > 1e-6:
            pts.append(c)
            
    if len(pts) < 3:
        return pts

    unwrapped = unwrap_coords(pts)
    sub = [unwrapped[0]]
    for i in range(1, len(unwrapped)):
        p1 = sub[-1]
        p2 = unwrapped[i]
        dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if dist > max_step_deg:
            n = int(math.ceil(dist / max_step_deg))
            for k in range(1, n):
                t = k / float(n)
                sub.append([
                    round(p1[0] + (p2[0] - p1[0]) * t, 5),
                    round(p1[1] + (p2[1] - p1[1]) * t, 5)
                ])
        sub.append(p2)

    cur = sub
    for _ in range(iterations):
        smoothed = [cur[0]]
        for i in range(len(cur) - 1):
            p0 = cur[i]
            p1 = cur[i + 1]
            q = [round(0.75 * p0[0] + 0.25 * p1[0], 5), round(0.75 * p0[1] + 0.25 * p1[1], 5)]
            r = [round(0.25 * p0[0] + 0.75 * p1[0], 5), round(0.25 * p0[1] + 0.75 * p1[1], 5)]
            smoothed.extend([q, r])
        smoothed.append(cur[-1])
        cur = smoothed
        
    return [[c[0], round(wrap_lng(c[1]), 5)] for c in cur]


def compute_corridor_coverage(p1, p2):
    if not GLOBAL_SAFE_GRIDS:
        return 0.0
    resolution = 0.20
    dlng = wrapped_lng_diff(p1[1], p2[1])
    dist = math.hypot(p2[0] - p1[0], dlng)
    steps = max(2, int(math.ceil(dist / resolution)))
    hits = 0
    for s in range(steps + 1):
        t = s / float(steps)
        lat = p1[0] + (p2[0] - p1[0]) * t
        lng = wrap_lng(p1[1] + dlng * t)
        gk = (int(round(lat / resolution)), int(round(lng / resolution)))
        if gk in GLOBAL_SAFE_GRIDS or \
           (gk[0]+1, gk[1]) in GLOBAL_SAFE_GRIDS or (gk[0]-1, gk[1]) in GLOBAL_SAFE_GRIDS or \
           (gk[0], gk[1]+1) in GLOBAL_SAFE_GRIDS or (gk[0], gk[1]-1) in GLOBAL_SAFE_GRIDS:
            hits += 1
    return hits / float(steps + 1)


def score_candidate_route(coords, p1, p2, is_main_candidate, corridor_cov, is_strait_or_manual=False):
    if not coords or len(coords) < 2:
        return -999999.0

    if route_crosses_land(coords, use_buffer=False, is_searoute_native=is_main_candidate):
        return -999999.0
    if not is_main_candidate and any(is_deep_inland(c[0], c[1]) for c in coords):
        return -999999.0
    if intersects_northern_territories(coords):
        return -999999.0

    straight_dist = geo_dist_deg(p1, p2)
    route_dist = route_length_deg(coords)

    if is_strait_or_manual:
        max_detour = 30.0
        max_excess = 4.0
    else:
        max_detour = 8.0 if is_main_candidate else 3.0
        max_excess = 3.0 if is_main_candidate else 0.85

    if route_dist > (straight_dist * max_detour) and (route_dist - straight_dist) > max_excess:
        return -999999.0

    score = 100.0

    if not is_strait_or_manual and not is_main_candidate:
        if route_crosses_land(coords, use_buffer=True):
            score -= 50.0

    detour_ratio = route_dist / max(straight_dist, 0.05)
    if detour_ratio > 1.0:
        penalty_weight = 2.0 if (is_strait_or_manual or is_main_candidate) else 12.0
        score -= (detour_ratio - 1.0) * penalty_weight

    for i in range(1, len(coords) - 1):
        v1x, v1y = coords[i][0] - coords[i-1][0], wrapped_lng_diff(coords[i-1][1], coords[i][1])
        v2x, v2y = coords[i+1][0] - coords[i][0], wrapped_lng_diff(coords[i][1], coords[i+1][1])
        n1, n2 = math.hypot(v1x, v1y), math.hypot(v2x, v2y)
        if n1 > 0.02 and n2 > 0.02:
            cos_t = (v1x * v2x + v1y * v2y) / (n1 * n2)
            if cos_t < 0.0:
                score -= (14.0 if is_main_candidate else 22.0) * abs(cos_t)
            elif cos_t < 0.5:
                score -= (4.0 if is_main_candidate else 7.0) * (0.5 - cos_t)

    if GLOBAL_SAFE_GRIDS:
        resolution = 0.20
        hits = sum(1 for c in coords if (int(round(c[0]/resolution)), int(round(c[1]/resolution))) in GLOBAL_SAFE_GRIDS)
        score += (hits / float(len(coords))) * 18.0

    if not is_strait_or_manual:
        crosses_shore_buffer = route_crosses_land([p1, p2], use_buffer=True)
        if not is_main_candidate:
            if corridor_cov >= 0.30 and straight_dist <= 8.0 and not crosses_shore_buffer:
                score += 35.0
        else:
            if crosses_shore_buffer:
                score += 45.0
            elif straight_dist > 6.0:
                score += 30.0
            elif corridor_cov >= 0.30:
                score -= 15.0

    return score


def finalize_endpoints(res, p1, p2):
    out = [list(pt) for pt in res]
    if geo_dist_deg(out[0], p1) > 0.001:
        out.insert(0, list(p1))
    else:
        out[0] = list(p1)
    if geo_dist_deg(out[-1], p2) > 0.001:
        out.append(list(p2))
    else:
        out[-1] = list(p2)
    return out


def _compute_base_geometry(p1, p2, is_port_route=False):
    if is_deep_inland(p1[0], p1[1]) or is_deep_inland(p2[0], p2[1]):
        return None

    straight_dist = geo_dist_deg(p1, p2)
    manual_algo = get_manual_override_algorithm(p1, p2)
    is_strait = is_narrow_strait_zone(p1, p2)

    # A0. チャンギ半島横断（エリア内外問わず東側2段階海上ゲートを強制経由）
    if is_changi_crossing(p1, p2):
        changi_detour = get_changi_detour_path(p1, p2)
        if changi_detour and len(changi_detour) >= 2:
            return changi_detour

    # A. ジョホールバル・シンガポール周辺：他船の実測値による補完を最優先
    if is_johore_singapore_zone(p1, p2):
        johore_path = get_johore_crowd_path(p1, p2)
        if johore_path and len(johore_path) >= 2:
            return johore_path
        if not route_crosses_land([p1, p2], use_buffer=False, allow_johore_real=True):
            return [p1, p2]

    # B. 樺太（サハリン）発着：宗谷海峡経由で主要航路と接続
    if is_sakhalin_point(p1) or is_sakhalin_point(p2):
        sak_route = get_sakhalin_main_route(p1, p2)
        if sak_route and len(sak_route) >= 2:
            return sak_route
    
    if is_port_route:
        if straight_dist < 0.25 and not route_crosses_land([p1, p2], use_buffer=False) and not intersects_northern_territories([p1, p2]):
            return [p1, p2]
        sr_res = query_valid_searoute(p1, p2)
        if sr_res and not intersects_northern_territories(sr_res):
            sr_pulled = string_pull_path(subdivide_long_segments(sr_res, 0.25), max_lookahead=30, use_buffer=True)
            return remove_d_spikes(sr_pulled, protect_turns=False)
        if manual_algo == "主要航路ベース" or is_strait:
            local_res = get_local_strait_path(p1, p2)
            if local_res and not route_crosses_land(local_res, use_buffer=False):
                return local_res
        if straight_dist <= 12.0:
            cs_res = get_crowd_sourced_path(p1, p2, strict_buffer=False)
            if cs_res and not route_crosses_land(cs_res, use_buffer=False) and not intersects_northern_territories(cs_res):
                return cs_res
        return None

    direct_crosses_buf = route_crosses_land([p1, p2], use_buffer=True)
    direct_crosses_raw = route_crosses_land([p1, p2], use_buffer=False)
    direct_northern    = intersects_northern_territories([p1, p2])

    # C. 東京湾・瀬戸内海・マラッカ海峡などの手動固定＆狭水道エリア：主要航路を最優先
    if manual_algo == "主要航路ベース" or is_strait:
        if straight_dist < 0.20 and not direct_crosses_raw and not direct_northern:
            return [p1, p2]
        sr_res = query_valid_searoute(p1, p2)
        if sr_res:
            sr_pulled = string_pull_path(subdivide_long_segments(sr_res, 0.20), max_lookahead=25, use_buffer=False)
            return remove_d_spikes(sr_pulled, protect_turns=False)
        local_strait = get_local_strait_path(p1, p2)
        if local_strait and score_candidate_route(local_strait, p1, p2, False, 0.5, is_strait_or_manual=True) > -500000:
            return local_strait
        if not direct_crosses_raw and not direct_northern and straight_dist < 1.0:
            return [p1, p2]
        if straight_dist <= 12.0:
            cs_fb = get_crowd_sourced_path(p1, p2, strict_buffer=False)
            if cs_fb and score_candidate_route(cs_fb, p1, p2, False, 0.5, is_strait_or_manual=True) > -500000:
                return cs_fb
        # 狭水道・河川フォールバック（画像認識×実測値）
        if straight_dist <= 1.2 and not direct_northern:
            tile_path = get_tile_water_path(p1, p2)
            if tile_path:
                return tile_path
        return None

    if manual_algo == "測定点ベース":
        if straight_dist < 2.5 and not direct_crosses_buf and not direct_northern:
            return [p1, p2]
        if straight_dist <= 12.0:
            cs_res = get_crowd_sourced_path(p1, p2, strict_buffer=True)
            if cs_res:
                cs_res = remove_d_spikes(cs_res, protect_turns=False)
                if score_candidate_route(cs_res, p1, p2, False, 0.8) > -500000:
                    return cs_res

    # 障害物・海岸線バリアに触れない外洋近〜中距離（約220km以内）は円滑直線を即決
    if straight_dist < 2.0 and not direct_crosses_buf and not direct_northern:
        return [p1, p2]

    corridor_cov = compute_corridor_coverage(p1, p2)
    candidates = []

    # 候補A：他船実測点ベース（13km離岸バリア遵守）
    if straight_dist <= 12.0:
        cs_cand = get_crowd_sourced_path(p1, p2, strict_buffer=True)
        if cs_cand:
            cs_cand = remove_d_spikes(cs_cand, protect_turns=False)
            sc_a = score_candidate_route(cs_cand, p1, p2, False, corridor_cov, is_strait_or_manual=False)
            if sc_a > -500000:
                candidates.append((sc_a, cs_cand))

    # 候補B：主要航路（searoute） — 太平洋横断・外洋V字フック引き締め対応
    sr_cand = query_valid_searoute(p1, p2)
    if sr_cand:
        sr_cand = subdivide_long_segments(sr_cand, max_step_deg=0.25)
        sr_cand = string_pull_path(sr_cand, max_lookahead=50, use_buffer=True)
        sr_cand = remove_d_spikes(sr_cand, protect_turns=False)
        sc_b = score_candidate_route(sr_cand, p1, p2, True, corridor_cov, is_strait_or_manual=False)
        if sc_b > -500000:
            candidates.append((sc_b, sr_cand))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # D. 断続化防止フォールバック
    if not direct_crosses_raw and not direct_northern and straight_dist < 1.2:
        return [p1, p2]

    fb_res = 0.20 if straight_dist <= 12.0 else 0.50
    cs_relaxed = get_crowd_sourced_path(p1, p2, strict_buffer=False, resolution=fb_res)
    if cs_relaxed:
        cs_relaxed = remove_d_spikes(cs_relaxed, protect_turns=True)
        if score_candidate_route(cs_relaxed, p1, p2, False, corridor_cov, is_strait_or_manual=True) > -500000:
            return cs_relaxed

    # E. 最終フォールバック：地図タイル画像認識 × AIS実測値（河川・河口港・ボルネオ等の小島・湾内）
    if straight_dist <= 1.2 and not direct_northern:
        tile_path = get_tile_water_path(p1, p2)
        if tile_path and len(tile_path) >= 2:
            return tile_path

    return None


def interpolate_sea_route_linear(p1, p2, time_diff_hrs=None, is_port_route=False, prev_speed=0.0):
    global _ROUTE_CACHE_DIRTY
    cache_key = f"{round(p1[0], 3)},{round(p1[1], 3)}->{round(p2[0], 3)},{round(p2[1], 3)}|{'P' if is_port_route else 'S'}"
    
    if cache_key in _PERSISTENT_ROUTE_CACHE:
        base_res = _PERSISTENT_ROUTE_CACHE[cache_key]
    else:
        base_res = _compute_base_geometry(p1, p2, is_port_route=is_port_route)
        _PERSISTENT_ROUTE_CACHE[cache_key] = base_res
        _ROUTE_CACHE_DIRTY = True

    if not base_res or len(base_res) < 2:
        # 河川・超近距離（約15km以内）で速度妥当な場合のみ最終救済として直接接続
        if not is_port_route and not is_johore_singapore_zone(p1, p2) and not is_changi_crossing(p1, p2):
            d_deg = geo_dist_deg(p1, p2)
            if d_deg <= 0.14 and not intersects_northern_territories([p1, p2]):
                if not time_diff_hrs or time_diff_hrs <= 0 or ((d_deg * 60.0) / time_diff_hrs) <= 20.0:
                    return [list(p1), list(p2)]
        return None

    res = finalize_endpoints(base_res, p1, p2)

    if not is_port_route and time_diff_hrs and time_diff_hrs >= MIN_TIME_HRS_FOR_SPEED:
        route_dist = route_length_deg(res)
        speed_knots = (route_dist * 60.0) / time_diff_hrs
        if speed_knots > MAX_GENERAL_SPEED_KNOTS:
            return None

    return res


def try_infer_segment_via_port(prev_p, curr_p, prev_dest, curr_dest, time_diff_hrs, effective_port_dict):
    p_dest = str(prev_dest).strip().upper()
    c_dest = str(curr_dest).strip().upper()
    
    if not p_dest or p_dest in ("-", "", "NONE"):
        return None
    if not c_dest or c_dest in ("-", "", "NONE"):
        return None
    if p_dest == c_dest:
        return None
        
    port_info = effective_port_dict.get(p_dest)
    if not port_info:
        return None
        
    raw_port_pos = [port_info["lat"], port_info["lng"]]
    
    if geo_dist_deg(prev_p, raw_port_pos) <= PORT_AREA_RADIUS_DEG or \
       geo_dist_deg(curr_p, raw_port_pos) <= PORT_AREA_RADIUS_DEG:
        return None
        
    port_approach = get_port_approach_point(raw_port_pos)
    if not port_approach:
        return None
    
    dist_deg = geo_dist_deg(prev_p, port_approach) + geo_dist_deg(port_approach, curr_p)
    if dist_deg > 25.0:
        return None
        
    if time_diff_hrs and time_diff_hrs > 0:
        req_speed = (dist_deg * 60.0) / time_diff_hrs
        if req_speed > 24.0:
            return None
            
    c1_list = interpolate_sea_route_linear(prev_p, port_approach, is_port_route=True)
    if not c1_list:
        return None
    c2_list = interpolate_sea_route_linear(port_approach, curr_p, is_port_route=True)
    if not c2_list:
        return None
        
    return c1_list + c2_list[1:]


def register_segment_to_grid(p1, p2, mmsi, grid_map, grid_size):
    dlng = wrapped_lng_diff(p1[1], p2[1])
    dist = max(abs(p2[0] - p1[0]), abs(dlng))
    steps = max(1, int(dist / (grid_size * 0.5)))
    keys = set()
    for s in range(steps + 1):
        t = s / float(steps)
        lat = p1[0] + (p2[0] - p1[0]) * t
        lng = wrap_lng(p1[1] + dlng * t)
        gk = (round(lat / grid_size), round(lng / grid_size))
        grid_map[gk].add(mmsi)
        keys.add(gk)
    return list(keys)


def update_history_and_generate_tracks(live_ships, master_map, existing_latest, port_dict):
    t_start = time.time()
    now_utc = datetime.now(timezone.utc)
    month_str = now_utc.strftime("%Y-%m")
    
    history_dir = os.path.join(DATA_DIR, "history", str(CURRENT_YEAR))
    os.makedirs(history_dir, exist_ok=True)
    history_file_path = os.path.join(history_dir, f"history_{month_str}.csv.gz")

    new_history_rows = []
    for mmsi, ship in live_ships.items():
        if mmsi not in master_map:
            continue
        if ship["lat"] is None or ship["lng"] is None:
            continue
        if is_deep_inland(ship["lat"], ship["lng"]):
            continue
        timestamp = now_utc.strftime("%Y/%m/%d %H:%M:%S")
        dest = ship.get("destination", "")
        eta = ship.get("eta", "")
        row = [timestamp, mmsi, ship["lat"], ship["lng"], ship["speed"], ship["heading"], dest, eta]
        new_history_rows.append(row)

    if new_history_rows:
        file_exists = os.path.exists(history_file_path)
        with gzip.open(history_file_path, "at", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "MMSI", "Lat", "Lng", "Speed", "Heading", "Destination", "ETA"])
            writer.writerows(new_history_rows)

    tracks_map = defaultdict(list)
    
    if os.path.exists(history_dir):
        for file in sorted(os.listdir(history_dir)):
            if not (file.endswith('.csv') or file.endswith('.csv.gz')):
                continue
            file_path = os.path.join(history_dir, file)
            try:
                open_func = gzip.open if file_path.endswith('.csv.gz') else open
                with open_func(file_path, "rt", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    c_ts, c_mmsi, c_lat, c_lng, c_spd = 0, 1, 2, 3, 4
                    for i, col_name in enumerate(header):
                        c_name = col_name.strip().lower()
                        if "lat" in c_name or "緯度" in c_name:
                            c_lat = i
                        elif "lon" in c_name or "lng" in c_name or "経度" in c_name:
                            c_lng = i
                        elif "mmsi" in c_name:
                            c_mmsi = i
                        elif "time" in c_name or "date" in c_name or "timestamp" in c_name or "日付" in c_name:
                            c_ts = i
                        elif "speed" in c_name or "sog" in c_name or "速力" in c_name:
                            c_spd = i

                    for idx, row in enumerate(reader):
                        if len(row) < 4:
                            continue
                        r_dest = ""
                        r_spd = 0.0
                        if len(row) == 6 or len(row) == 8:
                            r_ts, r_mmsi, r_lat, r_lng = row[0], row[1], row[2], row[3]
                            try:
                                r_spd = float(row[4])
                            except Exception:
                                pass
                            if len(row) == 8:
                                r_dest = str(row[6]).strip().upper()
                        elif len(row) > max(c_lat, c_lng, c_mmsi, c_ts):
                            r_ts, r_mmsi, r_lat, r_lng = row[c_ts], row[c_mmsi], row[c_lat], row[c_lng]
                            try:
                                if len(row) > c_spd:
                                    r_spd = float(row[c_spd])
                            except Exception:
                                pass
                            if len(row) > 6:
                                r_dest = str(row[6]).strip().upper()
                        else:
                            continue

                        mmsi_str = str(r_mmsi).strip().split(".")[0]
                        if mmsi_str not in master_map:
                            continue
                        try:
                            lat_val = float(r_lat)
                            lng_val = float(r_lng)
                            if not (-90.0 <= lat_val <= 90.0 and -180.0 <= lng_val <= 180.0):
                                continue
                            if is_deep_inland(lat_val, lng_val):
                                continue
                            ts = robust_parse_timestamp(r_ts)
                            if ts == datetime.min:
                                continue
                            pt = [round(lat_val, 4), round(lng_val, 4)]
                            tracks_map[mmsi_str].append((ts, pt, idx, r_dest, r_spd))
                        except Exception:
                            continue
            except Exception:
                pass

    effective_port_dict = infer_port_areas_from_fleet(tracks_map, port_dict)

    global GLOBAL_SAFE_GRIDS, GLOBAL_FINE_REAL_GRIDS, JOHORE_REAL_POINTS
    GLOBAL_SAFE_GRIDS.clear()
    GLOBAL_FINE_REAL_GRIDS.clear()
    johore_pts_set = set()
    CROWD_RESOLUTION = 0.20
    
    if os.path.exists(COVERAGE_POINTS_FILE):
        try:
            with open(COVERAGE_POINTS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                for item in raw_data:
                    if len(item) >= 2 and not is_deep_inland(item[0], item[1]):
                        gk = (int(round(item[0] / CROWD_RESOLUTION)), int(round(item[1] / CROWD_RESOLUTION)))
                        GLOBAL_SAFE_GRIDS.add(gk)
                        GLOBAL_FINE_REAL_GRIDS.add((int(round(item[0] * 100)), int(round(item[1] * 100))))
                        if 0.8 <= item[0] <= 2.2 and 103.0 <= item[1] <= 104.8:
                            johore_pts_set.add((round(item[0], 3), round(item[1], 3)))
        except Exception:
            pass

    for mmsi, data_list in tracks_map.items():
        for item in data_list:
            pt = item[1]
            gk = (int(round(pt[0] / CROWD_RESOLUTION)), int(round(pt[1] / CROWD_RESOLUTION)))
            GLOBAL_SAFE_GRIDS.add(gk)
            GLOBAL_FINE_REAL_GRIDS.add((int(round(pt[0] * 100)), int(round(pt[1] * 100))))
            if 0.8 <= pt[0] <= 2.2 and 103.0 <= pt[1] <= 104.8:
                johore_pts_set.add((round(pt[0], 3), round(pt[1], 3)))

    JOHORE_REAL_POINTS = list(johore_pts_set)

    routes_output = []
    calculated_headings = {}
    grid_all_map = defaultdict(set)
    GRID_SIZE = 0.08

    print(f"[INFO] 軌跡計算開始: 対象 {len(tracks_map)} 隻...", flush=True)

    for mmsi, data_list in tracks_map.items():
        if not data_list:
            continue
        data_list.sort(key=lambda x: (x[0], x[2]))
        
        cleaned_list = []
        for item in data_list:
            ts, pt, idx, r_dest, r_spd = item
            if is_deep_inland(pt[0], pt[1]):
                continue
            if not cleaned_list:
                cleaned_list.append([ts, pt, r_spd, r_dest])
                continue
            
            last_ts, last_pt, last_s, last_d = cleaned_list[-1]
            
            if pt == last_pt:
                if r_dest and r_dest not in ("-", "NONE"):
                    cleaned_list[-1][3] = r_dest
                continue
                
            time_diff_hrs = (ts - last_ts).total_seconds() / 3600.0
            if time_diff_hrs > 0:
                dist_nm = geo_dist_deg(last_pt, pt) * 60.0
                speed = dist_nm / time_diff_hrs
                if dist_nm > 3.0 and speed > 50.0:
                    continue
            
            eff_dest = r_dest if (r_dest and r_dest not in ("-", "NONE")) else last_d
            cleaned_list.append([ts, pt, r_spd, eff_dest])
                
        latest_coord = None
        if mmsi in existing_latest and existing_latest[mmsi].get("lat") is not None and existing_latest[mmsi].get("lng") is not None:
            curr_lat = float(existing_latest[mmsi]["lat"])
            curr_lng = float(existing_latest[mmsi]["lng"])
            if not is_deep_inland(curr_lat, curr_lng):
                curr_spd = existing_latest[mmsi].get("speed", 0.0)
                curr_dest = str(existing_latest[mmsi].get("destination", "")).strip().upper()
                latest_coord = [round(curr_lat, 4), round(curr_lng, 4)]
                latest_ts_str = existing_latest[mmsi].get("timestamp", "")
                latest_ts = robust_parse_timestamp(latest_ts_str)
                if latest_ts == datetime.min:
                    latest_ts = datetime.now(timezone.utc).replace(tzinfo=None)

                if not cleaned_list:
                    cleaned_list.append([latest_ts, latest_coord, curr_spd, curr_dest])
                elif cleaned_list[-1][1] != latest_coord:
                    last_ts, last_pt, last_s, last_d = cleaned_list[-1]
                    time_diff_hrs = (latest_ts - last_ts).total_seconds() / 3600.0
                    if time_diff_hrs > 0:
                        dist_nm = geo_dist_deg(last_pt, latest_coord) * 60.0
                        if not (dist_nm > 3.0 and (dist_nm / time_diff_hrs) > 50.0):
                            eff_dest = curr_dest if (curr_dest and curr_dest not in ("-", "NONE")) else last_d
                            cleaned_list.append([latest_ts, latest_coord, curr_spd, eff_dest])
                else:
                    if curr_dest and curr_dest not in ("-", "NONE"):
                        cleaned_list[-1][3] = curr_dest

        if cleaned_list and mmsi not in existing_latest:
            last_t, last_p, last_s, last_d = cleaned_list[-1]
            m_info = master_map.get(mmsi, {})
            existing_latest[mmsi] = {
                "mmsi": mmsi,
                "name": m_info.get("name") or f"MMSI:{mmsi}",
                "imo": m_info.get("imo", "-"),
                "category": m_info.get("category", "日本船社運航船"),
                "flag": m_info.get("flag", "-"),
                "type": m_info.get("type", "-"),
                "gt": m_info.get("gt", "-"),
                "owner": m_info.get("owner", "-"),
                "operator": m_info.get("operator", "-"),
                "survey": m_info.get("survey", "-"),
                "lat": last_p[0],
                "lng": last_p[1],
                "speed": last_s,
                "heading": 0.0,
                "destination": last_d or "-",
                "eta": "-",
                "timestamp": last_t.strftime("%Y/%m/%d %H:%M:%S") if last_t != datetime.min else ""
            }
            latest_coord = last_p
            
        raw_points = []
        for ts, pt, spd, dest in cleaned_list:
            if ts != datetime.min:
                raw_points.append({"lat": pt[0], "lng": pt[1], "ts": ts.strftime("%m/%d %H:%M"), "mmsi": mmsi})

        if len(cleaned_list) < 2: 
            calculated_headings[mmsi] = 0.0
            continue
            
        category = master_map.get(mmsi, {}).get("category", "外国籍船")

        ordered_blocks = []
        curr_block = [cleaned_list[0][1]]
        curr_protect = False
        curr_johore = False
        curr_searoute = False

        for i in range(1, len(cleaned_list)):
            prev_t, prev_p, prev_s, prev_d = cleaned_list[i-1]
            curr_t, curr_p, curr_s, curr_d = cleaned_list[i]
            time_diff_hrs = (curr_t - prev_t).total_seconds() / 3600.0 if prev_t != datetime.min else 0
            
            port_interpolated = try_infer_segment_via_port(prev_p, curr_p, prev_d, curr_d, time_diff_hrs, effective_port_dict)
            if port_interpolated:
                interpolated = port_interpolated
                curr_protect = True
                curr_searoute = True
            else:
                interpolated = interpolate_sea_route_linear(prev_p, curr_p, time_diff_hrs=time_diff_hrs, prev_speed=prev_s)
                if is_johore_singapore_zone(prev_p, curr_p) or is_changi_crossing(prev_p, curr_p):
                    curr_protect = True
                    curr_johore = True
                elif is_sakhalin_point(prev_p) or is_sakhalin_point(curr_p) or is_narrow_strait_zone(prev_p, curr_p) or get_manual_override_algorithm(prev_p, curr_p) == "主要航路ベース":
                    curr_protect = True
                    curr_searoute = True
                elif is_archipelago_or_canal_point(prev_p[0], prev_p[1]) or is_archipelago_or_canal_point(curr_p[0], curr_p[1]):
                    curr_searoute = True
            
            if interpolated and len(interpolated) >= 2:
                for pt in interpolated[1:]:
                    if abs(pt[0] - curr_block[-1][0]) > 1e-6 or abs(wrapped_lng_diff(curr_block[-1][1], pt[1])) > 1e-6:
                        curr_block.append(pt)
            else:
                if len(curr_block) >= 2:
                    ordered_blocks.append((curr_block, curr_protect, curr_johore, curr_searoute))
                curr_block = [curr_p]
                curr_protect = False
                curr_johore = False
                curr_searoute = False

        if len(curr_block) >= 2:
            ordered_blocks.append((curr_block, curr_protect, curr_johore, curr_searoute))

        if not ordered_blocks:
            continue

        processed_blocks = []
        
        for raw_coords, protect_turns, has_johore, has_searoute in ordered_blocks:
            if len(raw_coords) > 500:
                step = len(raw_coords) / 500.0
                sub_sampled = [raw_coords[int(j * step)] for j in range(500)]
                if sub_sampled[-1] != raw_coords[-1]:
                    sub_sampled.append(raw_coords[-1])
            else:
                sub_sampled = raw_coords
                
            de_spiked = remove_d_spikes(sub_sampled, protect_turns=protect_turns)
            smoothed = smooth_path_chaikin(de_spiked, iterations=4, max_step_deg=1.5)
            
            if route_crosses_land(smoothed, use_buffer=False, allow_johore_real=has_johore, is_searoute_native=has_searoute) or intersects_northern_territories(smoothed):
                smoothed_tight = smooth_path_chaikin(de_spiked, iterations=3, max_step_deg=0.35)
                if not route_crosses_land(smoothed_tight, use_buffer=False, allow_johore_real=has_johore, is_searoute_native=has_searoute) and not intersects_northern_territories(smoothed_tight):
                    smoothed = smoothed_tight
                else:
                    smoothed = de_spiked
                
            split_parts = split_coords_at_dateline(smoothed)
            for part in split_parts:
                if len(part) >= 2:
                    processed_blocks.append(part)

        if not processed_blocks: 
            calculated_headings[mmsi] = 0.0
            continue

        last_coords = processed_blocks[-1]
        heading = calculate_bearing(last_coords[-2], last_coords[-1])
        calculated_headings[mmsi] = heading

        segments = []
        for coords in processed_blocks:
            n_pts = len(coords)
            num_chunks = min(12, max(1, (n_pts - 1) // 6))
            chunk_size = (n_pts - 1) / float(num_chunks)
            for c_idx in range(num_chunks):
                s_i = int(round(c_idx * chunk_size))
                e_i = int(round((c_idx + 1) * chunk_size))
                if e_i <= s_i:
                    e_i = min(n_pts - 1, s_i + 1)
                sub_chunk = coords[s_i : e_i + 1]
                if len(sub_chunk) >= 2:
                    chunk_g_keys = set()
                    for k in range(len(sub_chunk) - 1):
                        gks = register_segment_to_grid(sub_chunk[k], sub_chunk[k+1], mmsi, grid_all_map, GRID_SIZE)
                        chunk_g_keys.update(gks)
                    seg_obj = {
                        "coords": sub_chunk,
                        "g_keys": list(chunk_g_keys)
                    }
                    segments.append(seg_obj)

        if segments:
            routes_output.append({
                "mmsi": mmsi, 
                "category": category, 
                "heading": heading, 
                "segments": segments,
                "raw_points": raw_points
            })

    for r in routes_output:
        for seg in r["segments"]:
            g_keys = seg.pop("g_keys", [])
            shared = set()
            if g_keys:
                count = max(len(grid_all_map.get(gk, set())) for gk in g_keys)
                for gk in g_keys:
                    shared.update(grid_all_map.get(gk, set()))
            else:
                count = 1
            if not shared:
                shared.add(r["mmsi"])
                
            w_mult = min(3.2, 1.0 + (0.5 * math.log2(max(1, count))))
            seg["weight_multiplier"] = round(w_mult, 2)
            seg["shared_mmsis"] = list(shared)

    for mmsi, ship_data in existing_latest.items():
        ship_data["heading"] = calculated_headings.get(mmsi, 0.0)
        dest_val = ship_data.get("destination", "-")
        ship_data["dest_name"] = effective_port_dict.get(dest_val.upper(), {}).get("name", "") if dest_val != "-" else ""

    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes_output, f, ensure_ascii=False)

    save_persistent_route_cache()
    print(f"[INFO] 軌跡計算完了: {len(routes_output)} 隻 (所要時間: {round(time.time() - t_start, 1)}秒)", flush=True)
    return calculated_headings


def update_coverage_from_all_latest(existing_latest):
    t_cov = time.time()
    current_time = time.time()
    six_months_ago = current_time - EXPIRATION_SECONDS

    existing_points_dict = {}
    if os.path.exists(COVERAGE_POINTS_FILE):
        try:
            with open(COVERAGE_POINTS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                for item in raw_data:
                    if len(item) >= 2 and not is_deep_inland(item[0], item[1]):
                        lat, lng = item[0], item[1]
                        ts = item[2] if len(item) >= 3 else current_time
                        existing_points_dict[(lat, lng)] = ts
        except Exception:
            pass

    for mmsi, ship in existing_latest.items():
        lat = ship.get("lat")
        lng = ship.get("lng")
        if lat is not None and lng is not None and not is_deep_inland(float(lat), float(lng)):
            ts_str = ship.get("timestamp", "")
            dt = robust_parse_timestamp(ts_str)
            ts = dt.replace(tzinfo=timezone.utc).timestamp() if dt != datetime.min else current_time
            existing_points_dict[(round(float(lat), 2), round(float(lng), 2))] = ts

    active_points = {}
    for coords, ts in existing_points_dict.items():
        if ts >= six_months_ago:
            active_points[coords] = ts

    merged_points = [[k[0], k[1], v] for k, v in active_points.items()]
    with open(COVERAGE_POINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged_points, f, ensure_ascii=False)

    if active_points:
        snapped_coords = {(round(lat * 20.0) / 20.0, round(lng * 20.0) / 20.0) for lat, lng in active_points.keys()}
        BUFFER_DEG = 0.33
        circles = [Point(lng, lat).buffer(BUFFER_DEG, quad_segs=6) for lat, lng in snapped_coords]
        merged_polygon = unary_union(circles).simplify(0.01, preserve_topology=True)
        
        coverage_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": mapping(merged_polygon),
                    "properties": {
                        "name": "Global AIS Coverage (Active)",
                        "buffer_nm": 20
                    }
                }
            ]
        }
        with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(coverage_geojson, f, ensure_ascii=False)

    print(f"[INFO] カバレッジ更新完了 ({round(time.time() - t_cov, 1)}秒)", flush=True)
    return list(active_points.keys())


def main():
    t_total = time.time()
    client = get_spreadsheet_client()
    if not client:
        return
        
    sync_and_load_algorithm_map(client)
    load_persistent_route_cache()
    
    port_dict, existing_ais_names = get_port_dictionary(client)
    master_map, master_counts = get_master_data(client)
    
    existing_latest = {}
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                for s in json.load(f).get("latest", []):
                    m_key = str(s.get("mmsi", "")).strip().split(".")[0]
                    if m_key and m_key in master_map and s.get("lat") is not None and s.get("lng") is not None:
                        if not is_deep_inland(float(s["lat"]), float(s["lng"])):
                            existing_latest[m_key] = s
        except Exception:
            pass

    bboxes = generate_targeted_bboxes(master_map, existing_latest)
    live_ships = asyncio.run(collect_ais_data(set(master_map.keys()), bboxes))

    for mmsi, ship in live_ships.items():
        if mmsi not in master_map:
            continue
        m_info = master_map.get(mmsi, {})
        prev = existing_latest.get(mmsi, {})
        
        new_lat = ship["lat"] if ship["lat"] is not None else prev.get("lat")
        new_lng = ship["lng"] if ship["lng"] is not None else prev.get("lng")
        if new_lat is None or new_lng is None:
            continue
        if is_deep_inland(float(new_lat), float(new_lng)):
            continue

        new_speed = ship["speed"] if ship["lat"] is not None else prev.get("speed", 0.0)
        new_ts = ship["timestamp"] if (ship["lat"] is not None and ship.get("timestamp")) else prev.get("timestamp", ship.get("timestamp"))

        existing_latest[mmsi] = {
            "mmsi": mmsi,
            "name": m_info.get("name") or ship["name"] or prev.get("name") or f"MMSI:{mmsi}",
            "imo": m_info.get("imo", "-"),
            "category": m_info.get("category", "日本船社運航船"),
            "flag": m_info.get("flag", "-"),
            "type": m_info.get("type", "-"),
            "gt": m_info.get("gt", "-"),
            "owner": m_info.get("owner", "-"),
            "operator": m_info.get("operator", "-"),
            "survey": m_info.get("survey", "-"),
            "lat": new_lat,
            "lng": new_lng,
            "speed": new_speed,
            "heading": prev.get("heading", 0.0),
            "destination": ship["destination"] or prev.get("destination", "-"),
            "eta": ship["eta"] or prev.get("eta", "-"),
            "timestamp": new_ts
        }

    for mmsi, ship_data in existing_latest.items():
        if mmsi in master_map:
            m = master_map[mmsi]
            if not ship_data.get("name") or ship_data["name"].startswith("MMSI:"):
                ship_data["name"] = m["name"] or ship_data["name"]
            if ship_data.get("imo") in ("-", "", None):
                ship_data["imo"] = m["imo"]
            if ship_data.get("category") in ("日本船社運航船", "", None):
                ship_data["category"] = m["category"]
            if ship_data.get("flag") in ("-", "", None):
                ship_data["flag"] = m["flag"]
            if ship_data.get("type") in ("-", "", None):
                ship_data["type"] = m["type"]
            if ship_data.get("gt") in ("-", "", None):
                ship_data["gt"] = m["gt"]
            if ship_data.get("owner") in ("-", "", None):
                ship_data["owner"] = m["owner"]
            if ship_data.get("operator") in ("-", "", None):
                ship_data["operator"] = m["operator"]
            if ship_data.get("survey") in ("-", "", None):
                ship_data["survey"] = m["survey"]
            
        dest_val = ship_data.get("destination", "-")
        ship_data["dest_name"] = port_dict.get(dest_val.upper(), {}).get("name", "") if dest_val != "-" else ""

    active_coords = update_coverage_from_all_latest(existing_latest)
    sync_and_load_algorithm_map(client, active_points=active_coords)

    update_history_and_generate_tracks(live_ships, master_map, existing_latest, port_dict)
    
    recent_count = len([mmsi for mmsi, ship in live_ships.items() if ship.get("lat") is not None and ship.get("lng") is not None])

    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "masterCounts": master_counts, 
            "recent_count": recent_count,
            "latest": list(existing_latest.values())
        }, f, ensure_ascii=False, indent=2)

    seen_dests = set()
    for ship in existing_latest.values():
        d = str(ship.get("destination", "")).strip().upper()
        if d and d not in ("-", "NONE"):
            seen_dests.add(d)
    
    new_dests = seen_dests - existing_ais_names
    if new_dests:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Port_Dictionary")
            append_data = [[d, "", ""] for d in sorted(list(new_dests))]
            sheet.append_rows(append_data)
            print(f"[INFO] 辞書に {len(new_dests)} 件の新しい仕向地を追加しました。", flush=True)
        except Exception:
            pass

    print(f"[INFO] 全処理正常完了 (合計所要時間: {round(time.time() - t_total, 1)}秒)", flush=True)


if __name__ == "__main__":
    main()
