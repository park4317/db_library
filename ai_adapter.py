"""Provider-neutral DB_library enrichment interface."""

import json
import os
import re

import requests

CATEGORIES = {"일반시황", "반도체", "제약·바이오", "이차전지", "방산", "AI인프라", "가상자산", "전략·백테스트", "기타"}
PRIORITIES = {"상", "중", "하"}


class EnrichmentUnavailable(Exception):
    pass


def _prompt(text, title, channel):
    return (
        "다음 자료를 투자정보 아카이브용으로 한국어로 정리하세요. 사실과 해석을 구분하고 원문에 없는 수치나 주장을 추가하지 마세요. "
        "JSON 객체만 반환하세요. 필드: summary(3~5문장), category(일반시황/반도체/제약·바이오/이차전지/방산/AI인프라/가상자산/전략·백테스트/기타 중 하나), "
        "tags(0~3개 문자열), priority(상/중/하).\n"
        f"제목: {title[:200]}\n채널: {channel[:200]}\n원문: {text[:12000]}"
    )


def _gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise EnrichmentUnavailable("GEMINI_KEY_MISSING")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}},
        timeout=45,
    )
    response.raise_for_status()
    parts = response.json()["candidates"][0]["content"]["parts"]
    return "".join(part.get("text", "") for part in parts)


def _claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnrichmentUnavailable("CLAUDE_KEY_MISSING")
    from anthropic import Anthropic
    client = Anthropic(api_key=key, max_retries=0, timeout=45)
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
    response = client.messages.create(
        model=model, max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")


PROVIDERS = {"gemini": _gemini, "claude": _claude}


def _validate(raw):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    value = json.loads(cleaned)
    summary = value.get("summary")
    category = value.get("category")
    tags = value.get("tags")
    priority = value.get("priority")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise ValueError("INVALID_SUMMARY")
    if category not in CATEGORIES or priority not in PRIORITIES:
        raise ValueError("INVALID_CLASSIFICATION")
    if not isinstance(tags, list) or len(tags) > 3 or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("INVALID_TAGS")
    return {"summary": summary.strip(), "category": category,
            "tags": [tag.strip()[:100] for tag in tags], "priority": priority}


def _safe_error_category(exc):
    """Return only a bounded classification, never provider response text."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return "AUTH", status
        if status == 429:
            return "RATE_LIMIT", status
        if 400 <= status < 500:
            return "HTTP_4XX", status
        if 500 <= status < 600:
            return "HTTP_5XX", status
    name = type(exc).__name__
    if "Timeout" in name:
        return "TIMEOUT", None
    if name in ("ConnectionError", "APIConnectionError"):
        return "CONNECTION", None
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)):
        return "RESPONSE_SCHEMA", None
    if isinstance(exc, EnrichmentUnavailable):
        return "CONFIG", None
    return "UNKNOWN", None


def enrich(text, title, channel):
    """Return validated fields plus `_provider`; never include key or raw response in errors."""
    primary = os.environ.get("AI_PROVIDER", "gemini").lower()
    fallback = os.environ.get("AI_FALLBACK_PROVIDER", "claude").lower()
    if primary not in PROVIDERS or fallback not in PROVIDERS:
        raise EnrichmentUnavailable("INVALID_PROVIDER_CONFIG")
    for provider in dict.fromkeys((primary, fallback)):
        try:
            result = _validate(PROVIDERS[provider](_prompt(text, title, channel)))
            result["_provider"] = provider
            return result
        except Exception as exc:
            category, status = _safe_error_category(exc)
            status_label = str(status) if status is not None else "NA"
            print(f"AI_PROVIDER_FAIL provider={provider} category={category} status={status_label}", flush=True)
            continue
    raise EnrichmentUnavailable("AI_ENRICHMENT_FAILED")

