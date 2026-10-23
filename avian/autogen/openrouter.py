"""Send pregen.gen_one's Gemini request through OpenRouter instead.

pregen.py (upstream) builds the whole request: the prompt, the species note,
and the captioned reference images (IMAGE 1 positive, IMAGE 2 lookalike,
IMAGE 3 style). Rather than fork that logic, this swaps urllib's urlopen for the
duration of the call: a request to pregen.GEMINI_URL is translated to an
OpenRouter chat completion with image output, part for part and in order, and
the reply is translated back into Gemini's shape. Everything else passes
through untouched, and HTTP errors (429/5xx for pregen's retry, 402 for an
empty balance) propagate as they would from Google.
"""
from __future__ import annotations

import contextlib
import json
import urllib.request

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash-image"


class _Reply:
    """Just enough of urlopen()'s result for pregen.gen_one."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def to_openrouter(payload: dict, model: str) -> dict:
    content = []
    for part in payload["contents"][0]["parts"]:
        if "text" in part:
            content.append({"type": "text", "text": part["text"]})
            continue
        data = part.get("inline_data") or part["inlineData"]
        mime = data.get("mime_type") or data["mimeType"]
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data['data']}"}})
    return {"model": model, "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"]}


def to_gemini(resp: dict) -> dict:
    parts = []
    for choice in resp.get("choices") or []:
        for image in (choice.get("message") or {}).get("images") or []:
            url = (image.get("image_url") or image.get("imageUrl") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                mime, data = url[len("data:"):].split(";base64,", 1)
                parts.append({"inline_data": {"mime_type": mime, "data": data}})
    finish = ((resp.get("choices") or [{}])[0]).get("finish_reason") or "?"
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}


@contextlib.contextmanager
def routed(gemini_url: str, key: str, model: str = DEFAULT_MODEL):
    real = urllib.request.urlopen

    def urlopen(req, *args, **kwargs):
        if getattr(req, "full_url", None) != gemini_url:
            return real(req, *args, **kwargs)
        body = to_openrouter(json.loads(req.data), model)
        forward = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "X-Title": "AvianVisitors autogen",
        })
        with real(forward, *args, **kwargs) as r:
            resp = json.loads(r.read())
        return _Reply(json.dumps(to_gemini(resp)).encode())

    urllib.request.urlopen = urlopen
    try:
        yield
    finally:
        urllib.request.urlopen = real
