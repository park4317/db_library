"""Archive Telegram DB_library posts in Notion without an AI dependency."""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get("YOUTUBE_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("YOUTUBE_TELEGRAM_CHAT_ID")
NOTION_KEY = os.environ.get("NOTION_API_KEY")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
STATE_FILE = Path(__file__).parent / "state" / "last_update_id.json"


def headers():
    return {
        "Authorization": f"Bearer {NOTION_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def safe_error(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP_{exc.response.status_code}"
    return type(exc).__name__


def notion_query(payload):
    response = requests.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        headers=headers(), json=payload, timeout=20,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def message_key(post):
    return f"{post['chat_id']}:{post['message_id']}"


def saved(post):
    return bool(notion_query({"filter": {
        "property": "Telegram Message ID",
        "rich_text": {"equals": message_key(post)},
    }}))


def source_link(post):
    chat_id = post["chat_id"]
    if chat_id.startswith("-100") and post["message_id"]:
        return f"https://t.me/c/{chat_id[4:]}/{post['message_id']}"
    return None


def body_blocks(text):
    # Notion accepts up to 100 children per create request; fail rather than truncate.
    pieces = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    if len(pieces) > 100:
        raise ValueError("TELEGRAM_BODY_TOO_LONG")
    return [{"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"text": {"content": piece}}]
    }} for piece in pieces]


def archive(post):
    if saved(post):
        return "duplicate"
    text = post["text"]
    title = next((line.strip() for line in text.splitlines() if line.strip()), "Telegram 첨부 게시물")
    title = re.sub(r"^\*+", "", title).strip()[:200] or "Telegram 첨부 게시물"
    now = datetime.now(timezone.utc).isoformat()
    properties = {
        "Title": {"title": [{"text": {"content": title}}]},
        "채널명": {"rich_text": [{"text": {"content": post["chat_title"][:200]}}]},
        "수집일": {"date": {"start": now}},
        "요약": {"rich_text": [{"text": {"content": "SUMMARY_PENDING"}}]},
        "구분": {"select": {"name": "메모"}},
        "Telegram Message ID": {"rich_text": [{"text": {"content": message_key(post)}}]},
        "Telegram Channel ID": {"rich_text": [{"text": {"content": post["chat_id"]}}]},
        "Source Type": {"rich_text": [{"text": {"content": "TELEGRAM_CONTEXT"}}]},
        "Attachment Present": {"checkbox": bool(post["attachment_types"])},
    }
    if post["posted_at"]:
        properties["게시일"] = {"date": {
            "start": datetime.fromtimestamp(int(post["posted_at"]), timezone.utc).isoformat()
        }}
    link = source_link(post)
    if link:
        properties["URL"] = {"url": link}
    body = text
    if post["attachment_types"]:
        body = f"[첨부자료 있음: {', '.join(post['attachment_types'])}]\n\n{text}"
    response = requests.post(
        "https://api.notion.com/v1/pages", headers=headers(), timeout=30,
        json={"parent": {"database_id": DATABASE_ID}, "properties": properties,
              "children": body_blocks(body)},
    )
    response.raise_for_status()
    if not saved(post):
        raise RuntimeError("NOTION_READBACK_FAILED")
    return "created"


def fetch_updates(offset):
    response = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params={"offset": offset + 1, "timeout": 0}, timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("ok") is not True:
        raise RuntimeError("TELEGRAM_FETCH_FAILED")
    updates = payload.get("result", [])
    posts = []
    for update in sorted(updates, key=lambda item: item.get("update_id", 0)):
        message = update.get("channel_post") or update.get("message") or {}
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if CHAT_ID and chat_id != str(CHAT_ID):
            continue
        attachment_types = [key for key in (
            "photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker"
        ) if message.get(key)]
        text = (message.get("text") or message.get("caption") or "").strip()
        if not text and not attachment_types:
            continue
        posts.append({
            "chat_id": chat_id,
            "chat_title": chat.get("title") or "DB_library",
            "message_id": str(message.get("message_id", "")),
            "posted_at": message.get("date"),
            "attachment_types": attachment_types,
            "text": text or f"[텍스트 없는 첨부 게시물: {', '.join(attachment_types)}]",
        })
    max_update_id = max((item.get("update_id", 0) for item in updates), default=offset)
    return posts, max_update_id


def main():
    missing = [name for name, value in {
        "YOUTUBE_TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "NOTION_API_KEY": NOTION_KEY,
        "NOTION_DATABASE_ID": DATABASE_ID,
    }.items() if not value]
    if missing:
        print(f"INGEST_FAIL category=ENV_MISSING names={','.join(missing)}")
        return 1
    try:
        notion_query({"page_size": 1})
        offset = 0
        if STATE_FILE.exists():
            offset = int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_update_id", 0))
        posts, max_update_id = fetch_updates(offset)
        created = duplicate = failed = 0
        for post in posts:
            try:
                outcome = archive(post)
                created += outcome == "created"
                duplicate += outcome == "duplicate"
            except Exception as exc:
                failed += 1
                print(f"INGEST_ITEM_FAIL category={safe_error(exc)}")
        if failed:
            print(f"INGEST_FAIL processed={len(posts)} created={created} duplicate={duplicate} failed={failed} checkpoint_unchanged=YES")
            return 1
        STATE_FILE.parent.mkdir(exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last_update_id": max_update_id}), encoding="utf-8")
        temporary.replace(STATE_FILE)
        print(f"INGEST_PASS processed={len(posts)} created={created} duplicate={duplicate} checkpoint={max_update_id}")
        return 0
    except Exception as exc:
        print(f"INGEST_FAIL category={safe_error(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

