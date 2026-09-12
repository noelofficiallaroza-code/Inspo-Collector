import json
import os
import re
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]
IDEAS_THREAD_ID = os.environ.get("IDEAS_THREAD_ID", "").strip()

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_sheets_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_state_and_data_worksheets(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        state_ws = sh.worksheet("State")
    except gspread.WorksheetNotFound:
        state_ws = sh.add_worksheet(title="State", rows=2, cols=2)
        state_ws.update("A1", [["last_update_id"]])
        state_ws.update("A2", [["0"]])

    data_ws = sh.sheet1
    return state_ws, data_ws


def get_last_update_id(state_ws):
    value = state_ws.acell("A2").value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def set_last_update_id(state_ws, update_id):
    state_ws.update("A2", [[str(update_id)]])


def detect_platform(link):
    if re.search(r"youtube\.com|youtu\.be", link, re.IGNORECASE):
        return "youtube"
    if re.search(r"instagram\.com", link, re.IGNORECASE):
        return "instagram"
    if re.search(r"tiktok\.com", link, re.IGNORECASE):
        return "tiktok"
    if re.search(r"facebook\.com|fb\.watch", link, re.IGNORECASE):
        return "facebook"
    return None


def extract_youtube_video_id(link):
    match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", link)
    return match.group(1) if match else None


def fetch_youtube_metadata(video_id):
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet,statistics",
            "id": video_id,
            "key": YOUTUBE_API_KEY,
        },
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return None
    video = items[0]
    return {
        "title": video["snippet"]["title"],
        "thumbnail": video["snippet"]["thumbnails"]["high"]["url"],
        "views": video["statistics"].get("viewCount", ""),
        "likes": video["statistics"].get("likeCount", ""),
    }


def fetch_ytdlp_metadata(link):
    import yt_dlp

    opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(link, download=False)

    return {
        "title": info.get("title", ""),
        "thumbnail": info.get("thumbnail", ""),
        "views": info.get("view_count", ""),
        "likes": info.get("like_count", ""),
    }


def get_sender_name(message):
    frm = message.get("from") or {}
    name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")]))
    return name or frm.get("username") or "Unknown"


def process_message(message):
    text = message.get("text") or message.get("caption") or ""
    url_match = URL_RE.search(text)
    if not url_match:
        return None

    link = url_match.group(0)
    platform = detect_platform(link)
    if not platform:
        return None

    if IDEAS_THREAD_ID:
        thread_id = message.get("message_thread_id")
        if str(thread_id) != IDEAS_THREAD_ID:
            return None

    sender = get_sender_name(message)
    date_shared = datetime.fromtimestamp(message["date"], tz=timezone.utc).strftime("%Y-%m-%d")
    notes = text.replace(link, "").strip()

    try:
        if platform == "youtube":
            video_id = extract_youtube_video_id(link)
            metadata = fetch_youtube_metadata(video_id) if video_id else None
        else:
            metadata = fetch_ytdlp_metadata(link)
    except Exception as exc:  # noqa: BLE001 - log and skip, don't crash the whole run
        print(f"Failed to fetch metadata for {link}: {exc}")
        metadata = None

    if not metadata:
        metadata = {"title": "", "thumbnail": "", "views": "", "likes": ""}

    return [
        date_shared,
        sender,
        platform,
        link,
        metadata["title"],
        f'=IMAGE("{metadata["thumbnail"]}")' if metadata["thumbnail"] else "",
        metadata["views"],
        metadata["likes"],
        notes,
        "",
    ]


def main():
    gc = get_sheets_client()
    state_ws, data_ws = get_state_and_data_worksheets(gc)
    last_update_id = get_last_update_id(state_ws)

    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        params={"offset": last_update_id + 1, "timeout": 0},
        timeout=30,
    )
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    if not updates:
        print("No new Telegram messages.")
        return

    rows_to_append = []
    highest_update_id = last_update_id

    for update in updates:
        highest_update_id = max(highest_update_id, update["update_id"])
        message = update.get("message") or update.get("edited_message") or update.get("channel_post")
        if not message:
            continue

        row = process_message(message)
        if row:
            rows_to_append.append(row)

    if rows_to_append:
        data_ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"Appended {len(rows_to_append)} row(s).")
    else:
        print("No supported links in this batch.")

    set_last_update_id(state_ws, highest_update_id)


if __name__ == "__main__":
    main()
