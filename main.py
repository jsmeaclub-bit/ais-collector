/**
 * 日本関係外航船 航行状況モニター - バックエンド処理
 */

// ▼ 1. GETリクエスト処理（GitHub Actions および Web画面向け） ▼
function doGet(e) {
  const p = (e && e.parameter) ? e.parameter : {};

  // ① GitHub Actions (process_routes.py) への航路データ提供API
  if (p.api === "routes") {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName("Route_Summary_2026");
    if (!sheet) {
      return ContentService.createTextOutput(JSON.stringify([])).setMimeType(ContentService.MimeType.JSON);
    }
    
    const rows = sheet.getDataRange().getValues();
    const result = [];
    for (let i = 1; i < rows.length; i++) {
      const r = rows[i];
      const sLat = Number(r[2]), sLng = Number(r[3]), eLat = Number(r[4]), eLng = Number(r[5]);
      if (isNaN(sLat) || isNaN(sLng) || isNaN(eLat) || isNaN(eLng) || sLat === 0) continue;

      result.push({
        Category: String(r[1] || "日本支配船"),
        Start_Lat: sLat,
        Start_Lng: sLng,
        End_Lat: eLat,
        End_Lng: eLng,
        Count: Number(r[6]) || 1
      });
    }
    return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
  }

  // ② GitHub Actions (main.py) への MMSIリスト提供API ★ここを修復
  if (p.api === "mmsi" || p.type === "mmsi" || p.action === "mmsi" || p.action === "get_mmsi" || p.target === "mmsi") {
    return getMmsiListResponse();
  }

  // ③ 通常ブラウザ閲覧時はダッシュボードHTMLを表示
  return HtmlService.createHtmlOutputFromFile('index')
    .setTitle('日本関係外航船 航行状況モニター')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no');
}

// MMSIリストを自動探索してJSONで返すヘルパー関数
function getMmsiListResponse() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  // マスタシートの候補名を順に探索
  const candidateNames = ["Ship_Master", "Master", "Ships", "MMSI", "MMSI_List", "Target_Ships", "対象船舶"];
  let masterSheet = null;
  for (let name of candidateNames) {
    masterSheet = ss.getSheetByName(name);
    if (masterSheet) break;
  }
  if (!masterSheet) masterSheet = ss.getSheets()[0]; // 見つからない場合は先頭シート

  const data = masterSheet.getDataRange().getValues();
  if (data.length <= 1) {
    return ContentService.createTextOutput(JSON.stringify([])).setMimeType(ContentService.MimeType.JSON);
  }

  // ヘッダー行から MMSI 列を探す（なければ1列目と仮定）
  let mmsiCol = 0;
  for (let c = 0; c < data[0].length; c++) {
    const header = String(data[0][c]).toUpperCase();
    if (header.includes("MMSI")) {
      mmsiCol = c;
      break;
    }
  }

  const mmsiList = [];
  for (let i = 1; i < data.length; i++) {
    const val = String(data[i][mmsiCol]).trim();
    if (val && /^\d{9}$/.test(val)) { // 9桁の数字（MMSI規格）
      mmsiList.push(val);
    } else if (val && !isNaN(Number(val)) && Number(val) > 100000000) {
      mmsiList.push(val);
    }
  }

  return ContentService.createTextOutput(JSON.stringify(mmsiList)).setMimeType(ContentService.MimeType.JSON);
}

// ▼ 2. Web画面（index.html）から呼ばれるリアルタイムデータ取得関数 ▼
function getMapData(targetYear) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const year = targetYear || "2026";
  
  // A. 最新位置シート「Route_Traffic」から取得
  const trafficSheet = ss.getSheetByName("Route_Traffic");
  const latest = [];
  if (trafficSheet) {
    const rows = trafficSheet.getDataRange().getValues();
    for (let i = 1; i < rows.length; i++) {
      const r = rows[i];
      const lat = Number(r[2]), lng = Number(r[3]);
      if (isNaN(lat) || isNaN(lng) || lat === 0 || lng === 0) continue;

      const catRaw = String(r[5] || "").trim();
      const category = catRaw.includes("日本籍") ? "日本籍船" : "日本支配船";

      latest.push({
        mmsi: String(r[0] || ""),
        name: String(r[1] || "Unknown"),
        lat: lat,
        lng: lng,
        speed: Number(r[4]) || 0,
        category: category,
        owner: String(r[6] || "-"),
        type: String(r[7] || "-"),
        survey: String(r[9] || "-")
      });
    }
  }

  // B. 航路集計シート「Route_Summary_2026」から取得
  const routeSheet = ss.getSheetByName("Route_Summary_" + year);
  const routes = [];
  if (routeSheet) {
    const rRows = routeSheet.getDataRange().getValues();
    for (let i = 1; i < rRows.length; i++) {
      const r = rRows[i];
      const sLat = Number(r[2]), sLng = Number(r[3]), eLat = Number(r[4]), eLng = Number(r[5]);
      if (isNaN(sLat) || isNaN(sLng) || isNaN(eLat) || isNaN(eLng) || sLat === 0) continue;

      routes.push({
        cat: String(r[1] || "日本支配船"),
        start: [sLat, sLng],
        end: [eLat, eLng],
        count: Number(r[6]) || 1
      });
    }
  }

  return {
    latest: latest,
    routes: routes,
    currentYear: year,
    years: ["2026"]
  };
}
