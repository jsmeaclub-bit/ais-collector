def remove_d_spikes(pts):
    """
    searouteが生成した海路のノード列から、沖合の1点へ引き込まれて戻る
    「D字スパイク（鋭角の往復・飛び出し）」だけを検出し、頂点ノードを削除する。
    ※陸を避ける緩やかなカーブは維持されるため、陸切りは発生しない。
    """
    if len(pts) < 3:
        return pts
    
    cleaned = [pts[0]]
    for i in range(1, len(pts) - 1):
        prev_p = cleaned[-1]
        curr_p = pts[i]
        next_p = pts[i + 1]
        
        # 前後の点へのベクトル
        v1x, v1y = curr_p[0] - prev_p[0], curr_p[1] - prev_p[1]
        v2x, v2y = next_p[0] - curr_p[0], next_p[1] - curr_p[1]
        
        norm1 = math.hypot(v1x, v1y)
        norm2 = math.hypot(v2x, v2y)
        
        if norm1 > 0 and norm2 > 0:
            # コサイン類似度（進行方向の変化）を計算
            # -1.0 に近いほど「180度真逆に折り返している（往復スパイク）」ことを示す
            cos_theta = (v1x * v2x + v1y * v2y) / (norm1 * norm2)
            
            # 135度以上の急激な折り返し（往復スパイク）であれば、この飛び出た頂点をスキップする
            if cos_theta < -0.7:
                continue
                
        cleaned.append(curr_p)
        
    cleaned.append(pts[-1])
    return cleaned

def interpolate_sea_route(p1, p2, time_diff_hrs=None, is_port_route=False, prev_speed=0.0):
    straight_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    # 極近距離（約16km未満）のみ直線接続
    if straight_dist < 0.15:
        return [p1, p2]

    try:
        origin = (p1[1], p1[0])
        destination = (p2[1], p2[0])
        route = sr.searoute(origin, destination)
        coords = route.get("geometry", {}).get("coordinates", [])
        
        if coords and len(coords) >= 2:
            res = [[round(c[1], 4), round(c[0], 4)] for c in coords]
            
            # 1. 「ノードが1つ以下（点への往復）」ならスパイクとして直線化
            lane_nodes = [
                c for c in res 
                if math.hypot(c[0] - p1[0], c[1] - p1[1]) > 0.01 and math.hypot(c[0] - p2[0], c[1] - p2[1]) > 0.01
            ]
            if len(lane_nodes) <= 1:
                return [p1, p2]

            # 2. 陸切りを防ぐため、ルート全体を破棄して直線にする速度・距離倍率チェックは廃止。
            # 代わりに、海路上の「鋭角な往復飛び出し（D字スパイクの頂点）」だけを間引く。
            if not is_port_route:
                res = remove_d_spikes(res)
                # スパイク頂点を削った結果、有効な中継ノードが消えた場合は直線にする
                if len(res) <= 2:
                    return [p1, p2]

            if abs(res[0][0] - p1[0]) > 0.001 or abs(res[0][1] - p1[1]) > 0.001: res.insert(0, p1)
            else: res[0] = p1
            if abs(res[-1][0] - p2[0]) > 0.001 or abs(res[-1][1] - p2[1]) > 0.001: res.append(p2)
            else: res[-1] = p2
            return catmull_rom_chain(res)
    except Exception:
        pass
    return [p1, p2]
