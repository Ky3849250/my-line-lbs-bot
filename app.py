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

# ==========================================
# 核心載入：啟動時將本地資料庫載入記憶體 (JSON/SQLite)
# ==========================================
LOCAL_AED_DATABASE = []
LOCAL_TOILET_DATABASE = []
LOCAL_WATER_DATABASE = []

BASE_DIR = os.path.dirname(__file__)
AED_JSON_PATH = os.path.join(BASE_DIR, "aed_formatted.json")
TOILET_JSON_PATH = os.path.join(BASE_DIR, "toilet.json")
WATER_JSON_PATH = os.path.join(BASE_DIR, "water_fountains_fixed_2_completed.json")
BUS_DB_PATH = os.path.join(BASE_DIR, "bus.db")
BUS_GZ_PATH = os.path.join(BASE_DIR, "bus.db.gz")

# 1. 啟動檢測：若無 bus.db 則自動由 bus.db.gz 解壓出來
if not os.path.exists(BUS_DB_PATH) and os.path.exists(BUS_GZ_PATH):
    try:
        print("【系統】正在自動解壓縮 bus.db.gz，請稍候...")
        with gzip.open(BUS_GZ_PATH, 'rb') as f_in:
            with open(BUS_DB_PATH, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        print("【系統】bus.db 解壓縮完成！")
    except Exception as e:
        print(f"【錯誤】bus.db.gz 解壓縮失敗: {e}")

# 2. 載入 AED 全量資料
if os.path.exists(AED_JSON_PATH):
    try:
        with open(AED_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_AED_DATABASE = json.load(f)
    except Exception as e:
        print(f"AED 讀取異常: {e}")

# 3. 載入公廁全量資料
if os.path.exists(TOILET_JSON_PATH):
    try:
        with open(TOILET_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_TOILET_DATABASE = json.load(f)
    except Exception as e:
        print(f"公廁讀取異常: {e}")

# 4. 載入飲水機資料
if os.path.exists(WATER_JSON_PATH):
    try:
        with open(WATER_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_WATER_DATABASE = json.load(f)
    except Exception as e:
        print(f"飲水機讀取異常: {e}")


def calculate_distance(origin_latitude: float, origin_longitude: float, 
                       destination_latitude: float, destination_longitude: float) -> float:
    earth_radius_meters = 6371000.0
    phi_origin = math.radians(origin_latitude)
    phi_destination = math.radians(destination_latitude)
    delta_phi = math.radians(destination_latitude - origin_latitude)
    delta_lambda = math.radians(destination_longitude - origin_longitude)
    
    haversine_a = (math.sin(delta_phi / 2.0) ** 2 + 
                   math.cos(phi_origin) * math.cos(phi_destination) * math.sin(delta_lambda / 2.0) ** 2)
    haversine_c = 2.0 * math.atan2(math.sqrt(haversine_a), math.sqrt(1.0 - haversine_a))
    return earth_radius_meters * haversine_c

def get_db_connection():
    conn = sqlite3.connect(BUS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# 公車站牌核心查詢：一站一張卡片 + 合併所有路線
# ==========================================
def fetch_bus_data(user_latitude: float, user_longitude: float) -> list:
    if not os.path.exists(BUS_DB_PATH):
        print("【警告】找不到 bus.db 檔案，無法執行公車查詢！")
        return []

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        lat_min, lat_max = user_latitude - 0.01, user_latitude + 0.01
        lon_min, lon_max = user_longitude - 0.01, user_longitude + 0.01
        
        cursor.execute('''
            SELECT * FROM bus_stops 
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
        ''', (lat_min, lat_max, lon_min, lon_max))
        rows = cursor.fetchall()
        conn.close()

        raw_list = []
        for row in rows:
            dist = calculate_distance(user_latitude, user_longitude, float(row['lat']), float(row['lon']))
            try:
                routes = json.loads(row['passing_routes']) if row['passing_routes'] else []
            except Exception:
                routes = []
                
            raw_list.append({
                'uid': str(row['stop_uid']),
                'name': str(row['stop_name']),
                'lat': float(row['lat']),
                'lon': float(row['lon']),
                'routes': routes if isinstance(routes, list) else [],
                'dist': dist
            })

        raw_list.sort(key=lambda x: x['dist'])

        grouped_stops = {}
        for item in raw_list:
            stop_name = item['name']
            if stop_name not in grouped_stops:
                grouped_stops[stop_name] = {
                    'uid': item['uid'],
                    'name': stop_name,
                    'type': '🚏 公車站牌',
                    'latitude': item['lat'],
                    'longitude': item['lon'],
                    'distance': round(item['dist']),
                    'passing_routes': [],
                    'seen_routes': set(),
                    'stop_uids_map': {}
                }
            
            target = grouped_stops[stop_name]
            for r in item['routes']:
                if not isinstance(r, dict): continue
                r_name = str(r.get('RouteName', '')).strip()
                if not r_name: continue
                d_code = str(r.get('Direction', '0'))
                route_key = f"{r_name}_{d_code}"
                
                if route_key not in target['seen_routes']:
                    target['seen_routes'].add(route_key)
                    target['passing_routes'].append({
                        'RouteName': r_name,
                        'Direction': d_code,
                        'RouteStops': r.get('RouteStops', [])
                    })
                    target['stop_uids_map'][route_key] = item['uid']

        final_results = list(grouped_stops.values())
        final_results.sort(key=lambda x: x['distance'])
        return final_results[:5]
    except Exception as e:
        print(f"【fetch_bus_data 發生例外】: {e}\n{traceback.format_exc()}")
        return []


def fetch_aed_data(user_latitude: float, user_longitude: float) -> list:
    all_aeds = []
    for item in LOCAL_AED_DATABASE:
        if not isinstance(item, dict): continue
        try:
            raw_lat = item.get("地點LAT") or item.get("latitude") or item.get("lat") or 0.0
            raw_lng = item.get("地點LNG") or item.get("longitude") or item.get("lng") or 0.0
            lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        if lat > 90 and lng < 90: lat, lng = lng, lat
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        place_name = str(item.get("場所名稱") or "AED 急救站").strip()
        location_detail = str(item.get("AED放置地點") or item.get("AED地點描述") or "詳見現場標示").strip()
        all_aeds.append({
            "name": place_name, "type": "🆘 AED", "latitude": lat, "longitude": lng, 
            "distance": round(dist), "extra_info": f"📍 位置：{location_detail}"
        })
    all_aeds.sort(key=lambda x: x["distance"])
    
    results = []
    for item in all_aeds:
        is_dup = False
        clean_name = item["name"].replace("國際藝術村", "").replace("十字藝廊1樓", "").strip()
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            acc_clean = accepted["name"].replace("國際藝術村", "").replace("十字藝廊1樓", "").strip()
            if d < 30.0 or ((clean_name in acc_clean or acc_clean in clean_name) and len(clean_name) > 2):
                is_dup = True; break
        if not is_dup: results.append(item)
        if len(results) >= 5: break
    return results

def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    all_toilets = []
    for item in LOCAL_TOILET_DATABASE:
        if not isinstance(item, dict): continue
        raw_lat = str(item.get("latitude") or item.get("緯度") or item.get("Latitude") or item.get("Py") or item.get("Y") or 0).strip()
        raw_lng = str(item.get("longitude") or item.get("經度") or item.get("Longitude") or item.get("Px") or item.get("X") or 0).strip()
        try: lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError): continue
        if lat > 90 and lng < 90: lat, lng = lng, lat
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        raw_name = str(item.get("name") or item.get("公廁名稱") or item.get("名稱") or "公共廁所").strip()
        raw_addr = str(item.get("address") or item.get("公廁(廁所)地址") or item.get("詳細地址") or "").strip()
        if "河濱" in raw_name and raw_addr and not any(k in raw_addr for k in ["臺北", "新北", "縣", "市"]):
            full_name = f"{raw_name} ({raw_addr})"
        elif "河濱" in raw_addr and raw_name and not "河濱" in raw_name:
            full_name = f"{raw_addr} ({raw_name})"
        else:
            full_name = raw_name
        toilet_type = str(item.get("type") or item.get("公廁類別") or item.get("型態") or "").strip()
        grade = str(item.get("grade") or item.get("等級") or "優良").strip()
        tags = []
        item_text = str(item)
        if toilet_type: tags.append(toilet_type)
        if "夜市" in item_text or "友善店家" in item_text: tags.append("🌙夜市友善")
        if "無障礙" in item_text or "身障" in item_text: tags.append("♿無障礙")
        if "性別友善" in item_text or "通用" in item_text: tags.append("🌈性別友善")
        tag_str = " | ".join(dict.fromkeys(tags)) if tags else "一般公廁"
        try: diaper_count = int(item.get("diaper") or item.get("尿布台") or 0)
        except (ValueError, TypeError): diaper_count = 1 if "尿布" in item_text or "親子" in item_text else 0
        diaper_info = f"👶 尿布台：有 ({diaper_count}台)" if diaper_count > 0 else "👶 尿布台：無"
        all_toilets.append({
            "name": full_name, "type": f"🚻 公廁 ({tag_str})", "latitude": lat, "longitude": lng, 
            "distance": round(dist), "extra_info": f"📍 位置：{raw_addr if raw_addr else '詳見現場'}\n⭐ 評等：{grade} | {diaper_info}"
        })
    all_toilets.sort(key=lambda x: x["distance"])
    results = []
    for item in all_toilets:
        is_dup = False
        clean_name = item["name"].split("(")[0].replace("男廁", "").replace("女廁", "").replace("公廁", "").strip()
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            acc_clean = accepted["name"].split("(")[0].replace("男廁", "").replace("女廁", "").replace("公廁", "").strip()
            if d < 30.0 or ((clean_name in acc_clean or acc_clean in clean_name) and len(clean_name) > 2):
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
        if lat > 90 and lng < 90: lat, lng = lng, lat
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        place_name = str(item.get("場所名稱") or item.get("name") or "飲水機").strip()
        address = str(item.get("詳細地址") or item.get("address") or "詳見現場標示").strip()
        place_type = str(item.get("場所屬性") or "公共場所").strip()
        water_temp = str(item.get("水溫") or "未知").strip()
        if not water_temp or water_temp == "nan": water_temp = "常溫"
        all_water.append({
            "name": place_name, "type": f"💧 飲水機 ({place_type})", "latitude": lat, "longitude": lng, 
            "distance": round(dist), "extra_info": f"📍 地址：{address}\n🌡️ 提供水溫：{water_temp}"
        })
    all_water.sort(key=lambda x: x["distance"])
    results = []
    for item in all_water:
        is_dup = False
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            if d < 20.0 or item["name"] == accepted["name"]:
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
            if dist <= 3000.0:
                name = str(station.get("sna", "YouBike")).replace("YouBike2.0_", "")
                av = int(station.get("available_rent_bikes") or station.get("sbi") or 0)
                em = int(station.get("available_return_bikes") or station.get("bemp") or 0)
                youbike_results.append({
                    "name": name, "type": "🚲 YouBike", "latitude": st_lat, "longitude": st_lng, "distance": round(dist),
                    "extra_info": f"可借: {av} 輛 | 可還: {em} 格"
                })
    except Exception as e: print(f"YouBike API 讀取異常: {e}")
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
    user_id = event.source.user_id
    user_text = event.message.text
    
    if any(k in user_text for k in ["公車", "站牌", "搭車", "客運"]): target_type = "🚏 公車站牌"
    elif any(k in user_text for k in ["公廁", "廁所", "洗洗手問", "尿尿"]): target_type = "🚻 公廁"
    elif any(k in user_text for k in ["飲水機", "喝水", "裝水"]): target_type = "💧 飲水機"
    elif "AED" in user_text.upper() or "急救" in user_text: target_type = "🆘 AED"
    elif any(k in user_text.lower() for k in ["youbike", "腳踏車", "單車", "ubike"]): target_type = "🚲 YouBike"
    else: target_type = "🚻 公廁"
        
    user_search_state[user_id] = target_type

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


@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location_message(event):
    user_id = event.source.user_id
    user_latitude = event.message.latitude
    user_longitude = event.message.longitude
    target_type = user_search_state.get(user_id, "🚻 公廁")
    
    try:
        if target_type == "🚲 YouBike": search_results = fetch_youbike_data(user_latitude, user_longitude)
        elif target_type == "🚻 公廁": search_results = fetch_public_toilet_data(user_latitude, user_longitude)
        elif target_type == "🆘 AED": search_results = fetch_aed_data(user_latitude, user_longitude)
        elif target_type == "💧 飲水機": search_results = fetch_water_fountain_data(user_latitude, user_longitude)
        elif target_type == "🚏 公車站牌": search_results = fetch_bus_data(user_latitude, user_longitude)
        else: search_results = []

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            if not search_results:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"方圓1公里內找不到【{target_type}】。")])
                )
                return

            carousel_contents = {"type": "carousel", "contents": []}
            
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                safe_name = urllib.parse.quote(str(item['name']))
                
                if liff_id: liff_map_url = f"https://liff.line.me/{liff_id}?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"
                else: liff_map_url = f"https://my-line-lbs-bot.onrender.com/liff/map?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"

                badge_color = "#00B900"
                if "飲水機" in target_type: badge_color = "#00BFFF"
                elif "AED" in target_type: badge_color = "#FF3333"
                elif "公車" in target_type: badge_color = "#FF9900"

                body_contents = [
                    {"type": "text", "text": str(item["type"]), "weight": "bold", "size": "xs", "color": badge_color},
                    {"type": "text", "text": str(item["name"]), "weight": "bold", "size": "md", "margin": "xs", "wrap": True},
                    {"type": "text", "text": f"📏 距離：約 {item['distance']} 公尺", "size": "xs", "color": "#888888", "margin": "sm"}
                ]

                if target_type == "🚏 公車站牌":
                    routes_list = item.get('passing_routes', [])
                    
                    # 【方案 B 核心】卡片上僅顯示前 6 條車班
                    display_routes = routes_list[:6]
                    route_chunks = [display_routes[i:i + 2] for i in range(0, len(display_routes), 2)]
                    route_rows = []
                    
                    for chunk in route_chunks:
                        row_contents = []
                        for r in chunk:
                            r_name = str(r.get('RouteName', ''))
                            d_code = str(r.get('Direction', '0'))
                            dir_label = "去" if d_code == "0" else "回" if d_code == "1" else "單"
                            route_key = f"{r_name}_{d_code}"
                            
                            specific_uid = str(item.get('stop_uids_map', {}).get(route_key, item['uid']))
                            
                            row_contents.append({
                                "type": "box",
                                "layout": "vertical",
                                "backgroundColor": "#F0F4F8",
                                "cornerRadius": "md",
                                "paddingAll": "sm",
                                "flex": 1,
                                "action": {
                                    "type": "postback",
                                    "label": f"{r_name}({dir_label})",
                                    "data": f"action=bus_route&uid={specific_uid}&route={urllib.parse.quote(r_name)}&dir={d_code}"
                                },
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": f"🚌 {r_name} ({dir_label})",
                                        "size": "xs",
                                        "color": "#1E88E5",
                                        "align": "center",
                                        "weight": "bold",
                                        "wrap": True
                                    }
                                ]
                            })
                        
                        if len(chunk) == 1:
                            row_contents.append({
                                "type": "box",
                                "layout": "vertical",
                                "flex": 1
                            })
                        
                        route_rows.append({
                            "type": "box",
                            "layout": "horizontal",
                            "spacing": "sm",
                            "margin": "xs",
                            "contents": row_contents
                        })
                    
                    if route_rows:
                        # 若該站牌公車路線超過 6 條，底部加入「查看全部路線」按鈕
                        extra_button = []
                        if len(routes_list) > 6:
                            extra_button.append({
                                "type": "button",
                                "action": {
                                    "type": "postback",
                                    "label": f"🔍 查看此站全部 {len(routes_list)} 條車班",
                                    "data": f"action=all_bus_routes&uid={item['uid']}&stop_name={urllib.parse.quote(item['name'])}"
                                },
                                "style": "secondary",
                                "height": "sm",
                                "margin": "sm"
                            })

                        body_contents.append({
                            "type": "box",
                            "layout": "vertical",
                            "margin": "md",
                            "spacing": "xs",
                            "contents": [
                                {"type": "text", "text": "👇 點擊車班查看所有行經站牌", "size": "xs", "color": "#555555", "weight": "bold", "margin": "xs"},
                                *route_rows,
                                *extra_button
                            ]
                        })
                else:
                    body_contents.append({"type": "text", "text": str(item.get("extra_info", "")), "size": "sm", "color": "#333333", "margin": "md", "wrap": True})

                bubble = {
                    "type": "bubble", 
                    "size": "mega",
                    "body": {"type": "box", "layout": "vertical", "contents": body_contents},
                    "footer": {
                        "type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [
                            {"type": "button", "action": {"type": "uri", "label": "📍 地圖預覽", "uri": liff_map_url}, "style": "secondary", "height": "sm"},
                            {"type": "button", "action": {"type": "uri", "label": "🗺️ 開始導航", "uri": google_navigation_url}, "style": "primary", "color": badge_color, "height": "sm"}
                        ]
                    }
                }
                carousel_contents["contents"].append(bubble)
            
            flex_container = FlexContainer.from_dict(carousel_contents)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text=f"已找到附近的{target_type}", contents=flex_container)])
            )
    except Exception as e:
        print(f"【Location Exception Log】處理定位時發生異常:\n{traceback.format_exc()}")
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="查詢過程發生錯誤，請稍後再試。")])
            )


# ==========================================
# Postback 事件處理：生成全車班選單與路線動態圖
# ==========================================
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    parsed = urllib.parse.parse_qs(data)
    action = parsed.get('action', [''])[0]
    
    # 【功能 1】使用者點擊「查看此站全部車班」-> 回傳獨立的全車班雙欄 Flex 選單
    if action == 'all_bus_routes':
        uid = parsed.get('uid', [''])[0]
        stop_name = urllib.parse.unquote(parsed.get('stop_name', [''])[0])
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM bus_stops WHERE stop_name = ?', (stop_name,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows: return
        
        # 整合此站名的所有行經車班
        all_routes = []
        seen_keys = set()
        stop_uids_map = {}
        
        for r_row in rows:
            try:
                r_json = json.loads(r_row['passing_routes']) if r_row['passing_routes'] else []
            except Exception:
                r_json = []
                
            for r in r_json:
                if not isinstance(r, dict): continue
                r_name = str(r.get('RouteName', '')).strip()
                if not r_name: continue
                d_code = str(r.get('Direction', '0'))
                route_key = f"{r_name}_{d_code}"
                
                if route_key not in seen_keys:
                    seen_keys.add(route_key)
                    all_routes.append(r)
                    stop_uids_map[route_key] = str(r_row['stop_uid'])
        
        # 建立雙欄位點擊選單
        route_chunks = [all_routes[i:i + 2] for i in range(0, len(all_routes), 2)]
        route_rows = []
        
        for chunk in route_chunks:
            row_contents = []
            for r in chunk:
                r_name = str(r.get('RouteName', ''))
                d_code = str(r.get('Direction', '0'))
                dir_label = "去" if d_code == "0" else "回" if d_code == "1" else "單"
                route_key = f"{r_name}_{d_code}"
                
                specific_uid = stop_uids_map.get(route_key, uid)
                
                row_contents.append({
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F0F4F8",
                    "cornerRadius": "md",
                    "paddingAll": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": f"{r_name}({dir_label})",
                        "data": f"action=bus_route&uid={specific_uid}&route={urllib.parse.quote(r_name)}&dir={d_code}"
                    },
                    "contents": [
                        {
                            "type": "text",
                            "text": f"🚌 {r_name} ({dir_label})",
                            "size": "xs",
                            "color": "#1E88E5",
                            "align": "center",
                            "weight": "bold",
                            "wrap": True
                        }
                    ]
                })
            
            if len(chunk) == 1:
                row_contents.append({"type": "box", "layout": "vertical", "flex": 1})
            
            route_rows.append({
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "xs",
                "contents": row_contents
            })
            
        bubble = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#FF9900",
                "contents": [{"type": "text", "text": f"🚏 【{stop_name}】全站車班 ({len(all_routes)}條)", "color": "#FFFFFF", "weight": "bold", "size": "md"}]
            },
            "body": {
                "type": "box", "layout": "vertical", 
                "contents": [
                    {"type": "text", "text": "👇 點擊車班查看完整行經站牌：", "size": "xs", "color": "#555555", "margin": "xs", "weight": "bold"},
                    {"type": "box", "layout": "vertical", "margin": "md", "contents": route_rows}
                ]
            }
        }
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token, 
                    messages=[FlexMessage(alt_text=f"{stop_name}全車班選單", contents=FlexContainer.from_dict(bubble))]
                )
            )

    # 【功能 2】點擊任何車班 -> 生成動態路線時間軸
    elif action == 'bus_route':
        uid = parsed.get('uid', [''])[0]
        route_name = urllib.parse.unquote(parsed.get('route', [''])[0])
        dir_code = parsed.get('dir', [''])[0]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT stop_name, passing_routes FROM bus_stops WHERE stop_uid = ?', (uid,))
        row = cursor.fetchone()
        conn.close()
        
        if not row: return
        
        current_stop_name = str(row['stop_name'])
        try:
            routes = json.loads(row['passing_routes']) if row['passing_routes'] else []
        except Exception:
            routes = []
        
        target_route = None
        for r in routes:
            if not isinstance(r, dict): continue
            if str(r.get('RouteName', '')) == route_name and str(r.get('Direction', '')) == dir_code:
                target_route = r
                break
                
        if not target_route: return
        
        stops_list = target_route.get('RouteStops', [])
        try: current_idx = stops_list.index(current_stop_name)
        except ValueError: current_idx = 0
            
        start_idx = 0
        end_idx = len(stops_list)
        if len(stops_list) > 95:
            start_idx = max(0, current_idx - 40)
            end_idx = min(len(stops_list), start_idx + 95)
        
        timeline_contents = []
        if start_idx > 0:
            timeline_contents.append({"type": "text", "text": f"↑ ...省略前方 {start_idx} 站", "color": "#888888", "size": "xs", "margin": "sm"})

        for i in range(start_idx, end_idx):
            s_name = str(stops_list[i])
            
            icon = ""
            if any(k in s_name for k in ["捷運", "MRT"]): icon = " 🚇"
            elif any(k in s_name for k in ["火車", "車站", "台鐵"]): icon = " 🚆"
            elif "高鐵" in s_name: icon = " 🚄"
            
            if i < current_idx:
                timeline_contents.append({
                    "type": "text", "text": f"⚪ {s_name}{icon}", 
                    "color": "#BDBDBD", "weight": "regular", "size": "sm", "margin": "xs"
                })
            elif i == current_idx:
                timeline_contents.append({
                    "type": "text", "text": f"📍 {s_name}{icon} (當前站)", 
                    "color": "#FF0000", "weight": "bold", "size": "md", "margin": "sm"
                })
            else:
                timeline_contents.append({
                    "type": "text", "text": f"🔵 {s_name}{icon}", 
                    "color": "#333333", "weight": "regular", "size": "sm", "margin": "xs"
                })
                
        if end_idx < len(stops_list):
            timeline_contents.append({
                "type": "text", "text": f"↓ ...以及後續 {len(stops_list)-end_idx} 站", 
                "color": "#888888", "size": "xs", "margin": "sm"
            })
            
        dir_label = "去程" if dir_code == "0" else "回程" if dir_code == "1" else "單向"
        bubble = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#FF9900",
                "contents": [{"type": "text", "text": f"🚌 {route_name} ({dir_label})", "color": "#FFFFFF", "weight": "bold", "size": "lg"}]
            },
            "body": {
                "type": "box", "layout": "vertical", "contents": timeline_contents
            }
        }
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token, 
                    messages=[FlexMessage(alt_text=f"{route_name}路線圖", contents=FlexContainer.from_dict(bubble))]
                )
            )

if __name__ == "__main__":
    app.run(port=5000)