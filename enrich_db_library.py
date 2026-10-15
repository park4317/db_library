"""Enrich already archived Notion DB_library posts; never alter raw pages on failure."""

import argparse
import os
import sys

import requests

from ai_adapter import EnrichmentUnavailable, PROVIDERS, enrich

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

NOTION_KEY = os.environ.get("NOTION_API_KEY")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")


def headers():
    return {"Authorization": f"Bearer {NOTION_KEY}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


def notion(method, url, **kwargs):
    response = requests.request(method, url, headers=headers(), timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def plain(property_value, kind):
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in property_value.get(kind, []))


def pending_pages(limit):
    cursor = None
    collected = []
    while len(collected) < limit:
        payload = {"page_size": min(100, limit - len(collected)),
                   "filter": {"property": "요약", "rich_text": {"equals": "SUMMARY_PENDING"}},
                   "sorts": [{"property": "게시일", "direction": "descending"}]}
        if cursor:
            payload["start_cursor"] = cursor
        result = notion("POST", f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", json=payload)
        collected.extend(result.get("results", []))
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return collected[:limit]


def explicit_pages(message_ids):
    """One-time legacy backfill by exact Telegram message IDs, never by fuzzy text."""
    pages = []
    channel_id = os.environ.get("YOUTUBE_TELEGRAM_CHAT_ID")
    if not channel_id:
        raise ValueError("CHANNEL_ID_REQUIRED_FOR_BACKFILL")
    for message_id in message_ids:
        key = f"{channel_id}:{message_id}"
        result = notion("POST", f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
                        json={"filter": {"property": "Telegram Message ID",
                                         "rich_text": {"equals": key}}})
        matches = result.get("results", [])
        if len(matches) != 1:
            raise ValueError("BACKFILL_ID_NOT_UNIQUE_OR_MISSING")
        page = matches[0]
        # A range may include rows already enriched; never overwrite them during recovery.
        if plain(page.get("properties", {}).get("요약", {}), "rich_text") == "SUMMARY_PENDING":
            pages.append(page)
    return pages


def page_body(page_id):
    chunks = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        result = notion("GET", f"https://api.notion.com/v1/blocks/{page_id}/children", params=params)
        for block in result.get("results", []):
            kind = block.get("type")
            if kind in ("paragraph", "heading_1", "heading_2", "heading_3"):
                chunks.append(plain(block.get(kind, {}), "rich_text"))
        cursor = result.get("next_cursor")
        if not cursor:
            return "\n".join(chunks)


def update_page(page_id, result):
    properties = {
        "요약": {"rich_text": [{"text": {"content": result["summary"]}}]},
        "카테고리": {"select": {"name": result["category"]}},
        "태그": {"multi_select": [{"name": tag} for tag in result["tags"]]},
        "우선순위": {"select": {"name": result["priority"]}},
    }
    notion("PATCH", f"https://api.notion.com/v1/pages/{page_id}", json={"properties": properties})
    page = notion("GET", f"https://api.notion.com/v1/pages/{page_id}")
    if plain(page.get("properties", {}).get("요약", {}), "rich_text") != result["summary"]:
        raise RuntimeError("NOTION_ENRICHMENT_READBACK_FAILED")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--message-range", help="Explicit legacy Telegram message ID range, e.g. 166:171")
    args = parser.parse_args()
    if not NOTION_KEY or not DATABASE_ID:
        print("ENRICHMENT_FAIL category=ENV_MISSING")
        return 1
    if not 1 <= args.limit <= 100:
        print("ENRICHMENT_FAIL category=INVALID_LIMIT")
        return 1
    primary = os.environ.get("AI_PROVIDER", "gemini").lower()
    fallback = os.environ.get("AI_FALLBACK_PROVIDER", "claude").lower()
    if primary not in PROVIDERS or fallback not in PROVIDERS:
        print("ENRICHMENT_FAIL category=AI_PROVIDER_CONFIG")
        return 1
    try:
        if args.message_range:
            start, end = (int(value) for value in args.message_range.split(":"))
            if start < 1 or end < start or end - start >= 100:
                raise ValueError("INVALID_MESSAGE_RANGE")
            pages = explicit_pages(range(start, end + 1))
        else:
            pages = pending_pages(args.limit)
    except Exception as exc:
        print(f"ENRICHMENT_FAIL stage=NOTION_QUERY category={type(exc).__name__}")
        return 1
    counts = {"gemini": 0, "claude": 0, "failed": 0}
    fatal_category = None
    for page in pages:
        props = page.get("properties", {})
        title = plain(props.get("Title", {}), "title")
        channel = plain(props.get("채널명", {}), "rich_text")
        try:
            body = page_body(page["id"])
        except Exception as exc:
            fatal_category = type(exc).__name__
            print(f"ENRICHMENT_FAIL stage=NOTION_READ category={fatal_category}")
            break
        if not body.strip():
            fatal_category = "SOURCE_BODY_EMPTY"
            print("ENRICHMENT_FAIL stage=NOTION_READ category=SOURCE_BODY_EMPTY")
            break
        try:
            result = enrich(body, title, channel)
        except EnrichmentUnavailable:
            counts["failed"] += 1
            print("AI_ENRICHMENT_FAILED category=PROVIDER_UNAVAILABLE")
            continue
        try:
            update_page(page["id"], result)
        except Exception as exc:
            fatal_category = type(exc).__name__
            print(f"ENRICHMENT_FAIL stage=NOTION_WRITE category={fatal_category}")
            break
        counts[result["_provider"]] += 1
        print(f"ENRICHED provider={result['_provider']}")
    if fatal_category:
        return 1
    if counts["failed"] == 0:
        status = "ENRICHMENT_PASS"
    elif counts["gemini"] + counts["claude"]:
        status = "ENRICHMENT_PASS_WITH_WARNING"
    else:
        status = "ENRICHMENT_WARNING"
    log_status = "PASS" if status == "ENRICHMENT_PASS" else "PASS_WITH_WARNING" if status == "ENRICHMENT_PASS_WITH_WARNING" else "WARNING"
    print(f"ENRICHMENT_DONE pending_seen={len(pages)} gemini={counts['gemini']} claude={counts['claude']} failed={counts['failed']} status={log_status} outcome={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

