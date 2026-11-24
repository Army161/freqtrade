"""LLM providers for the agent layer.

* anthropic : Claude via the official Anthropic SDK (default model claude-opus-5-5)
* xai       : Grok via xAI's HTTP API (https://api.x.ai/v1/chat/completions)
* rules     : deterministic fallback, no network, always available

Agents only ever receive aggregated metrics and return JSON proposals; they never get
exchange keys and never call exchanges directly.
"""

from __future__ import annotations

import json
import os
import re

import httpx


class ProviderError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ProviderError("no JSON object in model output")
    return json.loads(m.group(0))


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium"):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def complete_json(self, system: str, user: str) -> dict:
        import anthropic
        try:
            # Server-side refusal fallback (beta) keeps the agent loop alive if the primary
            # model declines; passed via extra_body so it works across SDK versions.
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            )
        except anthropic.AuthenticationError as e:
            raise ProviderError(f"anthropic auth failed: {e}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("anthropic rate limited") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"anthropic connection error: {e}") from e
        if resp.stop_reason == "refusal":
            raise ProviderError("model declined the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return extract_json(text)


class XAIProvider:
    name = "xai"

    def __init__(self, model: str | None = None):
        self.key = os.environ["XAI_API_KEY"]
        self.model = model or os.environ.get("XAI_MODEL", "grok-4")

    def complete_json(self, system: str, user: str) -> dict:
        r = httpx.post("https://api.x.ai/v1/chat/completions", timeout=120,
                       headers={"Authorization": f"Bearer {self.key}"},
                       json={"model": self.model, "temperature": 0.2,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        if r.status_code != 200:
            raise ProviderError(f"xai HTTP {r.status_code}: {r.text[:300]}")
        return extract_json(r.json()["choices"][0]["message"]["content"])


class RulesProvider:
    """Deterministic stand-in so the station is fully autonomous without any API key."""
    name = "rules"

    def complete_json(self, system: str, user: str) -> dict:
        return {"_rules": True}


def make_providers(cfg: dict) -> list:
    """Ordered list of providers to try (first that works wins); rules is always last."""
    order = cfg.get("providers", ["anthropic", "xai"])
    out = []
    for p in order:
        try:
            if p == "anthropic" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                out.append(AnthropicProvider(cfg.get("anthropic_model", "claude-opus-5-5"),
                                             cfg.get("anthropic_effort", "medium")))
            elif p == "xai" and os.environ.get("XAI_API_KEY"):
                out.append(XAIProvider(cfg.get("xai_model")))
        except Exception:  # noqa: BLE001 - missing SDK etc.
            continue
    out.append(RulesProvider())
    return out
