# 유튜브 → Notion 라이브러리 자동화 (초안)

출근 후 Claude Code에서 바로 테스트/디버깅할 수 있도록 미리 준비한 초안입니다.
여기(Claude 채팅 sandbox)는 youtube.com 도메인이 네트워크 화이트리스트에 막혀 있어
실제 실행 테스트는 못했습니다 — Claude Code 로컬 환경이나 GitHub Actions에서 실행하세요.

## 아키텍처

```
텔레그램 (전용 봇/전용 채널 — 기존 뉴스봇과 완전히 분리)
   → GitHub Actions (10분 간격 폴링, 기존 뉴스봇과 동일 패턴이지만 별도 워크플로우/시크릿)
      → youtube-transcript-api : 자막 추출 (공식 자막 API, 크롤링 아님)
      → oEmbed                 : 제목·채널명 메타데이터
      → Claude API             : 요약 + 카테고리 분류 + 태깅
      → Notion API             : DB 자동 저장 (URL 중복 체크)
```

매매봇은 장개시 전 / 장종료 전 하루 2번만 참조하면 되므로(실시간 아님) **Notion DB 하나로 충분**합니다.
사람이 보는 큐레이션 라이브러리와 봇이 참조하는 저장소를 굳이 분리할 필요 없음 — 별도 벡터DB는 불필요.
봇은 그 2번의 타이밍에 Notion API로 직접 쿼리(카테고리/날짜 필터)해서 최근 인사이트를 가져오면 됩니다.

## "폴더별 정리" 기능

일반시황 / 반도체 / 제약 / 이차전지 같은 폴더 분류, 됩니다. Claude가 자막을 읽고 영상마다
`카테고리`를 하나씩 자동으로 판단해서 저장합니다 (Notion에는 진짜 폴더가 없어서, DB의
**Select 필드 + Board 뷰**로 폴더처럼 구현했어요 — 자세한 건 `notion_schema.md` 참고).
Notion에서 DB 열고 뷰 하나를 Board 타입으로 추가 → Group by `카테고리` 설정하면
칼럼별로 영상이 자동 정렬되는 걸 바로 보실 수 있습니다.

## 준비물 (출근 후 10분이면 세팅 가능)

1. **새 텔레그램 봇 생성** — 텔레그램에서 `@BotFather` → `/newbot` → 새 봇 이름/username 지정 → 토큰 발급
   (기존 뉴스봇과 다른 봇이어야 함)
2. **전용 채널/그룹 생성** — 예: "유튜브라이브러리" 채널 하나 새로 만들고, 방금 만든 봇을 관리자로 추가
3. **chat_id 확인** — 채널에 아무 메시지나 하나 보낸 뒤
   `https://api.telegram.org/bot<토큰>/getUpdates` 호출해서 `chat.id` 확인 (채널은 보통 음수 값, `-100...`)
4. **Notion Integration 토큰** — notion.so/my-integrations 에서 발급, 대상 DB에 연결(Share) 필요
5. **Notion Database ID** — 아래 스키마로 DB 새로 생성 후 URL에서 추출
6. **Anthropic API Key** — 요약/태깅용
7. 아래 6개를 GitHub repo Settings → Secrets and variables → Actions 에 등록
   - `YOUTUBE_TELEGRAM_BOT_TOKEN`
   - `YOUTUBE_TELEGRAM_CHAT_ID` (전용 채널 chat_id — 있으면 다른 채팅 무시)
   - `ANTHROPIC_API_KEY`
   - `NOTION_API_KEY`
   - `NOTION_DATABASE_ID`

## 파일 구성

- `notion_schema.md` — Notion DB 필드 설계
- `youtube_to_notion.py` — 메인 파이프라인 스크립트
- `requirements.txt` — 의존성
- `.github/workflows/youtube-library.yml` — GitHub Actions 워크플로우 (10분 간격 폴링)
- `state/last_update_id.json` — 텔레그램 폴링 offset 저장용 (워크플로우가 자동으로 커밋)

## 출근 후 할 일 (Claude Code에서)

1. 새 리포로 push (기존 뉴스봇과 봇/채널/시크릿을 분리했으므로 같은 리포에 넣어도 워크플로우 파일만 나뉘면 됨 — 굳이 병합할 필요 없음)
2. `pip install -r requirements.txt` 후 로컬에서 `TG_TEST_URL="https://youtube.com/..." python youtube_to_notion.py --once` 같은 단발 테스트 (스크립트에 `--once` 옵션 추가해뒀습니다)
3. 실제 유튜브 자막이 잘 뽑히는지, 라이브 방송(자막 없는 경우) 처리 로직 확인 — 자막 없으면 어떻게 할지 정책 필요 (오디오 다운로드+Whisper 전사 vs 스킵)
4. Notion DB 실제 생성 후 스키마 필드명 맞추기
5. GitHub Secrets 등록 후 워크플로우 활성화
