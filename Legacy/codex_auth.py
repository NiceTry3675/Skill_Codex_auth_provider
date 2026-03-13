from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUTH_JSON_PATH = Path(__file__).resolve().parent / "auth.json"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "openid profile email offline_access"
DEFAULT_ORIGINATOR = os.environ.get("CODEX_OAUTH_ORIGINATOR", "python_smoke")
TOKEN_EXPIRY_SKEW_SECONDS = 60
CALLBACK_TIMEOUT_SECONDS = 5 * 60
SUCCESS_HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Codex Login Complete</title>
  </head>
  <body>
    <h1>Authorization successful</h1>
    <p>You can close this window and return to the terminal.</p>
  </body>
</html>
"""


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    account_id: str | None = None
    expires_at: int | None = None


@dataclass
class ParsedAuthorizationInput:
    code: str | None = None
    state: str | None = None


def _now_ts() -> int:
    return int(time.time())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _generate_pkce_verifier(length: int = 64) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_pkce_challenge(verifier: str) -> str:
    return _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())


def _generate_state() -> str:
    return _b64url_encode(secrets.token_bytes(32))


def _parse_jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None


def _extract_account_id_from_claims(claims: dict[str, Any]) -> str | None:
    auth_ns = claims.get("https://api.openai.com/auth")
    if isinstance(auth_ns, dict):
        nested = auth_ns.get("chatgpt_account_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    top = claims.get("chatgpt_account_id")
    if isinstance(top, str) and top.strip():
        return top.strip()

    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id.strip():
                return org_id.strip()

    return None


def _extract_account_id(tokens: dict[str, Any]) -> str:
    for key in ("id_token", "access_token"):
        raw = tokens.get(key)
        if isinstance(raw, str) and raw:
            claims = _parse_jwt_claims(raw)
            if claims:
                account_id = _extract_account_id_from_claims(claims)
                if account_id:
                    return account_id
    raise RuntimeError("Could not extract account_id from token response")


def _token_bundle_from_response(
    data: dict[str, Any],
    previous: TokenBundle | None = None,
) -> TokenBundle:
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(f"Token response missing access_token: {data}")

    refresh_token = data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = previous.refresh_token if previous else None

    id_token = data.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        id_token = previous.id_token if previous else None

    expires_in = data.get("expires_in")
    expires_at: int | None = previous.expires_at if previous else None
    if isinstance(expires_in, (int, float)):
        expires_at = _now_ts() + int(expires_in)

    account_id = _extract_account_id(data)
    if not account_id and previous:
        account_id = previous.account_id

    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        expires_at=expires_at,
    )


def _write_auth_file(bundle: TokenBundle) -> None:
    AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": "openai-codex",
        "auth_mode": "oauth_custom",
        "last_updated": _now_ts(),
        "last_refresh": _utc_now_iso(),
        "tokens": {
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "id_token": bundle.id_token,
            "account_id": bundle.account_id,
            "expires_at": bundle.expires_at,
        },
    }
    AUTH_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(AUTH_JSON_PATH, 0o600)
    except OSError:
        pass
    print(f"Saved auth cache to: {AUTH_JSON_PATH}")


def _load_auth_file() -> TokenBundle | None:
    if not AUTH_JSON_PATH.exists():
        return None

    try:
        payload = json.loads(AUTH_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid auth file {AUTH_JSON_PATH}: {exc}") from exc

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None

    refresh_token = tokens.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise RuntimeError(f"Invalid tokens.refresh_token in {AUTH_JSON_PATH}")

    id_token = tokens.get("id_token")
    if id_token is not None and not isinstance(id_token, str):
        raise RuntimeError(f"Invalid tokens.id_token in {AUTH_JSON_PATH}")

    account_id = tokens.get("account_id")
    if account_id is not None and not isinstance(account_id, str):
        raise RuntimeError(f"Invalid tokens.account_id in {AUTH_JSON_PATH}")

    expires_at = tokens.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, int):
        if isinstance(expires_at, float):
            expires_at = int(expires_at)
        else:
            raise RuntimeError(f"Invalid tokens.expires_at in {AUTH_JSON_PATH}")

    if not account_id:
        account_id = _extract_account_id(tokens)

    return TokenBundle(
        access_token=access_token.strip(),
        refresh_token=refresh_token.strip() if isinstance(refresh_token, str) and refresh_token.strip() else None,
        id_token=id_token.strip() if isinstance(id_token, str) and id_token.strip() else None,
        account_id=account_id,
        expires_at=expires_at,
    )


def _token_is_stale(bundle: TokenBundle) -> bool:
    if bundle.expires_at is not None:
        return bundle.expires_at <= _now_ts() + TOKEN_EXPIRY_SKEW_SECONDS

    claims = _parse_jwt_claims(bundle.access_token)
    if not claims:
        return True
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return float(exp) <= _now_ts() + TOKEN_EXPIRY_SKEW_SECONDS


def _http_form_post(url: str, form: dict[str, str], timeout: int = 30) -> tuple[int, dict[str, Any], str]:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "codex-custom-provider-smoke/0.3",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload, raw


def _build_authorize_url(verifier: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": _generate_pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": DEFAULT_ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class OAuthCallbackServer:
    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.event = threading.Event()
        self.error: str | None = None
        self.code: str | None = None
        self.state: str | None = None
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != REDIRECT_PATH:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not found")
                    return

                qs = urllib.parse.parse_qs(parsed.query)
                error = qs.get("error", [None])[0]
                error_description = qs.get("error_description", [None])[0]
                code = qs.get("code", [None])[0]
                state = qs.get("state", [None])[0]

                if error:
                    parent.error = str(error_description or error)
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    html = f"<html><body><h1>Authorization failed</h1><p>{parent.error}</p></body></html>"
                    self.wfile.write(html.encode("utf-8"))
                    parent.event.set()
                    return

                if not code:
                    parent.error = "Missing authorization code"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Missing authorization code</h1></body></html>")
                    parent.event.set()
                    return

                if state != parent.expected_state:
                    parent.error = "Invalid state - potential CSRF attack"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Invalid state</h1></body></html>")
                    parent.event.set()
                    return

                parent.code = str(code)
                parent.state = str(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(SUCCESS_HTML.encode("utf-8"))
                parent.event.set()

        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        self._server = ReusableTCPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: int) -> tuple[str, str]:
        if not self.event.wait(timeout=timeout):
            raise TimeoutError("OAuth callback timeout - authorization took too long")
        if self.error:
            raise RuntimeError(self.error)
        if not self.code or not self.state:
            raise RuntimeError("OAuth callback completed without code/state")
        return self.code, self.state

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None


def parse_authorization_input(value: str) -> ParsedAuthorizationInput:
    raw = value.strip()
    if not raw:
        return ParsedAuthorizationInput()

    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            qs = urllib.parse.parse_qs(parsed.query)
            return ParsedAuthorizationInput(
                code=qs.get("code", [None])[0],
                state=qs.get("state", [None])[0],
            )
    except ValueError:
        pass

    if raw.startswith("code="):
        qs = urllib.parse.parse_qs(raw)
        return ParsedAuthorizationInput(
            code=qs.get("code", [None])[0],
            state=qs.get("state", [None])[0],
        )

    if "#" in raw:
        code, state = raw.split("#", 1)
        return ParsedAuthorizationInput(code=code or None, state=state or None)

    return ParsedAuthorizationInput(code=raw)


def _prompt_for_redirect_url(expected_state: str) -> str:
    if not os.isatty(0):
        raise RuntimeError(
            "Interactive OAuth is required, but stdin is not a TTY. "
            "Run with a terminal or pre-populate auth.json."
        )

    while True:
        pasted = input("Paste the full redirect URL or authorization code: ").strip()
        if not pasted:
            print("No redirect URL was pasted. Try again.")
            continue

        parsed = parse_authorization_input(pasted)
        if parsed.state and parsed.state != expected_state:
            print("Invalid state - potential CSRF attack. Try again.")
            continue
        if parsed.code:
            return parsed.code
        print("Could not parse an authorization code. Try again.")


def exchange_authorization_code(code: str, verifier: str) -> TokenBundle:
    status, payload, raw = _http_form_post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=120,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Token exchange failed ({status})\n{raw}")
    return _token_bundle_from_response(payload)


def refresh_access_token(bundle: TokenBundle) -> TokenBundle:
    if not bundle.refresh_token:
        raise RuntimeError("No refresh_token available")

    status, payload, raw = _http_form_post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=120,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Token refresh failed ({status})\n{raw}")
    return _token_bundle_from_response(payload, previous=bundle)


def run_browser_login() -> TokenBundle:
    verifier = _generate_pkce_verifier()
    state = _generate_state()
    authorize_url = _build_authorize_url(verifier=verifier, state=state)

    server = OAuthCallbackServer(expected_state=state)
    manual_callback = False
    try:
        try:
            server.start()
        except OSError as exc:
            manual_callback = True
            print(
                f"Could not bind local callback server on {REDIRECT_HOST}:{REDIRECT_PORT}. "
                f"Falling back to manual paste. Original error: {exc}"
            )

        print("Open this URL in your browser to sign in with ChatGPT:")
        print(authorize_url)
        print("")
        if manual_callback:
            print("After the browser redirects, copy the full final URL that starts with:")
            print(REDIRECT_URI)
            print("")
        else:
            print(f"Waiting for local callback on {REDIRECT_HOST}:{REDIRECT_PORT} ...\n")

        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

        if manual_callback:
            code = _prompt_for_redirect_url(state)
        else:
            try:
                code, returned_state = server.wait(timeout=CALLBACK_TIMEOUT_SECONDS)
                if returned_state != state:
                    raise RuntimeError("Invalid state - potential CSRF attack")
            except TimeoutError:
                print("Timed out waiting for the local callback.")
                print("Paste the final redirect URL instead.\n")
                code = _prompt_for_redirect_url(state)

        return exchange_authorization_code(code, verifier)
    finally:
        server.stop()


def ensure_auth(interactive: bool = True) -> tuple[str, str | None]:
    bundle = _load_auth_file()
    if bundle and not _token_is_stale(bundle):
        return bundle.access_token, bundle.account_id

    if bundle and bundle.refresh_token:
        print("Refreshing Codex access token...")
        bundle = refresh_access_token(bundle)
        _write_auth_file(bundle)
        return bundle.access_token, bundle.account_id

    if not interactive:
        raise RuntimeError(
            "No usable auth.json found. Run with --login in an interactive terminal first."
        )

    print("Starting browser login for ChatGPT/Codex...")
    print(f"Auth cache path: {AUTH_JSON_PATH}")
    bundle = run_browser_login()
    _write_auth_file(bundle)
    return bundle.access_token, bundle.account_id


def login_only() -> tuple[str, str | None]:
    return ensure_auth(interactive=True)
