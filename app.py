import os
import json
import time
import glob
import shutil
import tempfile
import threading
import schedule
import requests
import gspread
import yt_dlp
import gradio as gr

# --- إضافة مسار Deno لبيئة تشغيل Render لفك حماية تيك توك ---
if "/opt/render/project/.deno/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"/opt/render/project/.deno/bin:{os.environ.get('PATH', '')}"

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

def send_telegram_video_file(path, caption):
    """يرفع ملف فيديو محلي مباشرة إلى تيليجرام (multipart) بدل تمرير رابط."""
    url = f"{TELEGRAM_API_URL}/sendVideo"
    try:
        with open(path, "rb") as f:
            res = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"video": f},
                timeout=180,
            )
        if res.status_code != 200:
            print(f"⚠️ تيليجرام رفض الفيديو: {res.status_code} {res.text[:200]}")
        return res.status_code == 200
    except Exception as e:
        print(f"خطأ إرسال الفيديو: {e}")
        return False

def send_telegram_photos_files(paths, caption):
    """يرفع مجموعة صور محلية مباشرة إلى تيليجرام (multipart) بدل تمرير روابط."""
    url = f"{TELEGRAM_API_URL}/sendMediaGroup"
    media = []
    files = {}
    opened = []
    try:
        for i, p in enumerate(paths[:10]):
            attach_name = f"photo{i}"
            f = open(p, "rb")
            opened.append(f)
            files[attach_name] = f
            media.append({"type": "photo", "media": f"attach://{attach_name}"})
        media[0]["caption"] = caption
        media[0]["parse_mode"] = "HTML"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media)}
        res = requests.post(url, data=payload, files=files, timeout=180)
        if res.status_code != 200:
            print(f"⚠️ تيليجرام رفض الصور: {res.status_code} {res.text[:200]}")
        return res.status_code == 200
    except Exception as e:
        print(f"خطأ إرسال الصور: {e}")
        return False
    finally:
        for f in opened:
            f.close()

def fetch_tiktok_videos(username):
    """يجلب قائمة آخر الفيديوهات لحساب معيّن (بدون تحميل)."""
    ydl_opts = {'extract_flat': True, 'quiet': True, 'playlistend': 6, 'logger': SilentLogger()}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{username}", download=False)
            return info.get('entries', [])
    except Exception as e:
        print(f"فشل جلب فيديوهات @{username}: {e}")
        return []

def download_post(link, workdir):
    """
    يحمّل منشور تيك توك (فيديو أو سلايدشو صور) محلياً مباشرة عبر yt-dlp،
    بدل الاعتماد على tikwm كوسيط. يرجّع (النوع, قائمة الملفات, اسم الناشر).
    النوع: 'video' أو 'images' أو None عند الفشل.

    منشورات السلايدشو غالباً لا تملك "format" فيديو حقيقي عند تيك توك،
    والصور تكون متاحة فقط كـ thumbnails، لذلك نفحص المنشور أولاً قبل
    تقرير طريقة التحميل المناسبة.
    """
    probe_opts = {
        'quiet': True,
        'logger': SilentLogger(),
        'skip_download': True,
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(link, download=False)
    except Exception as e:
        print(f"   ⚠️ فشل تحليل المنشور عبر yt-dlp: {e}")
        return None, [], None

    if not info:
        return None, [], None

    author = info.get('uploader') or info.get('creator') or info.get('channel')
    post_id = info.get('id', '')
    formats = info.get('formats') or []
    has_real_video = any((f.get('vcodec') not in (None, 'none')) for f in formats)

    # الحالة 1: فيديو حقيقي -> نحمّله عادي
    if has_real_video:
        video_opts = {
            'quiet': True,
            'logger': SilentLogger(),
            'outtmpl': os.path.join(workdir, '%(id)s.%(ext)s'),
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
            'retries': 2,
        }
        try:
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                ydl.download([link])
            files = sorted(glob.glob(os.path.join(workdir, f"{post_id}*")))
            videos = [f for f in files if f.lower().endswith(('.mp4', '.mov', '.webm', '.mkv'))]
            if videos:
                return 'video', videos, author
        except Exception as e:
            print(f"   ⚠️ فشل تحميل الفيديو: {e}")
            # نكمل ونجرب أسلوب الصور احتياطاً

    # الحالة 2: سلايدشو صور -> نحمّل الـ thumbnails (كل شريحة = صورة)
    images_opts = {
        'quiet': True,
        'logger': SilentLogger(),
        'outtmpl': os.path.join(workdir, '%(id)s.%(ext)s'),
        'skip_download': True,
        'writethumbnail': True,
        'write_all_thumbnails': True,
        'noplaylist': True,
        'retries': 2,
    }
    try:
        with yt_dlp.YoutubeDL(images_opts) as ydl:
            ydl.download([link])
    except Exception as e:
        print(f"   ⚠️ فشل تحميل صور السلايدشو: {e}")
        return None, [], author

    files = sorted(glob.glob(os.path.join(workdir, f"{post_id}*")))
    images = [f for f in files if f.lower().endswith(('.jpg', '.jpeg', '.webp', '.png'))]
    if images:
        return 'images', images, author

    # لم نجد لا فيديو ولا صور - نطبع بنية المعلومات للمساعدة بالتشخيص لاحقاً
    print(f"   ❓ تعذّر تحديد نوع المنشور {link} — formats موجودة: {len(formats)}, "
          f"thumbnails: {len(info.get('thumbnails') or [])}")
    return None, [], author

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

                    with tempfile.TemporaryDirectory() as workdir:
                        kind, files, author = download_post(link, workdir)

                        if not kind or not files:
                            print(f"   ⏭️ تعذّر تحميل: {link}")
                            continue

                        caption = f"🎥 <b>{author or username}</b>\n🔗 <a href='{link}'>رابط الفيديو</a>"

                        if kind == 'video':
                            sent_ok = send_telegram_video_file(files[0], caption)
                        else:
                            sent_ok = send_telegram_photos_files(files, caption)

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
