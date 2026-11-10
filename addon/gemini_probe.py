#!/usr/bin/env python3
"""Direct fixed-prompt Gemini API probe; never print key, response or URL params."""
import json
import os
import sys
import urllib.error
import urllib.request

URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
PROMPT = "Reply with exactly the word READY and nothing else."


def probe(key, opener=urllib.request.urlopen):
    if not isinstance(key, str) or not key.strip():
        return 11
    payload = json.dumps({"contents": [{"parts": [{"text": PROMPT}]}],
                          "generationConfig": {"temperature": 0}}).encode("utf-8")
    req = urllib.request.Request(URL, data=payload, method="POST", headers={
        "x-goog-api-key": key, "Content-Type": "application/json",
        "User-Agent": "go-aider-key-verification"})
    try:
        with opener(req, timeout=25) as response:
            if response.status != 200:
                return 14
            data = json.loads(response.read(60_000))
    except urllib.error.HTTPError as error:
        # Do not read/print response body or request; only fixed HTTP classes.
        if error.code in (401, 403):
            return 11
        if error.code == 429:
            return 12
        if error.code in (400, 404):
            return 13
        return 14
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 14
    try:
        result = " ".join(part.get("text", "") for candidate in data["candidates"]
                          for part in candidate["content"]["parts"])
    except (KeyError, TypeError):
        return 15
    return 0 if result.strip() == "READY" else 15


if __name__ == "__main__":
    code = probe(os.environ.get("GEMINI_API_KEY", ""))
    # Exit status is enough to diagnose without printing the provider's raw output.
    raise SystemExit(code)
