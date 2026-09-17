import os
import math
import requests
from flask import Flask, request, abort
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    TextMessage, QuickReply, QuickReplyItem, LocationAction,
    FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, LocationMessageContent

load_dotenv()
app = Flask(__name__)

channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)

# 儲存使用者選擇的查詢類別 (Key: user_id, Value: target_type)
user_search_state = {}

def calculate_distance(origin_latitude: float, origin_longitude: float, 
                       destination_latitude: float, destination_longitude: float) -> float:
    """
    使用 Haversine 公式計算兩組經緯度之間的直線物理距離（單位：公尺）
    """
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
    """ 擷取 YouBike 2.0 即時開放資料 """
    youbike_results = []
    try:
        youbike_api_url = "https://tcgbusfs.blob.core.windows.net/dotapp/youbike/v2/youbike_immediate.json"
        response = requests.get(youbike_api_url, timeout=5)
        response.raise_for_status()
        youbike_data_list = response.json()
        
        for station in youbike_data_list:
            raw_latitude = station.get("latitude") or station.get("lat") or 0.0
            raw_longitude = station.get("longitude") or station.get("lng") or 0.0
            
            station_latitude = float(raw_latitude)
            station_longitude = float(raw_longitude)
            
            if station_latitude == 0.0 or station_longitude == 0.0:
                continue
            
            distance_meters = calculate_distance(
                user_latitude, user_longitude, 
                station_latitude, station_longitude
            )
            
            if distance_meters <= 1000.0:
                raw_station_name = station.get("sna", "YouBike 站點")
                formatted_station_name = raw_station_name.replace("YouBike2.0_", "")
                
                available_bikes = int(station.get("available_rent_bikes") or station.get("sbi") or 0)
                empty_spaces = int(station.get("available_return_bikes") or station.get("bemp") or 0)
                
                youbike_results.append({
                    "name": formatted_station_name,
                    "type": "🚲 YouBike",
                    "latitude": station_latitude,
                    "longitude": station_longitude,
                    "distance": round(distance_meters),
                    "extra_info": f"可借: {available_bikes} 輛 | 可還: {empty_spaces} 格",
                    "is_available": available_bikes > 0
                })
    except Exception as error_exception:
        print(f"YouBike API 讀取異常: {error_exception}")
        
    return youbike_results


def fetch_public_toilet_data(user_latitude: float, user_longitude: float) -> list:
    """ 擷取台北市公廁開放資料 """
    toilet_results = []
    try:
        public_toilet_api_url = "https://data.taipei/api/v1/dataset/ca205b54-a06f-4d84-894c-d6ab5079ce79?scope=resourceAquire&limit=1000"
        response = requests.get(public_toilet_api_url, timeout=5)
        response.raise_for_status()
        data_json = response.json()
        
        toilet_data_list = data_json.get("result", {}).get("results", [])
        
        for toilet in toilet_data_list:
            raw_latitude = toilet.get("緯度") or toilet.get("latitude") or 0.0
            raw_longitude = toilet.get("經度") or toilet.get("longitude") or 0.0
            
            toilet_latitude = float(raw_latitude)
            toilet_longitude = float(raw_longitude)
            
            if toilet_latitude == 0.0 or toilet_longitude == 0.0:
                continue
                
            distance_meters = calculate_distance(
                user_latitude, user_longitude, 
                toilet_latitude, toilet_longitude
            )
            
            if distance_meters <= 1000.0:
                toilet_name = toilet.get("公廁名稱") or "公共廁所"
                toilet_grade = toilet.get("等級") or "優良"
                
                toilet_results.append({
                    "name": toilet_name,
                    "type": "🚻 公廁",
                    "latitude": toilet_latitude,
                    "longitude": toilet_longitude,
                    "distance": round(distance_meters),
                    "extra_info": f"環境評等: {toilet_grade}",
                    "is_available": True
                })
    except Exception as error_exception:
        print(f"公廁 API 讀取異常: {error_exception}")
        
    return toilet_results


def fetch_aed_data(user_latitude: float, user_longitude: float) -> list:
    """ 擷取台北市 AED 自動體外心臟去顫器設置點開放資料 """
    aed_results = []
    try:
        aed_api_url = "https://data.taipei/api/v1/dataset/cd050577-115f-4299-b37a-012ff490a632?scope=resourceAquire&limit=1000"
        response = requests.get(aed_api_url, timeout=5)
        response.raise_for_status()
        data_json = response.json()
        
        aed_data_list = data_json.get("result", {}).get("results", [])
        
        for aed in aed_data_list:
            raw_latitude = aed.get("緯度") or aed.get("latitude") or 0.0
            raw_longitude = aed.get("經度") or aed.get("longitude") or 0.0
            
            aed_latitude = float(raw_latitude)
            aed_longitude = float(raw_longitude)
            
            if aed_latitude == 0.0 or aed_longitude == 0.0:
                continue
                
            distance_meters = calculate_distance(
                user_latitude, user_longitude, 
                aed_latitude, aed_longitude
            )
            
            if distance_meters <= 1000.0:
                aed_place_name = aed.get("場所名稱") or "AED 急救站"
                aed_location_description = aed.get("AED放置地點") or aed.get("AED地點描述") or "詳見現場標示"
                
                aed_results.append({
                    "name": aed_place_name,
                    "type": "🆘 AED",
                    "latitude": aed_latitude,
                    "longitude": aed_longitude,
                    "distance": round(distance_meters),
                    "extra_info": f"放置位置: {aed_location_description}",
                    "is_available": True
                })
    except Exception as error_exception:
        print(f"AED API 讀取異常: {error_exception}")
        
    return aed_results


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
    
    # 根據使用者選擇的目標類別，呼叫對應的 API 擷取真實資料
    if target_type == "🚲 YouBike":
        search_results = fetch_youbike_data(user_latitude, user_longitude)
    elif target_type == "🚻 公廁":
        search_results = fetch_public_toilet_data(user_latitude, user_longitude)
    elif target_type == "🆘 AED":
        search_results = fetch_aed_data(user_latitude, user_longitude)
    else:
        search_results = []

    # 依照距離遠近進行排序，並截取前 10 筆最近結果
    search_results.sort(key=lambda item: item["distance"])
    search_results = search_results[:10]
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if not search_results:
            reply_messages = [TextMessage(text=f"方圓 1 公里內找不到【{target_type}】。")]
        else:
            carousel_contents = {"type": "carousel", "contents": []}
            
            for item in search_results:
                google_navigation_url = f"https://www.google.com/maps/dir/?api=1&destination={item['latitude']},{item['longitude']}"
                
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
                        "contents": [
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