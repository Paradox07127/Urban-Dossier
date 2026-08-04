#!/usr/bin/env python3
"""Smoke-test OpenClaw OpenResponses routing without printing credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18789")
    parser.add_argument("--agent", default="urban-dossier")
    parser.add_argument(
        "--token-file",
        default="/mnt/data/urban-dossier-state/runtime/openclaw-gateway.token",
    )
    parser.add_argument("--session", default="gateway-route-smoke")
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip().strip('"')
    response = httpx.post(
        f"{args.url.rstrip('/')}/v1/responses",
        headers={
            "Authorization": f"Bearer {token}",
            "x-openclaw-agent-id": args.agent,
            "x-openclaw-session-key": args.session,
        },
        json={
            "model": f"openclaw/{args.agent}",
            "input": "Reply exactly: gateway-route-ok",
            # Nemotron may emit hidden reasoning before the exact short reply.
            # A tiny limit can make a healthy route look broken.
            "max_output_tokens": 1024,
        },
        timeout=60,
    )
    safe = {"status": response.status_code}
    if response.is_success:
        payload = response.json()
        safe["id"] = payload.get("id")
        texts = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if isinstance(content.get("text"), str):
                    texts.append(content["text"])
        safe["text"] = "".join(texts)
    else:
        safe["error"] = response.text[:500]
    print(json.dumps(safe, ensure_ascii=False))
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
