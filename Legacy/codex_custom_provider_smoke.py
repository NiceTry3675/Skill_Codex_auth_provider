#!/usr/bin/env python3
"""
Minimal custom-provider Codex smoke test with built-in browser OAuth.

What this does:
1. Ensures the project auth.json contains a usable ChatGPT/Codex token
2. Sends a one-line prompt directly to the Codex backend
3. Prints the final text response

This is intentionally minimal:
- no codex CLI
- no codex app-server
- no tools
- no UI
- no persistence beyond auth.json

Environment variables:
    CODEX_MODEL          Optional. Default: gpt-5.3-codex
    CODEX_INSTRUCTIONS   Optional. Default: You are a concise assistant.

Run:
    python codex_custom_provider_smoke.py "Say hello in one sentence"
    python codex_custom_provider_smoke.py --login
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from Legacy.codex_auth import AUTH_JSON_PATH, ensure_auth

CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.3-codex")
DEFAULT_INSTRUCTIONS = os.environ.get(
    "CODEX_INSTRUCTIONS",
    "You are a concise assistant.",
)


def extract_output_text(payload: dict[str, Any]) -> str:
    """Best-effort extraction from a Responses-style payload."""
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


def extract_sse_output(raw: str) -> str | None:
    """Best-effort extraction from an SSE response body."""
    text_parts: list[str] = []
    final_payload: dict[str, Any] | None = None

    for block in raw.split("\n\n"):
        if not block.strip():
            continue

        event_type: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())

        if not data_lines:
            continue

        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue

        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
            continue

        if event_type in {"response.completed", "response.failed"} and isinstance(payload, dict):
            final_payload = payload

    if text_parts:
        return "".join(text_parts).strip()
    if isinstance(final_payload, dict):
        return extract_output_text(final_payload)
    return None


def build_request(prompt: str) -> dict[str, Any]:
    return {
        "model": DEFAULT_MODEL,
        "instructions": DEFAULT_INSTRUCTIONS,
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


def call_codex_backend(prompt: str) -> str:
    access_token, account_id = ensure_auth(interactive=True)
    body = json.dumps(build_request(prompt)).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "codex-custom-provider-smoke/0.1",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    req = urllib.request.Request(
        CODEX_ENDPOINT,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} from Codex backend\n"
            f"Response body:\n{raw}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling Codex backend: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sse_output = extract_sse_output(raw)
        if sse_output:
            return sse_output
        return raw.strip()

    return extract_output_text(payload)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Say hello in one sentence.")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Authenticate or refresh auth.json, then exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        if args.login:
            _, account_id = ensure_auth(interactive=True)
            print("=== LOGIN OK ===")
            print(f"Auth file: {AUTH_JSON_PATH}")
            if account_id:
                print(f"Account ID: {account_id}")
            return 0

        print(f"Prompt: {args.prompt}\n")
        answer = call_codex_backend(args.prompt)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=== RESPONSE ===")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
