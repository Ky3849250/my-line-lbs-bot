import os
import math
import json
import requests
import urllib3
import urllib.parse
import sqlite3
import gzip
import shutil
import traceback
import sys
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, render_template
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    TextMessage, QuickReply, QuickReplyItem, LocationAction,
    FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, LocationMessageContent, PostbackEvent

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

app = Flask(__name__)
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
liff_id = (os.getenv('LINE_LIFF_ID') or '').strip()

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)
user_search_state = {}

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

SUPERIOR_API_URL = "https://superiorapis-creator.cteam.com.tw/manager/feature/proxy/ab930e2f2d73/pub_ab931a177e6e"
SUPERIOR_API_TOKEN = (os.getenv('SUPERIOR_API_TOKEN') or '').strip()

# 核心載入：啟動時將本地資料庫載入記憶體
LOCAL_AED_DATABASE = []
LOCAL_TOILET_DATABASE = []
LOCAL_WATER_DATABASE = []
LOCAL_GAS_DATABASE = []

# 預設正確油價基準點
FALLBACK_PRICE_DATABASE = {
    "92無鉛汽油": "31.2",
    "95無鉛汽油": "32.7",
    "98無鉛汽油": "34.7",
    "超級柴油": "29.9"
}

BASE_DIR = os.path.dirname(__file__)
AED_JSON_PATH = os.path.join(BASE_DIR, "aed_formatted.json")
TOILET_JSON_PATH = os.path.join(BASE_DIR, "toilet.json")
WATER_JSON_PATH = os.path.join(BASE_DIR, "water_fountains_fixed_2_completed.json")
GAS_JSON_PATH = os.path.join(BASE_DIR, "gas_stations_final_2537_complete.json")
PRICE_JSON_PATH = os.path.join(BASE_DIR, "sixtypeoillistprice.json")
BUS_DB_PATH = os.path.join(BASE_DIR, "bus.db")
BUS_GZ_PATH = os.path.join(BASE_DIR, "bus.db.gz")

# 1. 自動解壓 SQLite 資料庫
if not os.path.exists(BUS_DB_PATH) and os.path.exists(BUS_GZ_PATH):
    try:
        print("【系統】正在自動解壓縮 bus.db.gz...", flush=True)
        with gzip.open(BUS_GZ_PATH, 'rb') as f_in:
            with open(BUS_DB_PATH, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
    except Exception as e:
        print(f"【錯誤】bus.db.gz 解壓縮失敗: {e}", flush=True)

# 2. 載入 AED 資料
if os.path.exists(AED_JSON_PATH):
    try:
        with open(AED_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_AED_DATABASE = json.load(f)
    except Exception: pass

# 3. 載入公廁資料
if os.path.exists(TOILET_JSON_PATH):
    try:
        with open(TOILET_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_TOILET_DATABASE = json.load(f)
    except Exception: pass

# 4. 載入飲水機資料
if os.path.exists(WATER_JSON_PATH):
    try:
        with open(WATER_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_WATER_DATABASE = json.load(f)
    except Exception: pass

# 5. 載入備援油價 JSON (加入「防開機污染」邏輯驗證)
if os.path.exists(PRICE_JSON_PATH):
    try:
        with open(PRICE_JSON_PATH, "r", encoding="utf-8") as f:
            price_data = json.load(f)
            temp_startup_prices = {}
            for item in price_data:
                p_name = item.get("產品名稱", "")
                p_val = item.get("參考牌價_金額")
                if p_val:
                    try:
                        val_float = float(p_val)
                        if "92" in p_name: temp_startup_prices["92無鉛汽油"] = val_float
                        elif "95" in p_name: temp_startup_prices["95無鉛汽油"] = val_float
                        elif "98" in p_name: temp_startup_prices["98無鉛汽油"] = val_float
                        elif "超級柴油" in p_name: temp_startup_prices["超級柴油"] = val_float
                    except (ValueError, TypeError): pass
            
            p92 = temp_startup_prices.get("92無鉛汽油", 0)
            p95 = temp_startup_prices.get("95無鉛汽油", 0)
            # 只有當 95 > 92 且價差合理時，才允許覆蓋預設備援庫，防止 22.9 舊數據污染開機值
            if p92 > 20.0 and p95 > p92 and (1.0 <= (p95 - p92) <= 2.5):
                for k, v in temp_startup_prices.items():
                    FALLBACK_PRICE_DATABASE[k] = f"{v:.1f}"
                print("【成功載入】離線備援油價資料庫 (已通過邏輯校驗)。", flush=True)
            else:
                print("【防護啟動】離線備援 JSON 數據異常 (含舊值污染)，保持預設正確油價 (32.7元/公升)。", flush=True)
    except Exception as e:
        print(f"【載入失敗】備援油價讀取異常: {e}", flush=True)

# 6. 載入全量優化版加油站資料庫
if os.path.exists(GAS_JSON_PATH):
    try:
        with open(GAS_JSON_PATH, "r", encoding="utf-8") as f:
            raw_gas_data = json.load(f)
            valid_gas = []
            for st in raw_gas_data:
                if not isinstance(st, dict): continue
                raw_lat = st.get("緯度") or st.get("latitude") or st.get("lat") or st.get("Py")
                raw_lng = st.get("經度") or st.get("longitude") or st.get("lng") or st.get("Px")
                if raw_lat is not None and raw_lng is not None:
                    valid_gas.append(st)
            LOCAL_GAS_DATABASE = valid_gas
            print(f"【成功載入】加油站資料庫共 {len(LOCAL_GAS_DATABASE)} 筆有效紀錄。", flush=True)
    except Exception as e:
        print(f"【載入失敗】加油站讀取異常: {e}", flush=True)

def calculate_distance(origin_latitude: float, origin_longitude: float,
                       destination_latitude: float, destination_longitude: float) -> float:
    earth_radius_meters = 6371000.0
    phi_origin = math.radians(origin_latitude)
    phi_destination = math.radians(destination_latitude)
    delta_phi = math.radians(destination_latitude - origin_latitude)
    delta_lambda = math.radians(destination_longitude - origin_longitude)

    haversine_a = (math.sin(delta_phi / 2.0) ** 2 +
                   math.cos(phi_origin) * math.cos(phi_destination) *
                   math.sin(delta_lambda / 2.0) ** 2)
    haversine_c = 2.0 * math.atan2(math.sqrt(haversine_a), math.sqrt(1.0 - haversine_a))
    return earth_radius_meters * haversine_c

def get_db_connection():
    conn = sqlite3.connect(BUS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def parse_date_key(date_str):
    """解析各式日期字串為 datetime 物件以進行精確時間降序排序"""
    for fmt in ["%Y/%m/%d", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"]:
        try:
            return datetime.strptime(str(date_str).strip(), fmt)
        except ValueError:
            pass
    return datetime.min

def fetch_cpc_prices():
    """雙軌抓取：優先連線 API 並進行 datetime 時間排序與 95 > 92 邏輯校驗，失敗則強制退回正確備援"""
    prices_info = FALLBACK_PRICE_DATABASE.copy()
    try:
        headers = {
            "Authorization": f"Bearer {SUPERIOR_API_TOKEN}",
            "User-Agent": "Mozilla/5.0"
        }
        res = requests.get(SUPERIOR_API_URL, headers=headers, timeout=3, verify=False)
        if res.status_code == 200:
            data = res.json()
            if data and isinstance(data, dict):
                # 精確以 datetime 物件降序排序，確保取得最新日期
                sorted_date_keys = sorted(data.keys(), key=parse_date_key, reverse=True)
                if sorted_date_keys:
                    latest_date_key = sorted_date_keys[0]
                    price_list = data[latest_date_key]
                    
                    temp_prices = {}
                    for item in price_list:
                        name = str(item.get("title", ""))
                        try:
                            price_val = float(item.get("price", 0))
                        except (ValueError, TypeError):
                            continue
                        
                        if price_val > 0:
                            if "92" in name: temp_prices["92無鉛汽油"] = price_val
                            elif "95" in name: temp_prices["95無鉛汽油"] = price_val
                            elif "98" in name: temp_prices["98無鉛汽油"] = price_val
                            elif "柴油" in name: temp_prices["超級柴油"] = price_val
                    
                    p92 = temp_prices.get("92無鉛汽油", 0)
                    p95 = temp_prices.get("95無鉛汽油", 0)
                    
                    # 強制邏輯校驗：95 必須大於 92，且價差在 1.0 ~ 2.5 元之間
                    if p92 > 20.0 and p95 > p92 and (1.0 <= (p95 - p92) <= 2.5):
                        for k, v in temp_prices.items():
                            prices_info[k] = f"{v:.1f}"
                        print(f"【即時油價】成功取得並通過邏輯校驗之最新油價 (日期: {latest_date_key})。", flush=True)
                    else:
                        print(f"【即時油價】第三方 API 油價數據異常 (95={p95}, 92={p92} 不符邏輯)，強制啟動正確備援 (32.7元)。", flush=True)
    except Exception as e:
        print(f"【即時油價】API 連線異常 ({e})，自動啟動備援機制。", flush=True)
    return prices_info

def build_gas_price_legend_flex():
    prices = fetch_cpc_prices()
    bubble = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0288D1",
            "contents": [{"type": "text", "text": "中油今日主產品牌價", "color": "#FFFFFF", "weight": "bold", "size": "md"}]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"⛽ 92無鉛汽油: {prices['92無鉛汽油']}元/公升", "size": "sm", "weight": "bold"},
                {"type": "text", "text": f"⛽ 95無鉛汽油: {prices['95無鉛汽油']}元/公升", "size": "sm", "weight": "bold"},
                {"type": "text", "text": f"⛽ 98無鉛汽油: {prices['98無鉛汽油']}元/公升", "size": "sm", "weight": "bold"},
                {"type": "text", "text": f"🚛 超級柴油: {prices['超級柴油']}元/公升", "size": "sm", "weight": "bold"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "🛠️ 服務項目圖例說明", "size": "sm", "weight": "bold", "color": "#E65100"},
                {"type": "text", "text": "🔹 自助 : 提供刷卡/電子支付自助加油", "size": "xs", "color": "#666666"},
                {"type": "text", "text": "🔹 充氣 : 提供獨立電子式充氣設備", "size": "xs", "color": "#666666"},
                {"type": "text", "text": "🔹 洗車 : 附設自動/手工洗車服務", "size": "xs", "color": "#666666"},
                {"type": "text", "text": "🔹 無障礙 : 設有獨立無障礙公廁", "size": "xs", "color": "#666666"},
                {"type": "text", "text": "🔹 換電站 : 附設電動機車充電/換電站", "size": "xs", "color": "#666666"}
            ]
        }
    }
    return FlexContainer.from_dict(bubble)

def fetch_gas_station_data(user_latitude: float, user_longitude: float) -> list:
    all_gas = []
    for item in LOCAL_GAS_DATABASE:
        raw_lat = str(item.get("緯度") or item.get("latitude") or item.get("lat") or item.get("Py") or 0).strip()
        raw_lng = str(item.get("經度") or item.get("longitude") or item.get("lng") or item.get("Px") or 0).strip()
        
        try: lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        
        if lat == 0.0 or lng == 0.0: continue
        
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        
        operator = str(item.get("營運主體") or item.get("類別") or "").strip()
        raw_name = str(item.get("加油站名稱") or item.get("站名") or "未命名加油站").strip()
        clean_name = raw_name.replace("加油站", "").strip()
        full_name = f"{clean_name}加油站({operator})" if operator else f"{clean_name}加油站"
        
        hours = str(item.get("營業時間", "未提供")).strip()
        hours_display = "🕒 24H 營業" if hours in ["00:00-24:00", "24H"] else f"🕒 {hours}"
        
        fuels = []
        if str(item.get("無鉛92", "0")) in ["1", "1.0", "True"]: fuels.append("92")
        if str(item.get("無鉛95", "0")) in ["1", "1.0", "True"]: fuels.append("95")
        if str(item.get("無鉛98", "0")) in ["1", "1.0", "True"]: fuels.append("98")
        if str(item.get("超柴", "0")) in ["1", "1.0", "True"]: fuels.append("柴油")
        fuel_str = " | ".join(fuels) if fuels else "未提供油品明細"
        
        icons = []
        if str(item.get("刷卡自助", "0")) in ["1", "1.0", "True"] or item.get("泵島汽油自助_服務時段"): icons.append("自助")
        if item.get("獨立電子式充氣") == "有" or item.get("充氣服務_服務時段"): icons.append("充氣")
        wash_info = item.get("洗車類別") or item.get("洗車_服務時段")
        if wash_info and str(wash_info).strip() not in ["無", "", "None"]: icons.append("洗車")
        if item.get("獨立無障礙公廁") == "有" or item.get("無障礙公廁_服務時段"): icons.append("無障礙")
        ev_swap = item.get("電動機車換電服務_服務時段") or item.get("電動機車服務_服務時段")
        if ev_swap and str(ev_swap).strip() not in ["無", "", "None"]: icons.append("換電站")
        icon_str = " ".join(icons) if icons else "無特殊附加服務"
        
        all_gas.append({
            "name": full_name,
            "type": "加油站",
            "latitude": lat,
            "longitude": lng,
            "distance": round(dist),
            "extra_info": f"{hours_display}\n⛽ 油品: {fuel_str}\n🛠️ 服務: {icon_str}"
        })
        
    all_gas.sort(key=lambda x: x["distance"])
    
    results = []
    for item in all_gas:
        is_dup = False
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            if d < 30.0:
                is_dup = True
                break
        if not is_dup:
            results.append(item)
        if len(results) >= 5: break
        
    return results

def get_stop_direction_hint(stop_name: str, passing_routes: list) -> str:
    next_stops = []
    for r in passing_routes:
        stops = r.get('RouteStops', [])
        if stop_name in stops:
            idx = stops.index(stop_name)
            if idx + 1 < len(stops):
                next_stops.append(str(stops[idx + 1]))
    unique_nexts = list(dict.fromkeys(next_stops))[:2]
    if unique_nexts: return f"🚏 下一站：{'/'.join(unique_nexts)}"
    return "路線終點站"

def fetch_bus_data(user_latitude: float, user_longitude: float) -> list:
    if not os.path.exists(BUS_DB_PATH): return []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        lat_min, lat_max = user_latitude - 0.02, user_latitude + 0.02
        lon_min, lon_max = user_longitude - 0.02, user_longitude + 0.02
        cursor.execute('SELECT * FROM bus_stops WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
                       (lat_min, lat_max, lon_min, lon_max))
        rows = cursor.fetchall()
        conn.close()
        raw_list = []
        for row in rows:
            dist = calculate_distance(user_latitude, user_longitude, float(row['lat']), float(row['lon']))
            try: routes = json.loads(row['passing_routes']) if row['passing_routes'] else []
            except Exception: routes = []
            raw_list.append({
                'uid': str(row['stop_uid']), 'name': str(row['stop_name']),
                'lat': float(row['lat']), 'lon': float(row['lon']),
                'routes': routes if isinstance(routes, list) else [], 'dist': dist
            })
        raw_list.sort(key=lambda x: x['dist'])
        
        grouped_cards = []
        for item in raw_list:
            stop_name, item_lat, item_lon = item['name'], item['lat'], item['lon']
            matched_card = None
            for card in grouped_cards:
                if card['name'] == stop_name and calculate_distance(item_lat, item_lon, card['latitude'], card['longitude']) < 15.0:
                    matched_card = card; break
            if matched_card:
                for r in item['routes']:
                    if not isinstance(r, dict): continue
                    r_name, d_code = str(r.get('RouteName', '')).strip(), str(r.get('Direction', '0'))
                    if not r_name: continue
                    route_key = f"{r_name}_{d_code}"
                    if route_key not in matched_card['seen_routes']:
                        matched_card['seen_routes'].add(route_key)
                        matched_card['passing_routes'].append({'RouteName': r_name, 'Direction': d_code, 'RouteStops': r.get('RouteStops', [])})
                        matched_card['stop_uids_map'][route_key] = item['uid']
            else:
                seen_routes, passing_routes, stop_uids_map = set(), [], {}
                for r in item['routes']:
                    if not isinstance(r, dict): continue
                    r_name, d_code = str(r.get('RouteName', '')).strip(), str(r.get('Direction', '0'))
                    if not r_name: continue
                    route_key = f"{r_name}_{d_code}"
                    if route_key not in seen_routes:
                        seen_routes.add(route_key)
                        passing_routes.append({'RouteName': r_name, 'Direction': d_code, 'RouteStops': r.get('RouteStops', [])})
                        stop_uids_map[route_key] = item['uid']
                grouped_cards.append({
                    'uid': item['uid'], 'name': stop_name, 'type': '🚏 公車站牌',
                    'latitude': item_lat, 'longitude': item_lon, 'distance': round(item['dist']),
                    'passing_routes': passing_routes, 'seen_routes': seen_routes, 'stop_uids_map': stop_uids_map
                })
        for card in grouped_cards:
            card['dir_hint'] = get_stop_direction_hint(card['name'], card['passing_routes'])
        grouped_cards.sort(key=lambda x: x['distance'])
        return grouped_cards[:5]
    except Exception as e:
        print(f"【fetch_bus_data 例外】: {e}", flush=True)
        return []

def fetch_aed_data(user_latitude: float, user_longitude: float) -> list:
    all_aeds = []
    for item in LOCAL_AED_DATABASE:
        if not isinstance(item, dict): continue
        raw_lat = item.get("地點LAT") or item.get("latitude") or item.get("lat") or 0.0
        raw_lng = item.get("地點LNG") or item.get("longitude") or item.get("lng") or 0.0
        try: lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        place_name = str(item.get("場所名稱") or "AED 急救站").strip()
        location_detail = str(item.get("AED放置地點") or item.get("AED地點描述") or "詳見現場標示").strip()
        all_aeds.append({
            "name": place_name, "type": "🆘 AED", "latitude": lat, "longitude": lng,
            "distance": round(dist), "extra_info": f"📍 位置: {location_detail}"
        })
    all_aeds.sort(key=lambda x: x["distance"])
    results = []
    for item in all_aeds:
        is_dup = False
        for accepted in results:
            if calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"]) < 30.0:
                is_dup = True; break
        if not is_dup: results.append(item)
        if len(results) >= 5: break
    return results

def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    all_toilets = []
    for item in LOCAL_TOILET_DATABASE:
        if not isinstance(item, dict): continue
        raw_lat = str(item.get("latitude") or item.get("緯度") or item.get("Py") or 0).strip()
        raw_lng = str(item.get("longitude") or item.get("經度") or item.get("Px") or 0).strip()
        try: lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        raw_name = str(item.get("name") or item.get("公廁名稱") or "公共廁所").strip()
        raw_addr = str(item.get("address") or item.get("公廁(廁所)地址") or "").strip()
        toilet_type = str(item.get("type") or item.get("公廁類別") or "").strip()
        grade = str(item.get("grade") or item.get("等級") or "優良").strip()
        all_toilets.append({
            "name": raw_name, "type": f"🚻 公廁({toilet_type})" if toilet_type else "🚻 公廁",
            "latitude": lat, "longitude": lng, "distance": round(dist),
            "extra_info": f"📍 位置: {raw_addr if raw_addr else '詳見現場'}\n⭐ 評等: {grade}"
        })
    all_toilets.sort(key=lambda x: x["distance"])
    results = []
    for item in all_toilets:
        is_dup = False
        for accepted in results:
            if calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"]) < 30.0:
                is_dup = True; break
        if not is_dup: results.append(item)
        if len(results) >= 5: break
    return results

def fetch_water_fountain_data(user_latitude: float, user_longitude: float) -> list:
    all_water = []
    for item in LOCAL_WATER_DATABASE:
        if not isinstance(item, dict): continue
        raw_lat = str(item.get("緯度") or item.get("latitude") or 0).strip()
        raw_lng = str(item.get("經度") or item.get("longitude") or 0).strip()
        try: lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        place_name = str(item.get("場所名稱") or item.get("name") or "飲水機").strip()
        address = str(item.get("詳細地址") or item.get("address") or "詳見現場標示").strip()
        water_temp = str(item.get("水溫") or "常溫").strip()
        all_water.append({
            "name": place_name, "type": "💧 飲水機", "latitude": lat, "longitude": lng,
            "distance": round(dist), "extra_info": f"📍 地址: {address}\n💧 水溫: {water_temp}"
        })
    all_water.sort(key=lambda x: x["distance"])
    results = []
    for item in all_water:
        is_dup = False
        for accepted in results:
            if calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"]) < 30.0:
                is_dup = True; break
        if not is_dup: results.append(item)
        if len(results) >= 5: break
    return results

def fetch_youbike_data(user_latitude: float, user_longitude: float) -> list:
    youbike_results = []
    try:
        url = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=5, verify=False)
        response.raise_for_status()
        for station in response.json():
            if not isinstance(station, dict): continue
            st_lat = float(station.get("latitude") or station.get("lat") or 0)
            st_lng = float(station.get("longitude") or station.get("lng") or 0)
            if st_lat == 0 or st_lng == 0: continue
            dist = calculate_distance(user_latitude, user_longitude, st_lat, st_lng)
            if dist <= 5000.0:
                name = str(station.get("sna", "YouBike")).replace("YouBike2.0_", "")
                av = int(station.get("available_rent_bikes") or station.get("sbi") or 0)
                em = int(station.get("available_return_bikes") or station.get("bemp") or 0)
                youbike_results.append({
                    "name": name, "type": "🚲 YouBike 2.0", "latitude": st_lat, "longitude": st_lng,
                    "distance": round(dist), "extra_info": f"🚲 可借: {av} 輛 | 🅿️ 可還: {em} 格"
                })
    except Exception as e: print(f"【YouBike API 例外】: {e}", flush=True)
    youbike_results.sort(key=lambda x: x["distance"])
    return youbike_results[:5]

@app.route("/liff/map", methods=['GET'])
def liff_map_page():
    return render_template("map.html")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    try:
        user_id = event.source.user_id
        user_text = event.message.text
        
        if any(k in user_text for k in ["加油", "汽油", "中油", "95", "92", "柴油"]): target_type = "加油站"
        elif any(k in user_text for k in ["公車", "站牌", "搭車", "客運"]): target_type = "公車站牌"
        elif any(k in user_text for k in ["公廁", "廁所", "洗洗手問", "尿尿"]): target_type = "公廁"
        elif any(k in user_text for k in ["飲水機", "喝水", "裝水"]): target_type = "飲水機"
        elif "AED" in user_text.upper() or "急救" in user_text: target_type = "AED"
        elif any(k in user_text.lower() for k in ["youbike", "腳踏車", "單車", "ubike"]): target_type = "YouBike"
        else:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                fallback_text = "本服務未提供文字搜尋功能，請點選下方「圖文選單」選擇您想查詢的設施唷！\n\n如有問題或建議，可回報至：service@example.com"
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=fallback_text)]
                    )
                )
            return

        user_search_state[user_id] = target_type
        print(f"【文字觸發】用戶: {user_id} | 選擇項目: {target_type}", flush=True)
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            quick_reply_buttons = QuickReply(items=[QuickReplyItem(action=LocationAction(label="📍 傳送目前位置"))])
            reply_text_message = f"您想尋找【{target_type}】！\n請點擊下方按鈕傳送位置："
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text_message, quick_reply=quick_reply_buttons)]
                )
            )
    except Exception as e:
        print(f"【handle_text_message 例外】: {e}", flush=True)

@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location_message(event):
    user_id = event.source.user_id
    user_latitude = event.message.latitude
    user_longitude = event.message.longitude
    target_type = user_search_state.get(user_id, "加油站")
    
    print(f"【定位請求 Debug】用戶: {user_id} | 項目: {target_type} | 座標: Lat={user_latitude}, Lng={user_longitude}", flush=True)
    
    try:
        if target_type == "YouBike": search_results = fetch_youbike_data(user_latitude, user_longitude)
        elif target_type == "公廁": search_results = fetch_public_toilet_data(user_latitude, user_longitude)
        elif target_type == "AED": search_results = fetch_aed_data(user_latitude, user_longitude)
        elif target_type == "飲水機": search_results = fetch_water_fountain_data(user_latitude, user_longitude)
        elif target_type == "公車站牌": search_results = fetch_bus_data(user_latitude, user_longitude)
        elif target_type == "加油站": search_results = fetch_gas_station_data(user_latitude, user_longitude)
        else: search_results = []
        
        print(f"【搜尋結果 Debug】找到 {len(search_results)} 筆資料", flush=True)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            if not search_results:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"附近找不到【{target_type}】設施。")])
                )
                return

            carousel_contents = {"type": "carousel", "contents": []}
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                if liff_id: liff_map_url = f"https://liff.line.me/{liff_id}?lat={item['latitude']}&lng={item['longitude']}"
                else: liff_map_url = f"https://my-line-lbs-bot.onrender.com/liff/map?lat={item['latitude']}&lng={item['longitude']}"

                body_contents = [
                    {"type": "text", "text": str(item["type"]), "weight": "bold", "size": "xs", "color": "#0288D1"},
                    {"type": "text", "text": str(item["name"]), "weight": "bold", "size": "md", "margin": "xs", "wrap": True}
                ]
                
                # 專屬呈現公車站牌方向指引
                if item.get("dir_hint"):
                    body_contents.append({"type": "text", "text": str(item["dir_hint"]), "size": "xs", "color": "#E65100", "weight": "bold", "margin": "xs"})

                body_contents.append({"type": "text", "text": f"📏 距離: 約 {item['distance']} 公尺", "size": "xs", "color": "#888888"})

                # 處理公車站牌按鈕雙欄網格
                if target_type == "公車站牌":
                    routes_list = item.get('passing_routes', [])
                    display_routes = routes_list[:6]
                    route_chunks = [display_routes[i:i+2] for i in range(0, len(display_routes), 2)]
                    route_rows = []
                    for chunk in route_chunks:
                        row_contents = []
                        for r in chunk:
                            r_name, d_code = str(r.get('RouteName', '')), str(r.get('Direction', '0'))
                            dir_label = "去" if d_code == "0" else "回" if d_code == "1" else "單"
                            route_key = f"{r_name}_{d_code}"
                            specific_uid = str(item.get('stop_uids_map', {}).get(route_key, item['uid']))
                            btn_label = f"{r_name}({dir_label})"[:20]
                            row_contents.append({
                                "type": "box", "layout": "vertical", "backgroundColor": "#F0F4F8", "cornerRadius": "md", "paddingAll": "sm", "flex": 1,
                                "action": {"type": "postback", "label": btn_label, "data": f"action=bus_route&uid={specific_uid}&route={urllib.parse.quote(r_name)}&dir={d_code}"},
                                "contents": [{"type": "text", "text": f"🚌 {r_name} ({dir_label})", "size": "xs", "color": "#1E88E5", "align": "center", "weight": "bold", "wrap": True}]
                            })
                        if len(chunk) == 1:
                            row_contents.append({"type": "box", "layout": "vertical", "flex": 1, "contents": [{"type": "filler"}]})
                        route_rows.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "xs", "contents": row_contents})
                    
                    if route_rows:
                        extra_button = []
                        if len(routes_list) > 6:
                            btn_text = f"🔍 查看全站{len(routes_list)}條車班"
                            extra_button.append({
                                "type": "button",
                                "action": {"type": "postback", "label": btn_text[:20], "data": f"action=all_bus_routes&uid={item['uid']}&stop_name={urllib.parse.quote(item['name'])}"},
                                "style": "secondary", "height": "sm", "margin": "sm"
                            })
                        body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": [{"type": "text", "text": "點擊車班查看路線時間軸:", "size": "xs", "color": "#555555"}, *route_rows, *extra_button]})
                else:
                    body_contents.append({"type": "text", "text": str(item.get("extra_info", "")), "size": "sm", "color": "#333333", "margin": "md", "wrap": True})

                bubble = {
                    "type": "bubble", "size": "mega",
                    "body": {"type": "box", "layout": "vertical", "contents": body_contents},
                    "footer": {
                        "type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [
                            {"type": "button", "action": {"type": "uri", "label": "🗺️ 地圖預覽", "uri": liff_map_url}, "style": "secondary"},
                            {"type": "button", "action": {"type": "uri", "label": "🚀 開始導航", "uri": google_navigation_url}, "style": "primary", "color": "#0288D1"}
                        ]
                    }
                }
                carousel_contents["contents"].append(bubble)

            reply_messages = []
            if target_type == "加油站":
                reply_messages.append(FlexMessage(alt_text="中油今日牌價與服務圖例", contents=build_gas_price_legend_flex()))
                
            reply_messages.append(FlexMessage(alt_text=f"已找到附近的{target_type}", contents=FlexContainer.from_dict(carousel_contents)))

            line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=reply_messages))

    except Exception as e:
        print(f"【handle_location_message 例外】:\n{traceback.format_exc()}", flush=True)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="查詢過程發生錯誤，請稍後再試。")])
            )

@handler.add(PostbackEvent)
def handle_postback(event):
    try:
        data = event.postback.data
        parsed = urllib.parse.parse_qs(data)
        action = parsed.get('action', [''])[0]
        
        if action == 'all_bus_routes':
            uid = parsed.get('uid', [''])[0]
            raw_stop_name = parsed.get('stop_name', [''])[0]
            try: stop_name = urllib.parse.unquote(raw_stop_name)
            except Exception: stop_name = raw_stop_name
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM bus_stops WHERE stop_uid=? OR stop_name=?', (uid, stop_name))
            rows = cursor.fetchall()
            conn.close()
            
            if not rows: return
            actual_stop_name = str(rows[0]['stop_name']) if rows else stop_name
            all_routes, seen_keys, stop_uids_map = [], set(), {}
            for r_row in rows:
                try: r_json = json.loads(r_row['passing_routes']) if r_row['passing_routes'] else []
                except Exception: r_json = []
                for r in r_json:
                    if not isinstance(r, dict): continue
                    r_name, d_code = str(r.get('RouteName', '')).strip(), str(r.get('Direction', '0'))
                    if not r_name: continue
                    route_key = f"{r_name}_{d_code}"
                    if route_key not in seen_keys:
                        seen_keys.add(route_key)
                        all_routes.append(r)
                        stop_uids_map[route_key] = str(r_row['stop_uid'])
            
            display_all_routes = all_routes[:30]
            route_chunks = [display_all_routes[i:i+2] for i in range(0, len(display_all_routes), 2)]
            route_rows = []
            for chunk in route_chunks:
                row_contents = []
                for r in chunk:
                    r_name, d_code = str(r.get('RouteName', '')), str(r.get('Direction', '0'))
                    dir_label = "去" if d_code == "0" else "回" if d_code == "1" else "單"
                    route_key = f"{r_name}_{d_code}"
                    specific_uid = stop_uids_map.get(route_key, uid)
                    btn_label = f"{r_name}({dir_label})"[:20]
                    row_contents.append({
                        "type": "box", "layout": "vertical", "backgroundColor": "#F0F4F8", "cornerRadius": "md", "paddingAll": "sm", "flex": 1,
                        "action": {"type": "postback", "label": btn_label, "data": f"action=bus_route&uid={specific_uid}&route={urllib.parse.quote(r_name)}&dir={d_code}"},
                        "contents": [{"type": "text", "text": f"🚌 {r_name} ({dir_label})", "size": "xs", "color": "#1E88E5", "align": "center", "weight": "bold", "wrap": True}]
                    })
                if len(chunk) == 1:
                    row_contents.append({"type": "box", "layout": "vertical", "flex": 1, "contents": [{"type": "filler"}]})
                route_rows.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "xs", "contents": row_contents})
                
            bubble = {
                "type": "bubble", "size": "mega",
                "header": {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FF9900",
                    "contents": [{"type": "text", "text": f"🚏 【{actual_stop_name}】全站車班 ({len(all_routes)}條)", "color": "#FFFFFF", "weight": "bold"}]
                },
                "body": {
                    "type": "box", "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "點擊車班查看路線時間軸:", "size": "xs", "color": "#555555"},
                        {"type": "box", "layout": "vertical", "margin": "md", "contents": route_rows}
                    ]
                }
            }
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text=f"{actual_stop_name}全車班選單", contents=FlexContainer.from_dict(bubble))])
                )

        elif action == 'bus_route':
            uid = parsed.get('uid', [''])[0]
            raw_route = parsed.get('route', [''])[0]
            try: route_name = urllib.parse.unquote(raw_route)
            except Exception: route_name = raw_route
            dir_code = parsed.get('dir', [''])[0]
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT stop_name, passing_routes FROM bus_stops WHERE stop_uid=?', (uid,))
            row = cursor.fetchone()
            conn.close()
            if not row: return
            
            current_stop_name = str(row['stop_name'])
            try: routes = json.loads(row['passing_routes']) if row['passing_routes'] else []
            except Exception: routes = []
            
            target_route = None
            for r in routes:
                if not isinstance(r, dict): continue
                if str(r.get('RouteName', '')) == route_name and str(r.get('Direction', '')) == dir_code:
                    target_route = r; break
            if not target_route: return
            
            stops_list = target_route.get('RouteStops', [])
            try: current_idx = stops_list.index(current_stop_name)
            except ValueError: current_idx = 0
            
            start_idx, end_idx = 0, len(stops_list)
            if len(stops_list) > 95:
                start_idx, end_idx = max(0, current_idx - 40), min(len(stops_list), start_idx + 95)
            
            timeline_contents = []
            if start_idx > 0: timeline_contents.append({"type": "text", "text": f"↑ ...省略前方 {start_idx} 站", "color": "#888888", "size": "xs"})
            
            for i in range(start_idx, end_idx):
                s_name = str(stops_list[i])
                icon = " 🚇" if any(k in s_name for k in ["捷運", "MRT"]) else " 🚆" if any(k in s_name for k in ["火車", "車站", "台鐵"]) else " 🚄" if "高鐵" in s_name else ""
                
                if i < current_idx: timeline_contents.append({"type": "text", "text": f"⚪ {s_name}{icon}", "color": "#BDBDBD", "weight": "regular", "size": "sm", "margin": "xs"})
                elif i == current_idx: timeline_contents.append({"type": "text", "text": f"📍 {s_name}{icon} (當前站)", "color": "#FF0000", "weight": "bold", "size": "md", "margin": "xs"})
                else: timeline_contents.append({"type": "text", "text": f"🔵 {s_name}{icon}", "color": "#333333", "weight": "regular", "size": "sm", "margin": "xs"})
                
            if end_idx < len(stops_list): timeline_contents.append({"type": "text", "text": f"↓ ...以及後續 {len(stops_list) - end_idx} 站", "color": "#888888", "size": "xs", "margin": "sm"})
            
            dir_label = "去程" if dir_code == "0" else "回程" if dir_code == "1" else "單向"
            bubble = {
                "type": "bubble", "size": "mega",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": "#FF9900", "contents": [{"type": "text", "text": f"🚌 {route_name} ({dir_label})", "color": "#FFFFFF", "weight": "bold"}]},
                "body": {"type": "box", "layout": "vertical", "contents": timeline_contents}
            }
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text=f"{route_name}路線圖", contents=FlexContainer.from_dict(bubble))])
                )
    except Exception as e:
        print(f"【Postback 例外】:\n{traceback.format_exc()}", flush=True)

if __name__ == "__main__":
    app.run(port=5000)