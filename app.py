import os
import math
import json
import requests
import urllib3
import urllib.parse
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
# 核心載入：啟動時將 AED 與公廁資料載入記憶體
# ==========================================
LOCAL_AED_DATABASE = []
LOCAL_TOILET_DATABASE = []

BASE_DIR = os.path.dirname(__file__)
AED_JSON_PATH = os.path.join(BASE_DIR, "aed.json")
TOILET_JSON_PATH = os.path.join(BASE_DIR, "toilet.json")

if os.path.exists(AED_JSON_PATH):
    try:
        with open(AED_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_AED_DATABASE = json.load(f)
            print(f"【成功載入】全台 AED 資料庫共 {len(LOCAL_AED_DATABASE)} 筆紀錄。")
    except Exception as e:
        print(f"【載入失敗】aed.json 讀取異常: {e}")

if os.path.exists(TOILET_JSON_PATH):
    try:
        with open(TOILET_JSON_PATH, "r", encoding="utf-8") as f:
            LOCAL_TOILET_DATABASE = json.load(f)
            print(f"【成功載入】全台公廁資料庫共 {len(LOCAL_TOILET_DATABASE)} 筆紀錄。")
    except Exception as e:
        print(f"【載入失敗】toilet.json 讀取異常: {e}")


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
    
    if not source_data:
        try:
            url = "https://tw-aed.mohw.gov.tw/openData?t=json"
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=5, verify=False)
            if response.status_code == 200:
                source_data = response.json()
        except Exception as e:
            print(f"線上 AED API 請求失敗: {e}")

    for item in source_data:
        if not isinstance(item, dict): continue
        try:
            raw_lat = str(item.get("地點LAT") or item.get("latitude") or item.get("lat") or 0).strip()
            raw_lng = str(item.get("地點LNG") or item.get("longitude") or item.get("lng") or 0).strip()
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
            "extra_info": f"位置: {location_detail}"
        })

    # 最單純的邏輯：依距離排序，直接取前 5 個
    all_aeds.sort(key=lambda x: x["distance"])
    return all_aeds[:5]


def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    all_toilets = []
    source_data = LOCAL_TOILET_DATABASE

    for item in source_data:
        if not isinstance(item, dict): continue
        
        # 讀取經緯度
        raw_lat = str(item.get("latitude") or item.get("緯度") or item.get("Latitude") or item.get("Py") or item.get("Y") or 0).strip()
        raw_lng = str(item.get("longitude") or item.get("經度") or item.get("Longitude") or item.get("Px") or item.get("X") or 0).strip()
        
        try:
            lat, lng = float(raw_lat), float(raw_lng)
        except (ValueError, TypeError):
            continue
            
        # 經緯度錯置防護
        if lat > 90 and lng < 90:
            lat, lng = lng, lat
            
        if lat == 0.0 or lng == 0.0: continue
            
        # 計算與用戶的距離
        dist = calculate_distance(user_latitude, user_longitude, lat, lng)
        
        # 讀取顯示資訊
        place_name = str(item.get("name") or item.get("公廁名稱") or item.get("公廁(廁所)名稱") or item.get("名稱") or "公共廁所").strip()
        address = str(item.get("address") or item.get("公廁(廁所)地址") or "").strip()
        toilet_type = str(item.get("type") or item.get("公廁類別") or item.get("型態") or "").strip()
        grade = str(item.get("grade") or item.get("等級") or "未評定").strip()
        
        try:
            diaper_count = int(item.get("diaper") or item.get("尿布台") or 0)
        except (ValueError, TypeError):
            diaper_count = 1 if "尿布" in str(item) else 0
            
        diaper_info = f"👶 尿布台：有 ({diaper_count}台)" if diaper_count > 0 else "👶 尿布台：無"
        
        # 組裝成清單
        all_toilets.append({
            "name": place_name, 
            "type": f"🚻 公廁 ({toilet_type})" if toilet_type else "🚻 公廁",
            "latitude": lat, 
            "longitude": lng, 
            "distance": round(dist),
            "extra_info": f"📍 {address if address else '無地址'}\n⭐ 評等：{grade} | {diaper_info}"
        })

    # 最單純的邏輯：依距離排序，直接取前 5 個
    all_toilets.sort(key=lambda x: x["distance"])
    return all_toilets[:5]


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
        
    # 最單純的邏輯：依距離排序，直接取前 5 個
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
    
    if "公廁" in user_text:
        target_type = "🚻 公廁"
    elif "AED" in user_text:
        target_type = "🆘 AED"
    elif "YouBike" in user_text or "腳踏車" in user_text:
        target_type = "🚲 YouBike"
    else:
        target_type = "🚻 公廁"
        
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
    
    if target_type == "🚲 YouBike":
        search_results = fetch_youbike_data(user_latitude, user_longitude)
    elif target_type == "🚻 公廁":
        search_results = fetch_public_toilet_data(user_latitude, user_longitude)
    elif target_type == "🆘 AED":
        search_results = fetch_aed_data(user_latitude, user_longitude)
    else:
        search_results = []

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if not search_results:
            reply_messages = [TextMessage(text=f"附近找不到【{target_type}】。")]
        else:
            carousel_contents = {"type": "carousel", "contents": []}
            
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                safe_name = urllib.parse.quote(item['name'])
                
                if liff_id:
                    liff_map_url = f"https://liff.line.me/{liff_id}?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"
                else:
                    liff_map_url = f"https://my-line-lbs-bot.onrender.com/liff/map?lat={item['latitude']}&lng={item['longitude']}&name={safe_name}"

                body_contents = [
                    {"type": "text", "text": item["type"], "weight": "bold", "size": "xs", "color": "#00B900"},
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
                                "color": "#00B900", 
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