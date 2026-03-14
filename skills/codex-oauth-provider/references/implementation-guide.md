# Codex OAuth Provider Implementation Guide

Use this guide when implementing the actual provider logic.

## Core constants

- `CLIENT_ID`: `app_EMoamEEZ73f0CkXaXp7hrann`
- `ISSUER`: `https://auth.openai.com`
- Browser authorize URL: `https://auth.openai.com/oauth/authorize`
- Token URL: `https://auth.openai.com/oauth/token`
- Device user-code URL: `https://auth.openai.com/api/accounts/deviceauth/usercode`
- Device polling URL: `https://auth.openai.com/api/accounts/deviceauth/token`
- Device browser URL: `https://auth.openai.com/codex/device`
- Device redirect URI: `https://auth.openai.com/deviceauth/callback`
- Codex backend endpoint: `https://chatgpt.com/backend-api/codex/responses`

## Browser mode flow

1. Generate PKCE verifier and S256 challenge.
2. Generate a random `state`.
3. Build the authorize URL with:
   - `response_type=code`
   - `client_id`
   - `redirect_uri`
   - `scope=openid profile email offline_access`
   - `code_challenge`
   - `code_challenge_method=S256`
   - `id_token_add_organizations=true`
   - `codex_cli_simplified_flow=true`
   - `state`
   - `originator`
4. Print the URL for the user.
5. Either:
   - wait on a localhost callback server, or
   - ask the user to paste the final redirect URL
6. Exchange the authorization code at `/oauth/token`.

## Device mode flow

1. POST `{"client_id": CLIENT_ID}` to the device user-code endpoint.
2. Print the returned browser URL and `user_code`.
3. Poll the device token endpoint with:
   - `device_auth_id`
   - `user_code`
4. On success, read:
   - `authorization_code`
   - `code_verifier`
5. Exchange that code through the same `/oauth/token` authorization-code flow.

## Token exchange and refresh

Authorization code exchange form body:

- `grant_type=authorization_code`
- `code`
- `redirect_uri`
- `client_id`
- `code_verifier`

Refresh exchange form body:

- `grant_type=refresh_token`
- `refresh_token`
- `client_id`

Persist `expires_at` as Unix seconds when `expires_in` is present.

## auth.json shape

Store a single-provider payload shaped like:

```json
{
  "provider": "openai-codex",
  "auth_mode": "oauth_custom",
  "last_updated": 1773490000,
  "tokens": {
    "access_token": "...",
    "refresh_token": "...",
    "id_token": "...",
    "account_id": "...",
    "expires_at": 1773493600
  }
}
```

Treat this as a local cache. The host app may wrap it differently, but keep the token fields intact.

## Account ID extraction

Derive `account_id` from JWT claims in `id_token` first, then `access_token`.

Preferred claim path:

- `https://api.openai.com/auth.chatgpt_account_id`
  - represented in parsed JSON as nested key `["https://api.openai.com/auth"]["chatgpt_account_id"]`

Fallbacks may include:

- top-level `chatgpt_account_id`
- first organization id in `organizations[0].id`

## Codex backend request shape

Send JSON with:

- `model`
- `instructions`
- `input`
- `store: false`
- `stream: true`

`input` should be a Responses-style item list. A minimal request is a single user message with one `input_text` block.

Headers:

- `Authorization: Bearer <access_token>`
- `User-Agent`
- `Content-Type: application/json`
- `Accept: text/event-stream`
- `ChatGPT-Account-Id: <account_id>` when available

## Streaming response handling

Parse SSE events.

Important events:

- `response.output_text.delta`
- `response.completed`
- `response.failed`
- `response.error`
- `response.incomplete`

Accumulate `delta` text. If no text deltas arrive, fall back to the completed payload and extract `output_text`, `text`, or `output[].content[].text`.

## Retry policy

- Refresh expired tokens before sending the request when possible.
- If the backend returns 401 and a refresh token exists, refresh once and retry once.
- Do not loop indefinitely on auth failures.
