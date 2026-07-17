import sys
import os
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)

import gspread
import requests
import time
import schedule
import threading
import json
import logging
import yt_dlp
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_NAME = "tiktok"
WORKSHEET_NAME = "sheet1"
SENT_LINKS_SHEET = "sent_links"
WORKER_BASE_URL = "https://fragrant-snow-cfba.jassimkassim9.workers.dev"
status_text = "البوت متوقف حالياً"

def load_memory(gc):
    try:
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(SENT_LINKS_SHEET)
        return set(filter(None, sheet.col_values(1)))
    except Exception as e:
        print(f"خطأ في تحميل الذاكرة: {e}")
        return set()

def save_to_memory(gc, link):
    try:
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(SENT_LINKS_SHEET)
        next_row = len(sheet.col_values(1)) + 1
        sheet.update_cell(next_row, 1, link)
    except Exception as e:
        print(f"خطأ في حفظ الذاكرة: {e}")

def send_telegram_video(video_url, caption):
    url = f"{WORKER_BASE_URL}/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "video": video_url, "caption": caption}
    try:
        requests.post(url, json=payload, timeout=60)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"خطأ في إرسال الفيديو: {e}")
        return False

def send_telegram_photos(images, caption):
    url = f"{WORKER_BASE_URL}/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    media = [{"type": "photo", "media": img} for img in images[:10]]
    media[0]["caption"] = caption
    payload = {"chat_id": TELEGRAM_CHAT_ID, "media": media}
    try:
        requests.post(url, json=payload, timeout=60)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"خطأ في إرسال الصور: {e}")
        return False

class SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

def fetch_tiktok_videos(username):
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'playlistend': 6,
        'logger': SilentLogger(),
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
            if info and 'entries' in info:
                entries = [e for e in info['entries'] if e]
                if entries:
                    return entries
        return []
    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        if "does not have any videos" in err:
            print(f"   ⚪ @{username} — لا يوجد فيديوهات")
        elif "private" in err:
            print(f"   🔒 @{username} — حساب خاص")
        elif "Unable to extract" in err:
            print(f"   🔒 @{username} — حساب خاص أو مخفي")
        else:
            print(f"   ❌ @{username} — فشل الجلب")
        return []
    except Exception:
        return []

def fetch_tikwm_data(link):
    for attempt in range(1, 4):
        try:
            res = requests.get(f"https://www.tikwm.com/api/?url={link}", timeout=15)
            data = res.json().get('data', {})
            if data:
                return data
            print(f"   ⚠️ tikwm رجع بيانات فارغة (محاولة {attempt}/3)")
        except Exception as e:
            print(f"   ❌ خطأ tikwm (محاولة {attempt}/3): {e}")
        time.sleep(3 * attempt)
    return None

def main_job():
    global status_text
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            return

        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        sent_memory = load_memory(gc)
        
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(WORKSHEET_NAME)
        users = sheet.col_values(1)
        if users and users[0].lower() == 'username':
            users = users[1:]

        for username in users:
            username = username.strip()
            if not username:
                continue
            
            # --- بداية التعديل: إضافة try-except لكل مستخدم ---
            try:
                print(f"\n🔍 فحص الحساب: @{username}")
                entries = fetch_tiktok_videos(username)
                
                if not entries:
                    continue

                for entry in entries:
                    video_id = entry.get('id')
                    if not video_id: continue
                    link = f"https://www.tiktok.com/@{username}/video/{video_id}"

                    if link in sent_memory:
                        continue

                    data = fetch_tikwm_data(link)
                    if not data: continue

                    # ... (بقية كود الإرسال الخاص بك) ...
                    # تأكد من أنك تضع منطق الإرسال هنا كما هو
                    
                    sent_memory.add(link)
                    save_to_memory(gc, link)
                    
                    # إضافة تأخير بسيط لتجنب الحظر من تليجرام أو تيك توك
                    time.sleep(2) 

            except Exception as e:
                print(f"❌ خطأ أثناء معالجة @{username}: {e}")
                continue # هذا السطر يضمن استمرار البوت للمستخدم التالي
            # --- نهاية التعديل ---

    except Exception as e:
        status_text = f"❌ خطأ عام: {str(e)}"
        print(status_text)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(status_text.encode('utf-8'))
    def log_message(self, format, *args):
        pass

def run_server():
    # Render يعطينا المنفذ عبر متغير PORT، إذا لم يوجد نستخدم 7860
    port = int(os.environ.get('PORT', 7860))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    print(f"✅ Web server يعمل على port {port}")
    server.serve_forever()

def run_schedule():
    print("⏳ انتظار 30 ثانية...")
    time.sleep(30)
    print("🚀 بدء تشغيل البوت...")
    main_job()
    schedule.every(60).minutes.do(main_job)
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=run_schedule, daemon=True).start()
threading.Thread(target=run_server, daemon=True).start()
print("✅ البوت والسيرفر يعملان في الخلفية")

# ✅ هذا السطر هو الحل — يخلي الـ main thread يشتغل للأبد
while True:
    time.sleep(60)
