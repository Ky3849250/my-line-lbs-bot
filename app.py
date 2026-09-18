import os
import math
import requests
import urllib3
import urllib.parse  # 新增：用來將中文字轉換為網址安全編碼
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

# 關閉 SSL 不安全連線警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
app = Flask(__name__)

channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
liff_id = (os.getenv('LINE_LIFF_ID') or '').strip()

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)

user_search_state = {}

# 偽裝成一般瀏覽器，避免被政府網站的防機器人機制阻擋
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def safe_parse_json_list(raw_json_data) -> list:
    if isinstance(raw_json_data, list):
        return raw_json_data
    elif isinstance(raw_json_data, dict):
        result = raw_json_data.get("result")
        if isinstance(result, dict):
            return result.get("results", [])
        elif isinstance(result, list):
            return result
        elif "results" in raw_json_data and isinstance(raw_json_data["results"], list):
            return raw_json_data["results"]
        elif "data" in raw_json_data and isinstance(raw_json_data["data"], list):
            return raw_json_data["data"]
    return []

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


def fetch_youbike_data(user_latitude: float, user_longitude: float) -> list:
    youbike_results = []
    try:
        youbike_api_url = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"
        response = requests.get(youbike_api_url, headers=REQUEST_HEADERS, timeout=5, verify=False)
        response.raise_for_status()
        youbike_data_list = safe_parse_json_list(response.json())
        
        for station in youbike_data_list:
            if not isinstance(station, dict): continue
            raw_lat = station.get("latitude") or station.get("lat") or 0.0
            raw_lng = station.get("longitude") or station.get("lng") or 0.0
            st_lat, st_lng = float(raw_lat), float(raw_lng)
            
            if st_lat == 0.0 or st_lng == 0.0: continue
            
            distance_meters = calculate_distance(user_latitude, user_longitude, st_lat, st_lng)
            if distance_meters <= 3000.0:
                formatted_name = str(station.get("sna", "YouBike")).replace("YouBike2.0_", "")
                av_bikes = int(station.get("available_rent_bikes") or station.get("sbi") or 0)
                em_spaces = int(station.get("available_return_bikes") or station.get("bemp") or 0)
                youbike_results.append({
                    "name": formatted_name, "type": "🚲 YouBike",
                    "latitude": st_lat, "longitude": st_lng, "distance": round(distance_meters),
                    "extra_info": f"可借: {av_bikes} 輛 | 可還: {em_spaces} 格"
                })
    except Exception as e:
        print(f"YouBike API 讀取異常: {e}")
    return youbike_results


def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    toilet_results = []
    try:
        public_toilet_api_url = "https://data.taipei/api/v1/dataset/ca205b54-a06f-4d84-894c-d6ab5079ce79?scope=resourceAquire&limit=10000"
        response = requests.get(public_toilet_api_url, headers=REQUEST_HEADERS, timeout=10, verify=False)
        response.raise_for_status()
        toilet_data_list = safe_parse_json_list(response.json())
        
        for toilet in toilet_data_list:
            if not isinstance(toilet, dict): continue
            raw_lat = toilet.get("緯度") or toilet.get("latitude") or 0.0
            raw_lng = toilet.get("經度") or toilet.get("longitude") or 0.0
            t_lat, t_lng = float(raw_lat), float(raw_lng)
            
            if t_lat == 0.0 or t_lng == 0.0: continue
                
            distance_meters = calculate_distance(user_latitude, user_longitude, t_lat, t_lng)
            if distance_meters <= 3000.0:
                toilet_results.append({
                    "name": str(toilet.get("公廁名稱") or "公共廁所"), "type": "🚻 公廁",
                    "latitude": t_lat, "longitude": t_lng, "distance": round(distance_meters),
                    "extra_info": f"環境評等: {toilet.get('等級') or '未知'}"
                })
    except Exception as e:
        print(f"公廁 API 讀取異常: {e}")
    return toilet_results


def fetch_aed_data(user_latitude: float, user_longitude: float) -> list:
    aed_results = []
    try:
        aed_api_url = "https://data.taipei/api/v1/dataset/cd050577-115f-4299-b37a-012ff490a632?scope=resourceAquire&limit=10000"
        response = requests.get(aed_api_url, headers=REQUEST_HEADERS, timeout=10, verify=False)
        response.raise_for_status()
        aed_data_list = safe_parse_json_list(response.json())
        
        for aed in aed_data_list:
            if not isinstance(aed, dict): continue
            raw_lat = aed.get("緯度") or aed.get("latitude") or 0.0
            raw_lng = aed.get("經度") or aed.get("longitude") or 0.0
            a_lat, a_lng = float(raw_lat), float(raw_lng)
            
            if a_lat == 0.0 or a_lng == 0.0: continue
                
            distance_meters = calculate_distance(user_latitude, user_longitude, a_lat, a_lng)
            if distance_meters <= 3000.0:
                aed_results.append({
                    "name": str(aed.get("場所名稱") or "AED 急救站"), "type": "🆘 AED",
                    "latitude": a_lat, "longitude": a_lng, "distance": round(distance_meters),
                    "extra_info": f"位置: {aed.get('AED放置地點') or '詳見現場標示'}"
                })
    except Exception as e:
        print(f"AED API 讀取異常: {e}")
    return aed_results


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

    search_results.sort(key=lambda item: item["distance"])
    search_results = search_results[:10]
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if not search_results:
            reply_messages = [TextMessage(text=f"方圓 3 公里內找不到【{target_type}】。")]
        else:
            carousel_contents = {"type": "carousel", "contents": []}
            
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                
                # 【關鍵修復】使用 urllib.parse.quote 將中文字進行 URL 安全編碼
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