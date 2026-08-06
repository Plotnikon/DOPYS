import json
import os
import pathlib
import re
import subprocess
import tempfile

import requests
import yt_dlp

# ─────────────────────────────────────────────
#  НАЛАШТУВАННЯ
# ─────────────────────────────────────────────

MAX_FILE_BYTES = 48 * 1024 * 1024  # запас під ліміт Telegram у 50 МБ
MIN_VIDEO_KBPS = 150  # нижче цього стискати вже немає сенсу — якість буде непридатна

# ─────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
OWNER_TG_ID = os.environ.get("OWNER_TG_ID", "").strip()  # опційно, але рекомендовано

STATE_FILE = pathlib.Path("download_state.json")
URL_RE = re.compile(r'https?://\S+')

IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXT = {"mp4", "webm", "mkv", "mov"}


def get_updates(offset):
    r = requests.get(
        f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def tg_json(method, payload):
    payload = {k: v for k, v in payload.items() if v is not None}
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=payload,
        timeout=60,
    )
    if not r.ok:
        print(f"  ! Telegram {method}: {r.text[:200]}")
    return r.ok


def tg_send_file(method, field_name, chat_id, file_path):
    with open(file_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
            data={"chat_id": chat_id},
            files={field_name: f},
            timeout=180,
        )
    if not r.ok:
        print(f"  ! Telegram {method}: {r.text[:200]}")
    return r.ok


def download_media(url, tmp_dir):
    """Тягне медіа за посиланням через yt-dlp у найкращій якості. Повертає шлях до файлу."""
    ydl_opts = {
        "outtmpl": f"{tmp_dir}/%(id)s.%(ext)s",
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        mp4_path = base + ".mp4"
        if os.path.exists(mp4_path):
            filename = mp4_path
    return filename


def get_duration_seconds(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"  ! Не вдалось визначити тривалість: {e}")
        return None


def compress_video(input_path, output_path, target_bytes, duration_seconds):
    """Перекодовує відео під заданий розмір через розрахований бітрейт. Повертає True/False."""
    audio_kbps = 96
    target_kbits = target_bytes * 8 / 1000
    video_kbps = int(target_kbits / duration_seconds - audio_kbps)

    if video_kbps < MIN_VIDEO_KBPS:
        return False

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-c:v", "libx264", "-preset", "fast",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                output_path,
            ],
            check=True, capture_output=True, timeout=600,
        )
        return os.path.exists(output_path)
    except Exception as e:
        print(f"  ! Помилка стиснення: {e}")
        return False


def handle_link(chat_id, url):
    print(f"  -> Обробляю посилання: {url}")
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            filename = download_media(url, tmp_dir)
        except Exception as e:
            print(f"  ! Помилка завантаження: {e}")
            tg_json("sendMessage", {
                "chat_id": chat_id,
                "text": f"Не вдалось завантажити це посилання 😕\n{str(e)[:300]}",
            })
            return

        if not os.path.exists(filename):
            tg_json("sendMessage", {
                "chat_id": chat_id,
                "text": "Не вдалось знайти медіа за цим посиланням.",
            })
            return

        ext = filename.rsplit(".", 1)[-1].lower()
        size = os.path.getsize(filename)

        if size > MAX_FILE_BYTES:
            if ext in VIDEO_EXT:
                print(f"  -> Файл {size // 1024 // 1024} МБ, пробую стиснути...")
                duration = get_duration_seconds(filename)
                compressed_path = os.path.join(tmp_dir, "compressed.mp4")

                if duration and compress_video(filename, compressed_path, MAX_FILE_BYTES, duration):
                    filename = compressed_path
                    ext = "mp4"
                    size = os.path.getsize(filename)
                    print(f"  -> Стиснуто до {size // 1024 // 1024} МБ")
                else:
                    tg_json("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Відео задовге — навіть після стиснення не влізе в ліміт Telegram-бота (50 МБ) з прийнятною якістю.",
                    })
                    return
            else:
                tg_json("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"Файл завеликий для Telegram-бота ({size // 1024 // 1024} МБ, ліміт 50 МБ) — не можу надіслати.",
                })
                return

        if size > MAX_FILE_BYTES:
            tg_json("sendMessage", {
                "chat_id": chat_id,
                "text": "Навіть після стиснення файл все ще завеликий — не можу надіслати.",
            })
            return

        if ext in VIDEO_EXT:
            ok = tg_send_file("sendVideo", "video", chat_id, filename)
        elif ext in IMAGE_EXT:
            ok = tg_send_file("sendPhoto", "photo", chat_id, filename)
        else:
            ok = tg_send_file("sendDocument", "document", chat_id, filename)

        if not ok:
            tg_json("sendMessage", {
                "chat_id": chat_id,
                "text": "Завантажив файл, але не вдалось надіслати його в Telegram.",
            })


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    last_update_id = state.get("last_update_id", 0)

    updates = get_updates(last_update_id + 1)
    if not updates:
        print("Нових повідомлень немає.")
        return

    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message")
        if not message:
            continue

        chat_id = message["chat"]["id"]
        from_id = message.get("from", {}).get("id")

        if OWNER_TG_ID and str(from_id) != OWNER_TG_ID:
            print(f"  - Ігноровано повідомлення не від власника (from_id={from_id})")
            continue

        text = message.get("text") or message.get("caption") or ""
        url_match = URL_RE.search(text)
        if not url_match:
            continue

        handle_link(chat_id, url_match.group(0))

    state["last_update_id"] = last_update_id
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
