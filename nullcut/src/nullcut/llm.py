"""OpenAI-compatible client for every model server nullcut talks to (plan §3, §11).

vLLM, llama.cpp and the Council router all speak this protocol, so a model
swap is an environment change. The cloud planner is a second endpoint that
stays off unless NULLCUT_CLOUD_PLANNER=1; it only ever receives text, never
footage.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Endpoint:
    url: str
    model: str
    key: str = ""
    timeout: float = 300.0

    @property
    def label(self) -> str:
        return f"{self.model} @ {self.url}"


def _env_endpoint(prefix: str, default_url: str | None, default_model: str) -> Endpoint | None:
    url = os.environ.get(f"{prefix}_URL", default_url or "")
    if not url:
        return None
    return Endpoint(url.rstrip("/"), os.environ.get(f"{prefix}_MODEL", default_model),
                    os.environ.get(f"{prefix}_KEY", ""))


def planner_endpoint() -> Endpoint | None:
    """Local Council seat by default; the cloud switch routes text-only planning out."""
    if os.environ.get("NULLCUT_CLOUD_PLANNER") == "1":
        cloud = _env_endpoint("NULLCUT_CLOUD_LLM", None, "")
        if cloud and cloud.model:
            return cloud
    if os.environ.get("NULLCUT_LLM") == "off":
        return None
    return _env_endpoint("NULLCUT_LLM", "http://127.0.0.1:8000/v1", "glm-4.5-air")


def vlm_endpoint() -> Endpoint | None:
    # No default: scene captions are optional and a missing VLM must cost
    # nothing, not a connection timeout per keyframe.
    return _env_endpoint("NULLCUT_VLM", None, "molmo2-8b")


def _headers(ep: Endpoint) -> dict:
    h = {"Content-Type": "application/json"}
    if ep.key:
        h["Authorization"] = f"Bearer {ep.key}"
    return h


def ping(ep: Endpoint | None, timeout: float = 2.0) -> bool:
    if ep is None:
        return False
    try:
        r = httpx.get(f"{ep.url}/models", headers=_headers(ep), timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def chat(ep: Endpoint, messages: list[dict], *, tools: list[dict] | None = None,
         schema: dict | None = None, schema_name: str = "output", max_tokens: int = 4096,
         temperature: float = 0.2) -> dict:
    """One chat completion; returns the assistant message dict.

    `schema` asks the server for guided JSON decoding (vLLM, llama.cpp and
    OpenAI all accept json_schema response_format). Callers still validate:
    a server that ignores the field must not be able to slip bad JSON through.
    """
    body: dict = {"model": ep.model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema_name, "schema": schema,
                                                   "strict": True}}
    try:
        r = httpx.post(f"{ep.url}/chat/completions", json=body, headers=_headers(ep),
                       timeout=ep.timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"{ep.label}: {exc}") from exc
    if r.status_code != 200:
        raise LLMError(f"{ep.label}: HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"{ep.label}: malformed response: {r.text[:300]}") from exc


def parse_json(text: str | None) -> dict:
    """Parse model JSON, tolerating a ```json fence some servers add despite the schema."""
    if not text:
        raise ValueError("empty response")
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t)


def image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}
