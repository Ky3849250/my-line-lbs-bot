import os
import math
import json
import requests
import urllib3
import urllib.parse
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
from linebot.v3.webhooks import MessageEvent, TextMessageContent, LocationMessageContent

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
# 核心載入：啟動時將三大本地資料庫載入記憶體
# ==========================================
LOCAL_AED_DATABASE = []
LOCAL_TOILET_DATABASE = []
LOCAL_WATER_DATABASE = []

BASE_DIR = os.path.dirname(__file__)
AED_JSON_PATH = os.path.join(BASE_DIR, "aed_formatted.json")
TOILET_JSON_PATH = os.path.join(BASE_DIR, "toilet.json")
WATER_JSON_PATH = os.path.join(BASE_DIR, "water_fountains_fixed_2_completed.json")

# 1. 載入 AED 全量資料
if os.path.exists(AED_JSON_PATH):
    try:
        with open(AED_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_AED_DATABASE = json.load(f)
            print(f"【成功載入】AED 資料庫共 {len(LOCAL_AED_DATABASE)} 筆紀錄。")
    except Exception as e:
        print(f"【載入失敗】AED 讀取異常: {e}")

# 2. 載入公廁全量資料
if os.path.exists(TOILET_JSON_PATH):
    try:
        with open(TOILET_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_TOILET_DATABASE = json.load(f)
            print(f"【成功載入】公廁資料庫共 {len(LOCAL_TOILET_DATABASE)} 筆紀錄。")
    except Exception as e:
        print(f"【載入失敗】公廁讀取異常: {e}")

# 3. 載入飲水機資料
if os.path.exists(WATER_JSON_PATH):
    try:
        with open(WATER_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_WATER_DATABASE = json.load(f)
            print(f"【成功載入】飲水機資料庫共 {len(LOCAL_WATER_DATABASE)} 筆紀錄。")
    except Exception as e:
        print(f"【載入失敗】飲水機讀取異常: {e}")


def calculate_distance(origin_latitude: float, origin_longitude: float, 
                       destination_latitude: float, destination_longitude: float) -> float:
    """ 使用 Haversine 公式計算球面直線距離（公尺） """
    earth_radius_meters = 6371000.0
    phi_origin = math.radians(origin_latitude)
    phi_destination = math.radians(destination_latitude)
    delta_phi = math.radians(destination_latitude - origin_latitude)
    delta_lambda = math.radians(destination_longitude - origin_longitude)
    
    haversine_a = (math.sin(delta_phi / 2.0) ** 2 + 
                   math.cos(phi_origin) * math.cos(phi_destination) * math.sin(delta_lambda / 2.0) ** 2)
    haversine_c = 2.0 * math.atan2(math.sqrt(haversine_a), math.sqrt(1.0 - haversine_a))
    return earth_radius_meters * haversine_c


def fetch_aed_data(user_latitude: float, user_longitude: float) -> list:
    all_aeds = []
    source_data = LOCAL_AED_DATABASE
    
    for item in source_data:
        if not isinstance(item, dict): continue
        try:
            raw_lat = item.get("地點LAT") or item.get("latitude") or item.get("lat") or 0.0
            raw_lng = item.get("地點LNG") or item.get("longitude") or item.get("lng") or 0.0
            lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError):
            continue
            
        if lat > 90 and lng < 90:
            lat, lng = lng, lat
            
        if lat == 0.0 or lng == 0.0: continue
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        
        place_name = str(item.get("場所名稱") or "AED 急救站").strip()
        location_detail = str(item.get("AED放置地點") or item.get("AED地點描述") or "詳見現場標示").strip()
        
        all_aeds.append({
            "name": place_name, 
            "type": "🆘 AED",
            "latitude": lat, 
            "longitude": lng, 
            "distance": round(dist),
            "extra_info": f"📍 位置：{location_detail}"
        })

    all_aeds.sort(key=lambda x: x["distance"])
    
    # 雙重去重邏輯：(A)空間距離<30m (B)名稱包含
    results = []
    for item in all_aeds:
        is_dup = False
        clean_name = item["name"].replace("國際藝術村", "").replace("十字藝廊1樓", "").strip()
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            acc_clean = accepted["name"].replace("國際藝術村", "").replace("十字藝廊1樓", "").strip()
            if d < 30.0 or ((clean_name in acc_clean or acc_clean in clean_name) and len(clean_name) > 2):
                is_dup = True
                break
        if not is_dup:
            results.append(item)
        if len(results) >= 5:
            break
            
    return results


def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    all_toilets = []
    source_data = LOCAL_TOILET_DATABASE

    for item in source_data:
        if not isinstance(item, dict): continue
        
        raw_lat = str(item.get("latitude") or item.get("緯度") or item.get("Latitude") or item.get("Py") or item.get("Y") or 0).strip()
        raw_lng = str(item.get("longitude") or item.get("經度") or item.get("Longitude") or item.get("Px") or item.get("X") or 0).strip()
        
        try:
            lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError):
            continue
            
        if lat > 90 and lng < 90:
            lat, lng = lng, lat
            
        if lat == 0.0 or lng == 0.0: continue
            
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        
        raw_name = str(item.get("name") or item.get("公廁名稱") or item.get("公廁(廁所)名稱") or item.get("名稱") or "公共廁所").strip()
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
        
        try:
            diaper_count = int(item.get("diaper") or item.get("尿布台") or 0)
        except (ValueError, TypeError):
            diaper_count = 1 if "尿布" in item_text or "親子" in item_text else 0
            
        diaper_info = f"👶 尿布台：有 ({diaper_count}台)" if diaper_count > 0 else "👶 尿布台：無"

        all_toilets.append({
            "name": full_name, 
            "type": f"🚻 公廁 ({tag_str})",
            "latitude": lat, 
            "longitude": lng, 
            "distance": round(dist),
            "extra_info": f"📍 位置：{raw_addr if raw_addr else '詳見現場'}\n⭐ 評等：{grade} | {diaper_info}"
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
                is_dup = True
                break
        if not is_dup:
            results.append(item)
        if len(results) >= 5:
            break

    return results


def fetch_water_fountain_data(user_latitude: float, user_longitude: float) -> list:
    """ 飲水機全局精確比對：同樣支援距離與地點名稱去重 """
    all_water = []
    source_data = LOCAL_WATER_DATABASE

    for item in source_data:
        if not isinstance(item, dict): continue
        
        raw_lat = str(item.get("緯度") or item.get("latitude") or 0).strip()
        raw_lng = str(item.get("經度") or item.get("longitude") or 0).strip()
        
        try:
            lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError):
            continue
            
        if lat > 90 and lng < 90:
            lat, lng = lng, lat
            
        if lat == 0.0 or lng == 0.0: continue
            
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        
        place_name = str(item.get("場所名稱") or item.get("name") or "飲水機").strip()
        address = str(item.get("詳細地址") or item.get("address") or "詳見現場標示").strip()
        place_type = str(item.get("場所屬性") or "公共場所").strip()
        water_temp = str(item.get("水溫") or "未知").strip()
        
        if not water_temp or water_temp == "nan": water_temp = "常溫"

        all_water.append({
            "name": place_name, 
            "type": f"💧 飲水機 ({place_type})",
            "latitude": lat, 
            "longitude": lng, 
            "distance": round(dist),
            "extra_info": f"📍 地址：{address}\n🌡️ 提供水溫：{water_temp}"
        })

    all_water.sort(key=lambda x: x["distance"])

    results = []
    for item in all_water:
        is_dup = False
        for accepted in results:
            d = calculate_distance(item["latitude"], item["longitude"], accepted["latitude"], accepted["longitude"])
            if d < 20.0 or item["name"] == accepted["name"]:
                is_dup = True
                break
        if not is_dup:
            results.append(item)
        if len(results) >= 5:
            break

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
                    "name": name, "type": "🚲 YouBike",
                    "latitude": st_lat, "longitude": st_lng, "distance": round(dist),
                    "extra_info": f"可借: {av} 輛 | 可還: {em} 格"
                })
    except Exception as e:
        print(f"YouBike API 讀取異常: {e}")
        
    youbike_results.sort(key=lambda x: x["distance"])
    return youbike_results[:5]


@app.route("/liff/map", methods=['GET'])
def liff_map_page():
    return render_template("map.html")


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    
    # 加入對飲水機的意圖辨識
    if any(k in user_text for k in ["公廁", "廁所", "洗手間", "尿尿"]):
        target_type = "🚻 公廁"
    elif any(k in user_text for k in ["飲水機", "喝水", "裝水"]):
        target_type = "💧 飲水機"
    elif "AED" in user_text.upper() or "急救" in user_text:
        target_type = "🆘 AED"
    elif any(k in user_text.lower() for k in ["youbike", "腳踏車", "單車", "ubike"]):
        target_type = "🚲 YouBike"
    else:
        target_type = "🚻 公廁"  # 預設
        
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
    
    # 根據意圖執行不同資料檢索函式
    if target_type == "🚲 YouBike":
        search_results = fetch_youbike_data(user_latitude, user_longitude)
    elif target_type == "🚻 公廁":
        search_results = fetch_public_toilet_data(user_latitude, user_longitude)
    elif target_type == "🆘 AED":
        search_results = fetch_aed_data(user_latitude, user_longitude)
    elif target_type == "💧 飲水機":
        search_results = fetch_water_fountain_data(user_latitude, user_longitude)
    else:
        search_results = []

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if not search_results:
            reply_messages = [TextMessage(text=f"方圓3公里內找不到【{target_type}】。")]
        else:
            carousel_contents = {"type": "carousel", "contents": []}
            
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                safe_name = urllib.parse.quote(item['name'])
                
                # 自動判斷使用 liff 還是 web url (根據你的環境變數)
                if liff_id:
                    liff_map_url = f"https://liff.line.me/{liff_id}?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"
                else:
                    liff_map_url = f"https://my-line-lbs-bot.onrender.com/liff/map?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"

                # 徽章顏色依賴於服務類型
                badge_color = "#00B900"
                if "飲水機" in target_type: badge_color = "#00BFFF"
                elif "AED" in target_type: badge_color = "#FF3333"

                body_contents = [
                    {"type": "text", "text": item["type"], "weight": "bold", "size": "xs", "color": badge_color},
                    {"type": "text", "text": item["name"], "weight": "bold", "size": "md", "margin": "xs", "wrap": True},
                    {"type": "text", "text": f"📏 距離：約 {item['distance']} 公尺", "size": "xs", "color": "#888888", "margin": "sm"},
                    {"type": "text", "text": item["extra_info"], "size": "sm", "color": "#333333", "margin": "md", "wrap": True}
                ]
                
                bubble = {
                    "type": "bubble",
                    "size": "kilo",
                    "body": {"type": "box", "layout": "vertical", "contents": body_contents},
                    "footer": {
                        "type": "box", 
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "button", 
                                "action": {
                                    "type": "uri", 
                                    "label": "📍 地圖預覽", 
                                    "uri": liff_map_url
                                }, 
                                "style": "secondary", 
                                "height": "sm"
                            },
                            {
                                "type": "button", 
                                "action": {
                                    "type": "uri", 
                                    "label": "🗺️ 開始導航", 
                                    "uri": google_navigation_url
                                }, 
                                "style": "primary", 
                                "color": badge_color, 
                                "height": "sm"
                            }
                        ]
                    }
                }
                carousel_contents["contents"].append(bubble)
            
            flex_container = FlexContainer.from_dict(carousel_contents)
            reply_messages = [FlexMessage(alt_text=f"已找到附近的{target_type}", contents=flex_container)]
        
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=reply_messages)
        )

if __name__ == "__main__":
    app.run(port=5000)