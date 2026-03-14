---
name: codex-oauth-provider
description: Implement or adapt a custom provider that authenticates to Codex using ChatGPT OAuth browser PKCE or device-code flows, stores and refreshes auth.json credentials, and sends direct requests to the chatgpt.com Codex backend with SSE parsing. Use when building Codex support into another CLI, desktop app, local tool, or agent runtime and you want the same pattern as codex_custom_provider_smoke.py without depending on codex login.
---

# Codex OAuth Provider

Provide a reusable implementation pattern for embedding Codex OAuth into another app.

## Workflow

1. Read `references/implementation-guide.md` before designing the provider.
2. Start from `assets/python/codex_oauth_provider_template.py` when the user wants a runnable baseline.
3. Read `references/porting-notes.md` when embedding the flow into a non-Python runtime, GUI app, daemon, or headless environment.
4. Preserve the login invariants:
   - Browser mode prints a login URL and does not auto-open the browser.
   - Device mode prints the device URL and user code, then polls for authorization.
   - Tokens are persisted in `auth.json` with `access_token`, `refresh_token`, `id_token`, `account_id`, and `expires_at`.
   - Codex backend requests use `store: false`, `stream: true`, SSE parsing, and `ChatGPT-Account-Id` when available.
5. Keep refresh logic and one-shot 401 retry behavior unless the host app already owns that policy.

## Reference Map

- Read `references/implementation-guide.md` for constants, request/response flow, auth storage shape, and backend call details.
- Read `references/porting-notes.md` for host-app integration advice, common failure modes, and portability constraints.
- Use `assets/examples/auth-json.example.json` as a storage example, not as a live credential file.

## Delivery

- Expose a host-appropriate way to select `browser` or `device` auth mode.
- Keep browser login link-driven; do not add automatic browser launching unless the user explicitly asks for it.
- Document which file or secret store owns the `auth.json` equivalent.
- Validate the final provider by completing a login flow and a real Codex round-trip.
