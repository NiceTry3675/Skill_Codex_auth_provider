#!/usr/bin/env python3
"""
Minimal custom-provider Codex smoke test (no local codex CLI required).

What this does
--------------
1. Reads or creates a project-local auth.json
2. Supports browser OAuth (PKCE) and device-code auth without invoking `codex login`
3. Refreshes tokens when needed
4. Sends a one-line prompt directly to the Codex backend
5. Prints the final text response

Important
---------
This script mirrors the public patterns used by projects like OpenCode
(browser PKCE + device code + direct Codex backend requests), plus the
manual redirect-paste pattern seen in OpenClaw's remote/headless flow.

It is intentionally small and self-contained:
- no Codex CLI
- no Codex app-server
- no tools
- no UI
- no persistence beyond auth.json

Usage
-----
python codex_custom_provider_smoke.py "Say hello in one sentence"
python codex_custom_provider_smoke.py --auth-mode browser "Say hello"
python codex_custom_provider_smoke.py --auth-mode browser --manual-callback "Say hello"
python codex_custom_provider_smoke.py --auth-mode device "Say hello"
python codex_custom_provider_smoke.py --force-login --auth-mode device "Say hello"

By default, auth.json is read from the current working directory.
Use --auth-file to point somewhere else.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# Public values observed in OpenCode's Codex integration.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
DEVICE_WEB_URL = f"{ISSUER}/codex/device"
DEVICE_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"

# Public backend endpoint observed in OpenCode.
CODEX_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

DEFAULT_REDIRECT_HOST = "127.0.0.1"
DEFAULT_REDIRECT_PORT = 1455
DEFAULT_REDIRECT_PATH = "/auth/callback"

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.3-codex")
DEFAULT_INSTRUCTIONS = os.environ.get(
    "CODEX_INSTRUCTIONS",
    "You are a concise assistant.",
)
DEFAULT_ORIGINATOR = os.environ.get("CODEX_OAUTH_ORIGINATOR", "python_smoke")
USER_AGENT = "codex-custom-provider-smoke/0.4"

TOKEN_EXPIRY_SKEW_SECONDS = 60
BROWSER_LOGIN_TIMEOUT_SECONDS = 5 * 60
DEVICE_LOGIN_TIMEOUT_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 120


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    account_id: Optional[str] = None
    expires_at: Optional[int] = None  # unix seconds


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_pkce_verifier(length: int = 64) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return b64url_encode(digest)


def generate_state() -> str:
    return b64url_encode(secrets.token_bytes(32))


def now_ts() -> int:
    return int(time.time())


def parse_jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return json.loads(b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None


def extract_account_id_from_claims(claims: dict[str, Any]) -> Optional[str]:
    auth_ns = claims.get("https://api.openai.com/auth")
    if isinstance(auth_ns, dict):
        nested = auth_ns.get("chatgpt_account_id")
        if isinstance(nested, str) and nested:
            return nested

    top = claims.get("chatgpt_account_id")
    if isinstance(top, str) and top:
        return top

    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id

    return None


def extract_account_id(tokens: dict[str, Any]) -> Optional[str]:
    for key in ("id_token", "access_token"):
        raw = tokens.get(key)
        if isinstance(raw, str) and raw:
            claims = parse_jwt_claims(raw)
            if claims:
                account_id = extract_account_id_from_claims(claims)
                if account_id:
                    return account_id
    return None


def token_bundle_from_response(data: dict[str, Any], previous: TokenBundle | None = None) -> TokenBundle:
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
    expires_at: Optional[int] = None
    if isinstance(expires_in, int):
        expires_at = now_ts() + expires_in
    elif isinstance(expires_in, float):
        expires_at = now_ts() + int(expires_in)
    elif previous:
        expires_at = previous.expires_at

    account_id = extract_account_id(data)
    if not account_id and previous:
        account_id = previous.account_id

    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        expires_at=expires_at,
    )


def load_auth_file(path: Path) -> TokenBundle:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing auth file: {path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise RuntimeError(f"Missing tokens object in {path}")

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError(f"Missing tokens.access_token in {path}")

    refresh_token = tokens.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise RuntimeError(f"Invalid tokens.refresh_token in {path}")

    id_token = tokens.get("id_token")
    if id_token is not None and not isinstance(id_token, str):
        raise RuntimeError(f"Invalid tokens.id_token in {path}")

    expires_at = tokens.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, int):
        if isinstance(expires_at, float):
            expires_at = int(expires_at)
        else:
            raise RuntimeError(f"Invalid tokens.expires_at in {path}")

    account_id = tokens.get("account_id")
    if account_id is not None and not isinstance(account_id, str):
        raise RuntimeError(f"Invalid tokens.account_id in {path}")

    return TokenBundle(
        access_token=access_token.strip(),
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        expires_at=expires_at,
    )


def save_auth_file(path: Path, bundle: TokenBundle) -> None:
    payload = {
        "provider": "openai-codex",
        "auth_mode": "oauth_custom",
        "last_updated": now_ts(),
        "tokens": {
            "access_token": bundle.access_token,
            "refresh_token": bundle.refresh_token,
            "id_token": bundle.id_token,
            "account_id": bundle.account_id,
            "expires_at": bundle.expires_at,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def token_is_stale(bundle: TokenBundle) -> bool:
    if bundle.expires_at is None:
        return False
    return bundle.expires_at <= now_ts() + TOKEN_EXPIRY_SKEW_SECONDS


def http_json_post(url: str, body: dict[str, Any], timeout: int = 30) -> tuple[int, dict[str, Any], str]:
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=req_headers,
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


def http_form_post(url: str, form: dict[str, str], timeout: int = 30) -> tuple[int, dict[str, Any], str]:
    req_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers=req_headers,
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


def build_authorize_url(redirect_uri: str, verifier: str, state: str) -> str:
    challenge = generate_pkce_challenge(verifier)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": DEFAULT_ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class OAuthCallbackServer:
    def __init__(self, host: str, port: int, expected_state: str) -> None:
        self.host = host
        self.port = port
        self.expected_state = expected_state
        self.event = threading.Event()
        self.error: Optional[str] = None
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self._server: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != DEFAULT_REDIRECT_PATH:
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
                    html = f"<html><body><h1>Authorization failed</h1><p>{parent.error}</p></body></html>"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
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
                self.wfile.write(
                    b"<html><body><h1>Authorization successful</h1>"
                    b"<p>You can close this window and return to the terminal.</p></body></html>"
                )
                parent.event.set()

        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        self._server = ReusableTCPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: int) -> tuple[str, str]:
        ok = self.event.wait(timeout=timeout)
        if not ok:
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


def choose_auth_mode(requested_mode: Optional[str]) -> str:
    if requested_mode:
        return requested_mode
    if not sys.stdin.isatty():
        raise RuntimeError("Non-interactive run requires --auth-mode when login is needed")

    while True:
        answer = input("Select auth mode [browser/device]: ").strip().lower()
        if answer in {"browser", "device"}:
            return answer
        print("Enter 'browser' or 'device'.")


def parse_redirect_result(pasted: str, expected_state: str) -> tuple[str, str]:
    if not pasted:
        raise RuntimeError("No redirect URL was pasted")

    parsed = urlparse(pasted)
    qs = parse_qs(parsed.query)
    error = qs.get("error", [None])[0]
    error_description = qs.get("error_description", [None])[0]
    if error:
        raise RuntimeError(str(error_description or error))

    code = qs.get("code", [None])[0]
    returned_state = qs.get("state", [None])[0]
    if not code:
        raise RuntimeError("Redirect URL did not contain code=")
    if returned_state != expected_state:
        raise RuntimeError("Invalid state - potential CSRF attack")
    return str(code), str(returned_state)


def exchange_code_for_tokens(code: str, redirect_uri: str, verifier: str, debug: bool = False) -> TokenBundle:
    status, payload, raw = http_form_post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if debug:
        eprint(f"[debug] token exchange status={status}")
        eprint(f"[debug] token exchange payload={json.dumps(payload, ensure_ascii=False, indent=2)}")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Token exchange failed ({status})\n{raw}")
    return token_bundle_from_response(payload)


def refresh_access_token(bundle: TokenBundle, debug: bool = False) -> TokenBundle:
    if not bundle.refresh_token:
        raise RuntimeError("No refresh_token available")

    status, payload, raw = http_form_post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if debug:
        eprint(f"[debug] refresh status={status}")
        eprint(f"[debug] refresh payload={json.dumps(payload, ensure_ascii=False, indent=2)}")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Token refresh failed ({status})\n{raw}")
    return token_bundle_from_response(payload, previous=bundle)


def login_browser(manual_callback: bool, debug: bool = False) -> TokenBundle:
    host = DEFAULT_REDIRECT_HOST
    port = DEFAULT_REDIRECT_PORT
    redirect_uri = f"http://localhost:{port}{DEFAULT_REDIRECT_PATH}"

    verifier = generate_pkce_verifier()
    state = generate_state()
    auth_url = build_authorize_url(redirect_uri=redirect_uri, verifier=verifier, state=state)

    server: Optional[OAuthCallbackServer] = None
    if not manual_callback:
        try:
            server = OAuthCallbackServer(host=host, port=port, expected_state=state)
            server.start()
        except OSError as exc:
            raise RuntimeError(
                f"Could not bind local callback server on {host}:{port}. "
                f"Retry with --manual-callback. Original error: {exc}"
            ) from exc

    print("Open this URL in your browser to sign in with ChatGPT:")
    print(auth_url)
    print()
    if manual_callback:
        print("After the browser redirects, copy the full final URL that starts with:")
        print(redirect_uri)
        print("and paste it below.\n")
    else:
        print(f"Waiting for local callback on {host}:{port} ...\n")

    try:
        if manual_callback:
            code, returned_state = parse_redirect_result(
                input("Paste the full redirect URL here: ").strip(),
                state,
            )
        else:
            assert server is not None
            try:
                code, returned_state = server.wait(timeout=BROWSER_LOGIN_TIMEOUT_SECONDS)
            except TimeoutError:
                print("Timed out waiting for local callback.")
                print("Paste the full redirect URL instead.\n")
                code, returned_state = parse_redirect_result(
                    input("Paste the full redirect URL here: ").strip(),
                    state,
                )
            if returned_state != state:
                raise RuntimeError("Invalid state - potential CSRF attack")

        bundle = exchange_code_for_tokens(str(code), redirect_uri=redirect_uri, verifier=verifier, debug=debug)
        return bundle
    finally:
        if server is not None:
            server.stop()


def login_device(debug: bool = False) -> TokenBundle:
    status, payload, raw = http_json_post(
        DEVICE_USERCODE_URL,
        {"client_id": CLIENT_ID},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if debug:
        eprint(f"[debug] device usercode status={status}")
        eprint(f"[debug] device usercode payload={json.dumps(payload, ensure_ascii=False, indent=2)}")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Failed to initiate device authorization ({status})\n{raw}")

    device_auth_id = payload.get("device_auth_id")
    user_code = payload.get("user_code")
    interval_raw = payload.get("interval")
    if not isinstance(device_auth_id, str) or not isinstance(user_code, str):
        raise RuntimeError(f"Malformed device auth response: {payload}")

    poll_interval = 5
    try:
        poll_interval = max(int(interval_raw), 1)
    except Exception:
        pass

    print("Open this URL in your browser:")
    print(DEVICE_WEB_URL)
    print()
    print(f"Enter this code: {user_code}\n")

    deadline = time.time() + DEVICE_LOGIN_TIMEOUT_SECONDS
    while time.time() < deadline:
        status, payload, raw = http_json_post(
            DEVICE_TOKEN_URL,
            {
                "device_auth_id": device_auth_id,
                "user_code": user_code,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if debug:
            eprint(f"[debug] device poll status={status}")
            eprint(f"[debug] device poll payload={json.dumps(payload, ensure_ascii=False, indent=2)}")

        if status == 200:
            authorization_code = payload.get("authorization_code")
            code_verifier = payload.get("code_verifier")
            if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
                raise RuntimeError(f"Malformed device token poll response: {payload}")
            return exchange_code_for_tokens(
                code=authorization_code,
                redirect_uri=DEVICE_REDIRECT_URI,
                verifier=code_verifier,
                debug=debug,
            )

        # This mirrors OpenCode's polling logic: keep polling on 403/404.
        if status in (403, 404):
            time.sleep(poll_interval + 3)
            continue

        raise RuntimeError(f"Unexpected device auth polling error ({status})\n{raw}")

    raise TimeoutError("Timed out waiting for device authorization")


def ensure_auth(auth_file: Path, auth_mode: Optional[str], force_login: bool, manual_callback: bool, debug: bool = False) -> TokenBundle:
    bundle: Optional[TokenBundle] = None

    if not force_login and auth_file.exists():
        bundle = load_auth_file(auth_file)
        if token_is_stale(bundle):
            if bundle.refresh_token:
                eprint("auth.json exists but token is stale; refreshing...")
                bundle = refresh_access_token(bundle, debug=debug)
                save_auth_file(auth_file, bundle)
            else:
                eprint("auth.json exists but token is stale and no refresh_token is present; logging in again...")
                bundle = None

    if force_login or bundle is None:
        selected_mode = choose_auth_mode(auth_mode)
        if selected_mode == "browser":
            bundle = login_browser(manual_callback=manual_callback, debug=debug)
        elif selected_mode == "device":
            bundle = login_device(debug=debug)
        else:
            raise RuntimeError(f"Unsupported auth mode: {selected_mode}")

        save_auth_file(auth_file, bundle)

    return bundle


def build_request(prompt: str, model: str, instructions: str) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    }
                ],
            }
        ],
        "store": False,
        "stream": True,
    }


def extract_output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"output_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
        if parts:
            return "\n".join(parts).strip()

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_sse_response(resp, debug: bool = False) -> str:
    event_type: Optional[str] = None
    data_lines: list[str] = []
    text_parts: list[str] = []
    final_payload: Optional[dict[str, Any]] = None

    def flush_event() -> None:
        nonlocal event_type, data_lines, final_payload
        if not data_lines:
            event_type = None
            data_lines = []
            return

        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            event_type = None
            data_lines = []
            return

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            if debug:
                eprint(f"[debug] non-json SSE data for event={event_type!r}: {data!r}")
            event_type = None
            data_lines = []
            return

        if debug:
            eprint(f"[debug] SSE event={event_type!r} payload={json.dumps(payload, ensure_ascii=False)}")

        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
        elif event_type == "response.completed":
            if isinstance(payload, dict):
                final_payload = payload
        elif event_type in {"response.failed", "response.error", "response.incomplete"}:
            raise RuntimeError(
                f"Response stream ended with {event_type}: "
                + json.dumps(payload, ensure_ascii=False, indent=2)
            )

        event_type = None
        data_lines = []

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            flush_event()
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())

    flush_event()

    if text_parts:
        return "".join(text_parts).strip()
    if final_payload is not None:
        return extract_output_text(final_payload)
    return ""


def call_codex_backend(bundle: TokenBundle, prompt: str, model: str, instructions: str, debug: bool = False) -> str:
    body = json.dumps(build_request(prompt=prompt, model=model, instructions=instructions)).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {bundle.access_token}",
        "User-Agent": USER_AGENT,
    }
    if bundle.account_id:
        headers["ChatGPT-Account-Id"] = bundle.account_id

    if debug:
        eprint(f"[debug] request headers={json.dumps(headers, ensure_ascii=False, indent=2)}")
        eprint(f"[debug] request body={body.decode('utf-8')}")

    req = urllib.request.Request(
        CODEX_API_ENDPOINT,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return parse_sse_response(resp, debug=debug)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from Codex backend\n{raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling Codex backend: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal custom-provider Codex smoke test")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello in one sentence.",
        help="One-line prompt to send",
    )
    parser.add_argument(
        "--auth-file",
        default=str(Path.cwd() / "auth.json"),
        help="Path to auth.json (default: ./auth.json)",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["browser", "device"],
        default=None,
        help="Login mode to use if auth.json is missing or --force-login is set",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force a fresh login and overwrite auth.json",
    )
    parser.add_argument(
        "--manual-callback",
        action="store_true",
        help="For browser auth: do not open a local callback server; paste the final redirect URL manually",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model id to request (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="Instructions string to send alongside the prompt",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug logs to stderr",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auth_file = Path(args.auth_file).resolve()

    print(f"Prompt: {args.prompt}\n")
    print(f"Auth file: {auth_file}\n")

    try:
        bundle = ensure_auth(
            auth_file=auth_file,
            auth_mode=args.auth_mode,
            force_login=args.force_login,
            manual_callback=args.manual_callback,
            debug=args.debug,
        )
        try:
            answer = call_codex_backend(
                bundle=bundle,
                prompt=args.prompt,
                model=args.model,
                instructions=args.instructions,
                debug=args.debug,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "HTTP 401" in message and bundle.refresh_token:
                eprint("Got 401 from Codex backend; refreshing token and retrying once...")
                bundle = refresh_access_token(bundle, debug=args.debug)
                save_auth_file(auth_file, bundle)
                answer = call_codex_backend(
                    bundle=bundle,
                    prompt=args.prompt,
                    model=args.model,
                    instructions=args.instructions,
                    debug=args.debug,
                )
            else:
                raise

    except Exception as exc:
        eprint(f"ERROR: {exc}")
        return 1

    print("=== RESPONSE ===")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
