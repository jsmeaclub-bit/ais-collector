import os
import csv
import gzip
import math
import json
from datetime import datetime
from collections import defaultdict

try:
    import searoute as sr
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "searoute"])
    import searoute as sr

DATA_DIR = "data"
HISTORY_DIR = os.path.join(DATA_DIR, "history")
REPORT_FILE = os.path.join(DATA_DIR, "threshold_optimization_report.json")


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


def classify_region(p1, p2):
    lat = (p1[0] + p2[0]) / 2.0
    lng = (p1[1] + p2[1]) / 2.0
    if 30.0 <= lat <= 43.0 and 135.0 <= lng <= 146.0:
        return "Japan_Coast (房総・東北)"
    if -37.0 <= lat <= -28.0 and 16.0 <= lng <= 33.0:
        return "South_Africa (喜望峰周辺)"
    if -28.0 <= lat <= -10.0 and 8.0 <= lng <= 16.0:
        return "West_Africa_Offshore (D字多発域)"
    return "Other_Global"


def extract_gap_samples_from_history():
    """data/history 配下の実測CSVからギャップ区間とsearoute計算結果を抽出する"""
    samples = []
    if not os.path.exists(HISTORY_DIR):
        return samples

    tracks_map = defaultdict(list)
    for root, _, files in os.walk(HISTORY_DIR):
        for file in sorted(files):
            if not (file.endswith(".csv") or file.endswith(".csv.gz")):
                continue
            file_path = os.path.join(root, file)
            open_func = gzip.open if file_path.endswith(".csv.gz") else open
            try:
                with open_func(file_path, "rt", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    for idx, row in enumerate(reader):
                        if len(row) < 4:
                            continue
                        try:
                            ts = robust_parse_timestamp(row[0])
                            mmsi = str(row[1]).strip().split(".")[0]
                            lat = float(row[2])
                            lng = float(row[3])
                            if ts != datetime.min and -90 <= lat <= 90 and -180 <= lng <= 180:
                                tracks_map[mmsi].append((ts, [round(lat, 4), round(lng, 4)], idx))
                        except Exception:
                            continue
            except Exception:
                continue

    for mmsi, pts in tracks_map.items():
        pts.sort(key=lambda x: (x[0], x[2]))
        for i in range(1, len(pts)):
            t1, p1, _ = pts[i - 1]
            t2, p2, _ = pts[i]
            if p1 == p2:
                continue
            dt_hrs = (t2 - t1).total_seconds() / 3600.0
            if dt_hrs <= 0:
                continue
            straight_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            # 0.04度（約4.4km）以上のギャップを対象にサンプリング
            if straight_dist < 0.04 or (straight_dist * 60.0 / dt_hrs) > 50.0:
                continue

            try:
                route = sr.searoute((p1[1], p1[0]), (p2[1], p2[0]))
                coords = route.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2:
                    continue
                res = [[round(c[1], 4), round(c[0], 4)] for c in coords]
                samples.append({
                    "mmsi": mmsi,
                    "p1": p1,
                    "p2": p2,
                    "dt_hrs": dt_hrs,
                    "straight_dist": straight_dist,
                    "raw_route": res,
                    "region": classify_region(p1, p2)
                })
            except Exception:
                continue

            # 計算時間短縮のため最大1,500サンプルまで抽出
            if len(samples) >= 1500:
                return samples

    return samples


def build_synthetic_benchmarks():
    """履歴データが少ない場合やエッジケース検証用の代表海域ベンチマーク"""
    cases = [
        # (p1, p2, dt_hrs, expected_is_valid_detour, label)
        ([35.30, 139.78], [35.75, 140.95], 5.5, True, "房総半島迂回（東京湾→犬吠埼沖・正常）"),
        ([38.25, 141.15], [39.65, 142.10], 7.0, True, "東北三陸沿岸迂回（仙台湾→宮古沖・正常）"),
        ([35.10, 139.75], [35.35, 140.45], 0.35, True, "房総半島迂回＋AISタイムスタンプ揺らぎ（短時間・正常）"),
        ([-33.90, 18.35], [-34.45, 20.80], 11.0, True, "南アフリカ喜望峰迂回（ケープタウン→アガラス岬・正常）"),
        ([-34.35, 18.40], [-33.98, 25.65], 24.0, True, "南アフリカ南岸迂回（喜望峰→ポートエリザベス・正常）"),
        ([-22.50, 14.30], [-23.10, 14.35], 3.0, False, "ナミビア沖D字スパイク（遠方航路引き込み・異常）"),
        ([-18.00, 11.75], [-19.20, 12.40], 5.0, False, "アフリカ西岸D字スパイク（大幅な沖合往復・異常）"),
        ([34.50, 138.80], [34.65, 138.95], 0.8, False, "伊豆沖点スナップスパイク（急角度往復・異常）"),
    ]
    benchmarks = []
    for p1, p2, dt_hrs, is_valid, label in cases:
        straight_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        route = sr.searoute((p1[1], p1[0]), (p2[1], p2[0]))
        coords = route.get("geometry", {}).get("coordinates", [])
        res = [[round(c[1], 4), round(c[0], 4)] for c in coords] if coords else [p1, p2]
        benchmarks.append({
            "mmsi": "BENCHMARK",
            "p1": p1,
            "p2": p2,
            "dt_hrs": dt_hrs,
            "straight_dist": straight_dist,
            "raw_route": res,
            "region": label,
            "ground_truth_valid": is_valid
        })
    return benchmarks


def label_samples(samples):
    """物理法則に基づき、各サンプルの正解ラベル（True=正常な海路維持 / False=スパイク破棄）を付与"""
    labeled = []
    for s in samples:
        if "ground_truth_valid" in s:
            labeled.append(s)
            continue

        res = s["raw_route"]
        route_dist = sum(
            math.hypot(res[i + 1][0] - res[i][0], res[i + 1][1] - res[i][1])
            for i in range(len(res) - 1)
        )
        s["route_dist"] = route_dist
        s["detour_ratio"] = route_dist / max(1e-6, s["straight_dist"])
        s["implied_speed"] = (route_dist * 60.0) / s["dt_hrs"]

        # 十分な時間差(>=1.0h)があるデータから信頼性の高い正解基準を作成
        if s["dt_hrs"] >= 1.0:
            if s["implied_speed"] <= 23.0 and s["detour_ratio"] <= 2.4:
                s["ground_truth_valid"] = True
                labeled.append(s)
            elif s["implied_speed"] > 26.5 or (s["detour_ratio"] > 3.2 and (route_dist - s["straight_dist"]) > 1.0):
                s["ground_truth_valid"] = False
                labeled.append(s)
    return labeled


def remove_d_spikes_test(pts, cos_thresh, leg_thresh):
    if len(pts) < 3:
        return pts
    cleaned = [pts[0]]
    for i in range(1, len(pts) - 1):
        prev_p = cleaned[-1]
        curr_p = pts[i]
        next_p = pts[i + 1]
        v1x, v1y = curr_p[0] - prev_p[0], curr_p[1] - prev_p[1]
        v2x, v2y = next_p[0] - curr_p[0], next_p[1] - curr_p[1]
        norm1 = math.hypot(v1x, v1y)
        norm2 = math.hypot(v2x, v2y)
        if norm1 > 0 and norm2 > 0:
            cos_theta = (v1x * v2x + v1y * v2y) / (norm1 * norm2)
            if cos_theta < cos_thresh and norm1 > leg_thresh and norm2 > leg_thresh:
                continue
        cleaned.append(curr_p)
    cleaned.append(pts[-1])
    return cleaned


def predict_valid_route(sample, params):
    """候補パラメータで海路が採用(True)されるか、直線破棄(False)されるかを判定"""
    if sample["straight_dist"] < params["gap_deg"]:
        return False

    res = remove_d_spikes_test(sample["raw_route"], params["cos_thresh"], params["leg_thresh"])
    if len(res) < 2:
        return False

    route_dist = sum(
        math.hypot(res[i + 1][0] - res[i][0], res[i + 1][1] - res[i][1])
        for i in range(len(res) - 1)
    )

    if route_dist > (sample["straight_dist"] * params["detour_ratio"]) and (route_dist - sample["straight_dist"]) > params["excess_deg"]:
        return False

    if sample["dt_hrs"] >= params["min_time_hrs"]:
        speed_knots = (route_dist * 60.0) / sample["dt_hrs"]
        if speed_knots > params["max_speed"]:
            return False

    return True


def run_grid_search(labeled_samples):
    grid = {
        "gap_deg": [0.05, 0.06, 0.08],
        "max_speed": [23.0, 24.0, 25.0, 26.0],
        "min_time_hrs": [0.35, 0.5, 0.75],
        "detour_ratio": [2.5, 2.8, 3.0, 3.3],
        "excess_deg": [0.6, 0.8, 1.0],
        "cos_thresh": [-0.80, -0.85, -0.90],
        "leg_thresh": [0.30, 0.35, 0.45],
    }

    results = []
    total_valid = sum(1 for s in labeled_samples if s["ground_truth_valid"])
    total_spike = len(labeled_samples) - total_valid

    for gap_deg in grid["gap_deg"]:
        for max_speed in grid["max_speed"]:
            for min_time_hrs in grid["min_time_hrs"]:
                for detour_ratio in grid["detour_ratio"]:
                    for excess_deg in grid["excess_deg"]:
                        for cos_thresh in grid["cos_thresh"]:
                            for leg_thresh in grid["leg_thresh"]:
                                params = {
                                    "gap_deg": gap_deg,
                                    "max_speed": max_speed,
                                    "min_time_hrs": min_time_hrs,
                                    "detour_ratio": detour_ratio,
                                    "excess_deg": excess_deg,
                                    "cos_thresh": cos_thresh,
                                    "leg_thresh": leg_thresh,
                                }
                                kept_valid = 0
                                rejected_spike = 0
                                for s in labeled_samples:
                                    pred = predict_valid_route(s, params)
                                    if s["ground_truth_valid"] and pred:
                                        kept_valid += 1
                                    elif (not s["ground_truth_valid"]) and (not pred):
                                        rejected_spike += 1

                                valid_retention = kept_valid / max(1, total_valid)
                                spike_removal = rejected_spike / max(1, total_spike)
                                # 陸切り防止（正常迂回の維持）を1.2倍重視したバランススコア
                                balance_score = (valid_retention * 1.2 + spike_removal * 1.0) / 2.2

                                results.append({
                                    "score": round(balance_score, 4),
                                    "valid_retention_pct": round(valid_retention * 100, 1),
                                    "spike_removal_pct": round(spike_removal * 100, 1),
                                    "params": params
                                })

    results.sort(key=lambda x: (x["score"], x["valid_retention_pct"], x["spike_removal_pct"]), reverse=True)
    return results


def main():
    print("1. AIS履歴データと海域ベンチマークを読み込み中...")
    history_samples = extract_gap_samples_from_history()
    benchmark_samples = build_synthetic_benchmarks()
    all_samples = label_samples(history_samples + benchmark_samples)

    valid_cnt = sum(1 for s in all_samples if s["ground_truth_valid"])
    spike_cnt = len(all_samples) - valid_cnt
    print(f"   評価対象サンプル数: {len(all_samples)}件 (実測履歴: {len(history_samples)}件 / ベンチマーク: {len(benchmark_samples)}件)")
    print(f"   内訳 -> 維持すべき正常迂回: {valid_cnt}件 / 除去すべきスパイク: {spike_cnt}件\n")

    print("2. 全パラメータ組み合わせのグリッドサーチを実行中...")
    ranked = run_grid_search(all_samples)
    best = ranked[0]
    p = best["params"]

    print("\n========================================================")
    print("  算出された最適バランス設定値（Top 1）")
    print("========================================================")
    print(f"  総合バランススコア   : {best['score'] * 100:.2f} 点")
    print(f"  正常迂回の維持率     : {best['valid_retention_pct']}% （陸地横断の防止率）")
    print(f"  スパイクの除去率     : {best['spike_removal_pct']}% （D字・点スパイク排除率）")
    print("--------------------------------------------------------")
    print("  【main.py 貼り付け用 最適定数セット】")
    print(f"  GAP_THRESHOLD_DEG       = {p['gap_deg']}")
    print(f"  MAX_GENERAL_SPEED_KNOTS = {p['max_speed']}")
    print(f"  MIN_TIME_HRS_FOR_SPEED  = {p['min_time_hrs']}")
    print(f"  MAX_DETOUR_RATIO        = {p['detour_ratio']}")
    print(f"  MAX_EXCESS_DETOUR_DEG   = {p['excess_deg']}")
    print(f"  SPIKE_COS_THETA         = {p['cos_thresh']}")
    print(f"  SPIKE_LEG_MIN_DEG       = {p['leg_thresh']}")
    print("========================================================\n")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "sample_count": len(all_samples),
            "best_result": best,
            "top_5_candidates": ranked[:5]
        }, f, ensure_ascii=False, indent=2)
    print(f"詳細レポートを {REPORT_FILE} に保存しました。")


if __name__ == "__main__":
    main()
