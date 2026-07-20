import os
import json
import time
import threading
import schedule
import requests
import gspread
import yt_dlp
import gradio as gr

# --- إعدادات البيئة ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# روابط الـ API
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GOOGLE_SHEET_NAME = "tiktok"
WORKSHEET_NAME = "sheet1"
SENT_LINKS_SHEET = "sent_links"

status_text = "البوت قيد التشغيل..."

# --- الدوال المساعدة ---
class SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

def load_memory(gc):
    try:
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(SENT_LINKS_SHEET)
        # استخدام get_all_values بدلاً من col_values لأنها أسرع وأكثر استقراراً مع الأعداد الكبيرة
        all_rows = sheet.get_all_values()
        # استخراج الرابط الأول من كل سطر وتصفية الخانات الفارغة
        links = set(row[0] for row in all_rows if row and row[0].strip())
        print(f"📦 تم تحميل {len(links)} رابط من الذاكرة بنجاح.")
        return links
    except Exception as e:
        print(f"❌ خطأ فادح في تحميل الذاكرة: {e}")
        # نرجع None بدلاً من set() فارغة لكي نعرف أن هناك خطأ حقيقي حدث
        return None

def save_to_memory(gc, link):
    try:
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(SENT_LINKS_SHEET)
        sheet.append_row([link])
    except Exception as e:
        print(f"خطأ حفظ الذاكرة: {e}")

def send_telegram_video(video_url, caption):
    url = f"{TELEGRAM_API_URL}/sendVideo"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "video": video_url, "caption": caption, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=60)
        return res.status_code == 200
    except Exception as e:
        print(f"خطأ إرسال الفيديو: {e}")
        return False

def send_telegram_photos(images, caption):
    url = f"{TELEGRAM_API_URL}/sendMediaGroup"
    media = [{"type": "photo", "media": img} for img in images[:10]]
    media[0]["caption"] = caption
    media[0]["parse_mode"] = "HTML"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media)}
    try:
        res = requests.post(url, json=payload, timeout=60)
        return res.status_code == 200
    except Exception as e:
        print(f"خطأ إرسال الصور: {e}")
        return False

def fetch_tiktok_videos(username):
    ydl_opts = {'extract_flat': True, 'quiet': True, 'playlistend': 6, 'logger': SilentLogger()}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
            return info.get('entries', [])
    except Exception as e:
        print(f"فشل جلب فيديوهات @{username}: {e}")
        return []

def fetch_tikwm_data(link):
    try:
        res = requests.get(f"https://www.tikwm.com/api/?url={link}", timeout=15)
        return res.json().get('data')
    except Exception as e:
        print(f"فشل جلب بيانات TikWM: {e}")
        return None

# --- الوظيفة الرئيسية ---
def main_job():
    global status_text
    try:
        if not GOOGLE_CREDENTIALS_JSON:
            status_text = "خطأ: GOOGLE_CREDENTIALS_JSON مفقود"
            return
        
        creds = json.loads(GOOGLE_CREDENTIALS_JSON)
        gc = gspread.service_account_from_dict(creds)
        
        # جلب الذاكرة
        sent_memory = load_memory(gc)
        
        # حماية: إذا فشل جلب الذاكرة بسبب القيود، أوقف الدورة فوراً ولا ترسل شيئاً مكرراً
        if sent_memory is None:
            print("⚠️ تم إيقاف الدورة مؤقتاً لعدم القدرة على قراءة الذاكرة (تجنباً للتكرار).")
            return
        
        sheet = gc.open(GOOGLE_SHEET_NAME).worksheet(WORKSHEET_NAME)
        users = [u for u in sheet.col_values(1) if u.lower() != 'username' and u.strip()]
        
        for username in users:
            try:
                print(f"\n🔍 فحص الحساب: @{username}")
                entries = fetch_tiktok_videos(username)
                
                for entry in entries:
                    video_id = entry.get('id')
                    if not video_id: continue
                    link = f"https://www.tiktok.com/@{username}/video/{video_id}"
                    
                    if link in sent_memory: continue
                    
                    data = fetch_tikwm_data(link)
                    if not data: continue
                    
                    author = data.get('music_info', {}).get('author', username)
                    caption = f"🎥 <b>{author}</b>\n🔗 <a href='{link}'>رابط الفيديو</a>"
                    
                    sent_ok = False
                    if data.get('images'):
                        sent_ok = send_telegram_photos(data['images'], caption)
                    else:
                        sent_ok = send_telegram_video(data.get('play'), caption)
                    
                    if sent_ok:
                        print(f"   ✅ تم إرسال فيديو جديد: {link}")
                        sent_memory.add(link)
                        save_to_memory(gc, link)
                        time.sleep(3) # تأخير لتجنب الحظر
            except Exception as e:
                print(f"خطأ مع المستخدم @{username}: {e}")
                continue
        status_text = "تم الفحص بنجاح"
    except Exception as e:
        status_text = f"خطأ عام: {e}"
        print(status_text)

# --- التشغيل ---
def run_schedule():
    print("🚀 بدء تشغيل البوت...")
    main_job()
    schedule.every(30).minutes.do(main_job)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    # تشغيل المهمة في الخلفية
    threading.Thread(target=run_schedule, daemon=True).start()
    # تشغيل واجهة Gradio لضمان بقاء السيرفر نشطاً
    demo = gr.Interface(fn=lambda: status_text, inputs=[], outputs="text")
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get('PORT', 7860)))
