# Codex Auth Provider

이 저장소는 두 가지 목적을 위해 만든 예제 리포지토리입니다.

1. 다른 앱에서 사용할 수 있는 **Codex custom OAuth provider 구현 방식**을 정리합니다.
2. 그 구현 방식을 재사용할 수 있도록 **Codex skill**로 배포합니다.

## 포함 내용

- [`codex_custom_provider_smoke.py`](./codex_custom_provider_smoke.py)
  - ChatGPT/Codex OAuth를 직접 처리하는 최소 실행 예제
  - `browser` PKCE 로그인과 `device` 로그인을 모두 지원합니다
  - `auth.json` 저장/refresh
  - `chatgpt.com/backend-api/codex/responses` 직접 호출
  - SSE 응답을 파싱합니다

- [`.codex/skills/codex-oauth-provider`](./.codex/skills/codex-oauth-provider)
  - 다른 앱이나 런타임에서 같은 패턴으로 Codex OAuth provider를 구현하실 때 사용하는 로컬 skill입니다
  - 구현 가이드, 포팅 메모, Python 템플릿을 포함합니다

- [`Legacy/`](./Legacy)
  - 이전 실험 버전을 보관합니다

## 이 리포지토리의 핵심 아이디어

Codex를 custom provider로 연결하실 때 `codex login`에 의존하지 않고도 다음 흐름을 직접 구현하실 수 있습니다.

- Browser OAuth PKCE
- Device-code auth
- `auth.json` 기반 토큰 캐시
- refresh token 갱신
- `ChatGPT-Account-Id` 헤더 포함 요청
- `store: false`, `stream: true` 기반 Codex backend 호출

현재 예제는 **브라우저를 자동 실행하지 않고 링크만 출력하는 방식**을 기본으로 합니다.

## 지원 범위에 대한 안내

이 저장소는 Anthropic 또는 Google 제공자 예제와 같은 범용 멀티프로바이더 토큰 중계가 아니라, **OpenAI Codex의 사용자 로그인 기반 인증 흐름**을 직접 구현하는 예제를 다룹니다.

OpenAI 공식 Codex 문서에는 `codex login`이 ChatGPT 계정 기반 브라우저 OAuth와 device auth를 지원한다고 안내되어 있습니다. 따라서 이 저장소는 개인 또는 사용자별 로그인 흐름을 다른 앱에 임베드하는 방법을 설명합니다.

다만 OpenAI 공식 문서 기준으로 `auth.json` 기반 Codex 계정 인증은 **신뢰할 수 있는 private 인프라와 사용자 본인 계정 범위**를 전제로 다뤄야 합니다. 자동화 기본값은 여전히 API key이며, `auth.json`은 비밀번호처럼 취급해야 하고, public 저장소나 공개 환경에 두면 안 됩니다.

즉, 이 예제는 **개인용 도구, 로컬 앱, 사내 trusted 환경의 사용자별 연동**에는 맞지만, 사용자 토큰을 공용 백엔드에서 대신 보관하거나, 재판매형 SaaS, 다중 사용자 서비스, 공유 세션 중계 계층처럼 운영하는 용도는 OpenAI 공식 지원 범위로 보기 어렵습니다.

## 빠른 사용

브라우저 또는 디바이스 인증으로 바로 테스트하실 수 있습니다.

```bash
python3 codex_custom_provider_smoke.py "Say hello in one sentence"
```

인증 모드를 명시하고 싶으시면:

```bash
python3 codex_custom_provider_smoke.py --auth-mode browser "Say hello"
python3 codex_custom_provider_smoke.py --auth-mode device "Say hello"
```

이미 만든 `auth.json`을 무시하고 다시 로그인하시려면:

```bash
python3 codex_custom_provider_smoke.py --force-login --auth-mode browser
```

## Skill 사용

이 프로젝트 안의 local skill은 다음 경로에 있습니다.

- [`.codex/skills/codex-oauth-provider/SKILL.md`](./.codex/skills/codex-oauth-provider/SKILL.md)

이 skill은 다음과 같은 요청에 맞춰 설계했습니다.

- 다른 CLI에 Codex OAuth provider 붙이기
- GUI 앱에서 browser/device auth 흐름 만들기
- `auth.json` 저장 형식과 refresh 로직 재사용하기
- Python 예제를 다른 런타임으로 포팅하기

## 참고

- 구현 템플릿: [`.codex/skills/codex-oauth-provider/assets/python/codex_oauth_provider_template.py`](./.codex/skills/codex-oauth-provider/assets/python/codex_oauth_provider_template.py)
- 구현 가이드: [`.codex/skills/codex-oauth-provider/references/implementation-guide.md`](./.codex/skills/codex-oauth-provider/references/implementation-guide.md)
- 포팅 노트: [`.codex/skills/codex-oauth-provider/references/porting-notes.md`](./.codex/skills/codex-oauth-provider/references/porting-notes.md)
- OpenAI Codex CLI login 문서: <https://developers.openai.com/codex/cli/reference/#codex-login>
- OpenAI Codex 인증 및 CI/CD 주의사항: <https://developers.openai.com/codex/auth/ci-cd-auth/#when-to-use-this>
