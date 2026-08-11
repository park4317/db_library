"""
텔레그램에 공유된 유튜브 링크 → 자막 추출 → Claude 요약/태깅 → Notion DB 저장

여기(Claude 채팅 sandbox)는 youtube.com 네트워크가 막혀 있어 실행 테스트를 못했습니다.
Claude Code / GitHub Actions 환경에서 테스트하세요.

필요 환경변수 (GitHub Secrets 또는 .env):
  YOUTUBE_TELEGRAM_BOT_TOKEN   # 기존 뉴스봇과 별도의 새 봇 (BotFather로 새로 발급)
  YOUTUBE_TELEGRAM_CHAT_ID     # (선택) 전용 채널/그룹 chat_id — 지정 시 다른 채팅은 무시
  ANTHROPIC_API_KEY
  NOTION_API_KEY
  NOTION_DATABASE_ID

※ 기존 텔레그램 뉴스봇과 완전히 분리된 봇/채널을 쓰도록 변수명을 다르게 뒀습니다.
  같은 GitHub repo에 뉴스봇 워크플로우가 있어도 시크릿 이름이 겹치지 않습니다.

사용법:
  python youtube_to_notion.py            # 텔레그램 폴링 → 새 유튜브 링크 처리 (state/last_update_id.json 사용)
  python youtube_to_notion.py --once URL # 단발 테스트, 텔레그램 없이 URL 하나만 바로 처리
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TELEGRAM_BOT_TOKEN = os.environ.get("YOUTUBE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("YOUTUBE_TELEGRAM_CHAT_ID")  # 선택: 지정 시 이 chat만 처리
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "last_update_id.json"

YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|live/|shorts/)|youtu\.be/)([\w-]{11})"
)

TAG_OPTIONS = [
    "매크로", "반도체 소부장", "AI인프라", "2차전지", "방산", "가상자산", "전략·백테스트", "기타",
]

# "폴더" 역할 — 영상 하나당 하나만 선택 (Notion Board 뷰의 Group by 기준)
CATEGORY_OPTIONS = [
    "일반시황", "반도체", "제약·바이오", "이차전지", "방산", "AI인프라", "가상자산", "전략·백테스트", "기타",
]


# ---------- 1. 텔레그램에서 새 메시지 가져오기 ----------

def get_telegram_updates():
    STATE_DIR.mkdir(exist_ok=True)
    last_update_id = 0
    if STATE_FILE.exists():
        last_update_id = json.loads(STATE_FILE.read_text()).get("last_update_id", 0)

    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        params={"offset": last_update_id + 1, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    urls = []
    max_update_id = last_update_id
    for u in updates:
        max_update_id = max(max_update_id, u["update_id"])

        # 채널 게시물은 "channel_post"로, 일반 그룹/DM 메시지는 "message"로 옴 — 둘 다 확인
        message = u.get("channel_post") or u.get("message") or {}
        if not message:
            continue

        # 전용 채널/그룹 chat_id를 지정해둔 경우, 다른 채팅(예: 개인 DM)은 무시
        if TELEGRAM_CHAT_ID:
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

        text = message.get("text", "") or ""
        for m in YOUTUBE_URL_RE.finditer(text):
            urls.append(f"https://www.youtube.com/watch?v={m.group(1)}")

    if max_update_id != last_update_id:
        STATE_FILE.write_text(json.dumps({"last_update_id": max_update_id}))

    return urls


# ---------- 2. 비디오 메타데이터 (oEmbed — API 키 불필요) ----------

def get_video_metadata(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    resp = requests.get(
        "https://www.youtube.com/oembed",
        params={"url": url, "format": "json"},
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "title": data.get("title", video_id),
            "channel": data.get("author_name", ""),
            "url": url,
        }
    # 라이브 방송 등 oEmbed가 막힌 경우 최소 정보만
    return {"title": video_id, "channel": "", "url": url}


# ---------- 3. 자막 추출 ----------

def get_transcript(video_id: str) -> str | None:
    try:
        # youtube-transcript-api v1.0+ 부터는 클래스 메서드가 아니라 인스턴스를 만들어서 써야 함
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)

        transcript = None
        try:
            transcript = transcript_list.find_transcript(["ko"])
        except NoTranscriptFound:
            try:
                transcript = transcript_list.find_transcript(["en"]).translate("ko")
            except NoTranscriptFound:
                transcript = next(iter(transcript_list), None)

        if transcript is None:
            return None

        fetched = transcript.fetch()
        return " ".join(seg["text"] for seg in fetched.to_raw_data())
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
        print(f"[자막 없음] {video_id}: {e}")
        return None
    except Exception as e:
        print(f"[자막 추출 실패] {video_id}: {e}")
        return None


# ---------- 4. Claude로 요약 + 태깅 ----------

def summarize_and_tag(title: str, channel: str, transcript: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""다음은 유튜브 영상의 자막 원문입니다. KB자산운용 ETF 상품기획 담당자가
월간 인사이트 자료와 투자 판단에 참고하기 위해 저장하는 라이브러리용 요약을 작성하세요.

제목: {title}
채널: {channel}

자막 원문(일부 생략될 수 있음):
{transcript[:12000]}

다음 JSON 형식으로만 답하세요 (설명 없이 JSON만):
{{
  "summary": "3~5문장 한국어 요약. 핵심 주장/데이터/전망 위주로.",
  "category": "아래 카테고리 중 영상의 주된 주제에 가장 가까운 것 딱 하나만 (폴더 분류용): {", ".join(CATEGORY_OPTIONS)}",
  "tags": ["category를 보완하는 세부 키워드 0~3개, 자유 서술 가능. 예: HBM, 양극재, 금리인하"],
  "priority": "상|중|하 (월간 인사이트 자료 활용 가치 기준)"
}}

주제가 여러 개 섞여 있어도 category는 반드시 하나만 고르세요 (가장 비중 높은 주제).
전체 시장 흐름/거시경제 위주면 "일반시황"을 고르세요."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # 코드블록으로 감싸서 올 경우 대비
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"summary": text[:500], "category": "기타", "tags": [], "priority": "중"}

    # category가 옵션 밖 값으로 오면 "기타"로 안전하게 강등
    if result.get("category") not in CATEGORY_OPTIONS:
        result["category"] = "기타"
    return result


# ---------- 5. Notion에 저장 (URL 중복 체크 포함) ----------

def url_already_saved(url: str) -> bool:
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
        headers=notion_headers(),
        json={"filter": {"property": "URL", "url": {"equals": url}}},
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json().get("results", [])) > 0


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def save_to_notion(meta: dict, analysis: dict, transcript: str):
    today = datetime.now(timezone.utc).astimezone().date().isoformat()

    properties = {
        "Title": {"title": [{"text": {"content": meta["title"][:200]}}]},
        "URL": {"url": meta["url"]},
        "채널명": {"rich_text": [{"text": {"content": meta.get("channel", "")}}]},
        "수집일": {"date": {"start": today}},
        "요약": {"rich_text": [{"text": {"content": analysis["summary"][:2000]}}]},
        "카테고리": {"select": {"name": analysis.get("category", "기타")}},
        "태그": {"multi_select": [{"name": t} for t in analysis.get("tags", [])]},
        "우선순위": {"select": {"name": analysis.get("priority", "중")}},
    }

    # 자막 원문은 페이지 본문(children)에 2000자 단위 블록으로 분할해서 저장
    children = []
    for i in range(0, min(len(transcript), 20000), 1900):
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": transcript[i:i + 1900]}}]},
        })

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(),
        json={
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": properties,
            "children": children,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------- 메인 파이프라인 ----------

def process_url(url: str):
    m = YOUTUBE_URL_RE.search(url)
    if not m:
        print(f"[스킵] 유튜브 URL 아님: {url}")
        return
    video_id = m.group(1)
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    if url_already_saved(canonical_url):
        print(f"[스킵] 이미 저장됨: {canonical_url}")
        return

    meta = get_video_metadata(video_id)
    transcript = get_transcript(video_id)

    if not transcript:
        # 자막이 없는 라이브/영상 — 정책 필요: 여기서는 요약 없이 메타데이터만 저장
        analysis = {
            "summary": "(자막 없음 — 수동 확인 필요)",
            "category": "기타",
            "tags": [],
            "priority": "중",
        }
        transcript = ""
    else:
        analysis = summarize_and_tag(meta["title"], meta["channel"], transcript)

    save_to_notion(meta, analysis, transcript)
    print(f"[저장 완료] {meta['title']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", metavar="URL", help="텔레그램 없이 URL 하나만 즉시 처리 (테스트용)")
    args = parser.parse_args()

    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        print(f"환경변수 누락: {missing}")
        sys.exit(1)

    if args.once:
        process_url(args.once)
        return

    if not TELEGRAM_BOT_TOKEN:
        print("YOUTUBE_TELEGRAM_BOT_TOKEN 누락 (폴링 모드에는 필요)")
        sys.exit(1)

    urls = get_telegram_updates()
    if not urls:
        print("새 유튜브 링크 없음")
        return

    for url in urls:
        try:
            process_url(url)
        except Exception as e:
            print(f"[에러] {url}: {e}")


if __name__ == "__main__":
    main()
