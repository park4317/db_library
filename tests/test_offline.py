"""Offline gate: no network, no credentials, no Notion writes."""

import os
from unittest.mock import patch

import ai_adapter
import youtube_to_notion as collector

SAMPLE = '{"summary":"검증된 사실을 요약한다. 추가 확인이 필요하다. 반대 근거도 남긴다.","category":"일반시황","tags":["시장"],"priority":"중"}'


def _fail(_):
    raise RuntimeError("provider unavailable")


with patch.dict(os.environ, {"AI_PROVIDER": "gemini", "AI_FALLBACK_PROVIDER": "claude"}):
    with patch.dict(ai_adapter.PROVIDERS, {"gemini": lambda _: SAMPLE, "claude": _fail}):
        assert ai_adapter.enrich("본문", "제목", "채널")["_provider"] == "gemini"
    with patch.dict(ai_adapter.PROVIDERS, {"gemini": _fail, "claude": lambda _: SAMPLE}):
        assert ai_adapter.enrich("본문", "제목", "채널")["_provider"] == "claude"
    with patch.dict(ai_adapter.PROVIDERS, {"gemini": _fail, "claude": _fail}):
        try:
            ai_adapter.enrich("본문", "제목", "채널")
        except ai_adapter.EnrichmentUnavailable:
            pass
        else:
            raise AssertionError("both providers failed but enrichment succeeded")

post = {"chat_id": "-100123", "message_id": "166", "text": "원문", "chat_title": "DB_library",
        "posted_at": 0, "attachment_types": []}


class FakeResponse:
    def raise_for_status(self):
        pass


with patch.object(collector, "saved", side_effect=[False, True]), \
     patch.object(collector.requests, "post", return_value=FakeResponse()):
    assert collector.archive(post) == "created"

with patch.object(collector, "saved", return_value=True):
    assert collector.archive(post) == "duplicate"


class FakeBadRequest:
    status_code = 400

    def __init__(self, detail):
        self.body = {"error": {"message": detail}}


assert ai_adapter._safe_error_category(FakeBadRequest("credit balance exhausted")) == ("BILLING_LIMIT", 400)
assert ai_adapter._safe_error_category(FakeBadRequest("model not available")) == ("MODEL_REQUEST", 400)
assert ai_adapter._safe_error_category(FakeBadRequest("unrecognized argument")) == ("INVALID_REQUEST_OTHER", 400)

print("OFFLINE_GATES=PASS gemini_primary=PASS claude_fallback=PASS both_fail_preserves_raw=PASS collector_ai_independent=PASS")

