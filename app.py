import os
import math
import json
import requests
import urllib3
import urllib.parse
import sqlite3
import gzip
import shutil
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
# 核心載入：啟動時將本地資料庫載入記憶體 (JSON)
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
# 各項設施查詢邏輯
# ==========================================
def fetch_bus_data(user_latitude: float, user_longitude: float) -> list:
    if not os.path.exists(BUS_DB_PATH):
        return []

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 限制查詢範圍，極大化降低記憶體與運算量
    lat_min, lat_max = user_latitude - 0.01, user_latitude + 0.01
    lon_min, lon_max = user_longitude - 0.01, user_longitude + 0.01
    
    cursor.execute('''
        SELECT * FROM bus_stops 
        WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
    ''', (lat_min, lat_max, lon_min, lon_max))
    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        dist = calculate_distance(user_latitude, user_longitude, row['lat'], row['lon'])
        results.append({
            'uid': row['stop_uid'],
            'name': row['stop_name'],
            'type': '🚏 公車站牌',
            'latitude': row['lat'],
            'longitude': row['lon'],
            'passing_routes': json.loads(row['passing_routes']),
            'distance': round(dist)
        })

    results.sort(key=lambda x: x['distance'])
    return results[:5]

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
    elif any(k in user_text for k in ["公廁", "廁所", "洗手間", "尿尿"]): target_type = "🚻 公廁"
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


@handler.add(LocationMessageContent, message=LocationMessageContent)
@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location_message(event):
    user_id = event.source.user_id
    user_latitude = event.message.latitude
    user_longitude = event.message.longitude
    target_type = user_search_state.get(user_id, "🚻 公廁")
    
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
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"方圓3公里內找不到【{target_type}】。")])
            )
            return

        carousel_contents = {"type": "carousel", "contents": []}
        
        for item in search_results:
            google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
            safe_name = urllib.parse.quote(item['name'])
            
            if liff_id: liff_map_url = f"https://liff.line.me/{liff_id}?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"
            else: liff_map_url = f"https://my-line-lbs-bot.onrender.com/liff/map?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"

            badge_color = "#00B900"
            if "飲水機" in target_type: badge_color = "#00BFFF"
            elif "AED" in target_type: badge_color = "#FF3333"
            elif "公車" in target_type: badge_color = "#FF9900"

            body_contents = [
                {"type": "text", "text": item["type"], "weight": "bold", "size": "xs", "color": badge_color},
                {"type": "text", "text": item["name"], "weight": "bold", "size": "md", "margin": "xs", "wrap": True},
                {"type": "text", "text": f"📏 距離：約 {item['distance']} 公尺", "size": "xs", "color": "#888888", "margin": "sm"}
            ]

            if target_type == "🚏 公車站牌":
                route_buttons = []
                # 確保放入「所有」行經該站牌的車班 (LINE Box layout 支援上限 100 個子元素，絕對夠放)
                for r in item.get('passing_routes', [])[:95]: 
                    r_name = r['RouteName']
                    d_code = str(r.get('Direction', ''))
                    dir_label = "去" if d_code == "0" else "回" if d_code == "1" else "單"
                    route_buttons.append({
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": f"{r_name}({dir_label})",
                            "data": f"action=bus_route&uid={item['uid']}&route={urllib.parse.quote(r_name)}&dir={d_code}"
                        },
                        "style": "secondary",
                        "height": "sm",
                        "margin": "xs",
                        "flex": 0 
                    })
                
                if route_buttons:
                    body_contents.append({
                        "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": "👇 點擊車班查看所有行經站牌", "size": "xs", "color": "#555555"},
                            {"type": "box", "layout": "horizontal", "wrap": True, "contents": route_buttons}
                        ]
                    })
            else:
                body_contents.append({"type": "text", "text": item.get("extra_info", ""), "size": "sm", "color": "#333333", "margin": "md", "wrap": True})

            bubble = {
                "type": "bubble", "size": "kilo",
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


# ==========================================
# Postback 事件處理：生成完整的路線動態圖
# ==========================================
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    parsed = urllib.parse.parse_qs(data)
    
    if parsed.get('action', [''])[0] == 'bus_route':
        uid = parsed.get('uid', [''])[0]
        route_name = parsed.get('route', [''])[0]
        dir_code = parsed.get('dir', [''])[0]
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT stop_name, passing_routes FROM bus_stops WHERE stop_uid = ?', (uid,))
        row = cursor.fetchone()
        conn.close()
        
        if not row: return
        
        current_stop_name = row['stop_name']
        routes = json.loads(row['passing_routes'])
        
        target_route = None
        for r in routes:
            if r['RouteName'] == route_name and str(r.get('Direction', '')) == dir_code:
                target_route = r
                break
                
        if not target_route: return
        
        stops_list = target_route.get('RouteStops', [])
        try: current_idx = stops_list.index(current_stop_name)
        except ValueError: current_idx = 0
            
        # 顯示全部站牌 (設定防護機制，若單一路線站牌數超過 95 站，則優先顯示當前與未來站牌以防 LINE 限制報錯)
        start_idx = 0
        end_idx = len(stops_list)
        if len(stops_list) > 95:
            start_idx = max(0, current_idx - 40)
            end_idx = min(len(stops_list), start_idx + 95)
        
        timeline_contents = []
        if start_idx > 0:
            timeline_contents.append({"type": "text", "text": f"↑ ...省略前方 {start_idx} 站", "color": "#888888", "size": "xs", "margin": "sm"})

        for i in range(start_idx, end_idx):
            s_name = stops_list[i]
            
            icon = ""
            if any(k in s_name for k in ["捷運", "MRT"]): icon = " 🚇"
            elif any(k in s_name for k in ["火車", "車站", "台鐵"]): icon = " 🚆"
            elif "高鐵" in s_name: icon = " 🚄"
            
            # 視覺化判定 (過去淡化、當前紅色、未來黑色)
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