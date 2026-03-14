# Porting Notes

Use this file when adapting the provider pattern to another host app.

## Host integration decisions

- Decide where `auth.json` lives.
  - CLI tool: current working directory or app state dir
  - Desktop app: app data directory
  - Multi-project tool: per-project file or central credential store plus project mapping
- Decide how the user chooses `browser` vs `device`.
  - CLI: flag plus interactive prompt fallback
  - GUI: radio/select control
  - Service or CI: explicit config only

## Browser UX

Default to link-based UX.

- Print or display the authorize URL.
- Do not auto-open the browser unless the product explicitly wants that behavior.
- Keep localhost callback support when the host can bind a port.
- Keep redirect-URL paste fallback for headless, remote, SSH, container, and port-restricted setups.

## Device UX

Display both:

- device URL
- user code

The app should keep polling in the background and show progress or waiting status.

## Non-interactive environments

If login is needed and the host cannot prompt:

- require explicit auth mode and callback strategy
- fail with a clear error instead of hanging

Good error examples:

- `Non-interactive run requires --auth-mode when login is needed`
- `Could not bind local callback server; retry with manual callback`

## Common failure modes

- Browser provider error page before login
  - verify `redirect_uri`, `originator`, PKCE, and authorize URL params
- Callback timeout
  - keep manual redirect paste fallback
- Missing `account_id`
  - decode JWT claims from `id_token` or `access_token`
- 401 from backend
  - refresh once, retry once
- Device polling loops forever
  - enforce timeout and handle only expected retry statuses

## Portability checklist

- Preserve PKCE and state validation exactly.
- Preserve token field names in storage.
- Preserve `store: false` and `stream: true`.
- Preserve SSE parsing and completed-payload fallback.
- Preserve one-shot refresh-and-retry behavior.
- Preserve browser-link-only behavior unless the host product explicitly wants auto-open.
