import os
import json
import time
import asyncio
import websockets
from datetime import datetime, timezone
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
import gspread
from google.oauth2.service_account import Credentials

AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
POINTS_FILE = os.path.join(DATA_DIR, "coverage_points.json")
CURRENT_YEAR = datetime.now(timezone.utc).year
COVERAGE_FILE = os.path.join(DATA_DIR, "coverage.json")

LISTEN_DURATION = 300  
# ★ 基地局の有効期限を180日（半年）に設定
EXPIRATION_SECONDS = 180 * 24 * 3600  

def get_spreadsheet_client():
    if not GCP_SA_KEY: return None
    try:
        sa_info = json.loads(GCP_SA_KEY)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        return gspread.authorize(Credentials.from_service_account_info(sa_info, scopes=scopes))
    except Exception as e:
        print(f"Error authenticating to Google Sheets: {e}")
        return None

def get_under_covered_bboxes(points, grid_size=30, max_boxes=12):
    grid_counts = {}
    lat_steps = int(180 / grid_size)
    lng_steps = int(360 / grid_size)
    
    for i in range(lat_steps):
        for j in range(lng_steps):
            grid_counts[(i, j)] = 0
            
    for pt in points:
        lat, lng = pt[0], pt[1]
        lat = max(-90, min(89.9999, lat))
        lng = max(-180, min(179.9999, lng))
        
        i = int((lat + 90) / grid_size)
        j = int((lng + 180) / grid_size)
        grid_counts[(i, j)] += 1
        
    sorted_grids = sorted(grid_counts.items(), key=lambda x: x[1])
    
    bboxes = []
    for (i, j), count in sorted_grids[:max_boxes]:
        min_lat = -90 + i * grid_size
        max_lat = min_lat + grid_size
        min_lng = -180 + j * grid_size
        max_lng = min_lng + grid_size
        bboxes.append([[min_lat, min_lng], [max_lat, max_lng]])
        
    bboxes.append([[-60, 40], [-20, 100]])
    bboxes.append([[-60, -160], [-20, -80]])
    bboxes.append([[-40, -40], [10, 20]])
    
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
                        
                        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
                            collected_points.append([round(lat, 2), round(lng, 2)])
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    continue
    except Exception as e: 
        print(f"[ERROR] WebSocket connection failed: {e}")
        
    return collected_points

def generate_coverage_polygon(coords):
    print(f"Generating polygon from {len(coords)} active points...")
    BUFFER_DEG = 0.83
    
    circles = [Point(lng, lat).buffer(BUFFER_DEG) for lat, lng in coords]
    merged_polygon = unary_union(circles)
    
    geojson_feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(merged_polygon),
                "properties": {
                    "name": "Global AIS Coverage (Active)",
                    "buffer_nm": 50
                }
            }
        ]
    }
    return geojson_feature

# ★ JSONデータと全く同じ内容をスプレッドシートにバックアップ
def backup_coverage_to_spreadsheet(active_points):
    client = get_spreadsheet_client()
    if not client:
        print("Google Sheets credentials not found. Skipping backup.")
        return
    try:
        wb = client.open_by_key(SPREADSHEET_ID)
        try:
            ws_cov = wb.worksheet("AIS_Coverage_History")
        except:
            ws_cov = wb.add_worksheet("AIS_Coverage_History", 1000, 10)
        
        ws_cov.clear()
        rows = [["Latitude", "Longitude", "Last_Seen_Timestamp", "Last_Seen_Date"]]
        for (lat, lng), ts in active_points.items():
            dt_str = datetime.fromtimestamp(ts, timezone.utc).strftime('%Y/%m/%d %H:%M:%S')
            rows.append([lat, lng, ts, dt_str])
        
        ws_cov.append_rows(rows)
        print("Successfully backed up coverage points to Spreadsheet.")
    except Exception as e:
        print(f"Failed to backup to spreadsheet: {e}")

def main():
    current_time = time.time()
    six_months_ago = current_time - EXPIRATION_SECONDS

    existing_points_dict = {}
    if os.path.exists(POINTS_FILE):
        try:
            with open(POINTS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                for item in raw_data:
                    lat, lng = item[0], item[1]
                    ts = item[2] if len(item) >= 3 else current_time
                    existing_points_dict[(lat, lng)] = ts
        except Exception as e:
            print(f"Error loading {POINTS_FILE}: {e}")
            
    print(f"Loaded {len(existing_points_dict)} existing coverage points.")

    bboxes = get_under_covered_bboxes(list(existing_points_dict.keys()), grid_size=30, max_boxes=12)
    new_points = asyncio.run(collect_coverage_data(bboxes))
    print(f"Collected {len(new_points)} new points.")

    for pt in new_points:
        lat, lng = pt[0], pt[1]
        existing_points_dict[(lat, lng)] = current_time

    active_points = {}
    expired_count = 0
    for coords, ts in existing_points_dict.items():
        if ts >= six_months_ago:
            active_points[coords] = ts
        else:
            expired_count += 1
            
    print(f"Total active coverage points: {len(active_points)} (Expired: {expired_count})")

    merged_points = [[k[0], k[1], v] for k, v in active_points.items()]
    with open(POINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged_points, f, ensure_ascii=False)
        
    # ★ 生成したJSONと同じデータをスプレッドシートへバックアップ
    backup_coverage_to_spreadsheet(active_points)

    if active_points:
        coverage_geojson = generate_coverage_polygon(list(active_points.keys()))
        with open(COVERAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(coverage_geojson, f, ensure_ascii=False)
        print("Coverage map successfully updated.")
    else:
        print("No points to generate coverage map.")

if __name__ == "__main__":
    main()
