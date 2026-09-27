"""Tiered, provider-swappable model access (the only file that talks to the LLM).

A fast model for bulk work and a judgment model for harder calls. All
Anthropic-specific request shaping lives here, so swapping providers means
editing only this file. On failure it raises ModelError with context rather than
returning a half-formed result, letting the caller decide how to degrade.

This project sends the model *text only*: minimized descriptor fields for
enrichment (enrich.py) and a sample of statement rows for the column-mapping
fallback (mapping.py). It never sends numbers it expects back as numbers: the
caller validates every response against a jsonschema, so the model cannot inject
or alter a figure that downstream code trusts. See
recurring-charge-auditor-SPEC.md sections 4, 6, 7.
"""

from __future__ import annotations

import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional; env vars may be set another way
    pass

FAST_MODEL = os.getenv("FAST_MODEL", "claude-sonnet-5")
JUDGMENT_MODEL = os.getenv("JUDGMENT_MODEL", "claude-opus-4-8")

_TIERS = {"fast": FAST_MODEL, "judgment": JUDGMENT_MODEL}


class ModelError(RuntimeError):
    """A model call failed or returned an unusable result."""


def model_for(tier: str) -> str:
    """Resolve a tier name ('fast' or 'judgment') to a concrete model id."""
    try:
        return _TIERS[tier]
    except KeyError:
        raise ModelError(f"unknown model tier: {tier!r}")


def _client():
    try:
        import anthropic
    except ImportError as e:
        raise ModelError(
            "the 'anthropic' package is not installed; run pip install -r requirements.txt"
        ) from e
    # The SDK resolves the API key from ANTHROPIC_API_KEY.
    return anthropic.Anthropic()


def _loads_json(text: str):
    """Parse a JSON value from model text, tolerating code fences or stray prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Last resort: slice out the outermost object or array.
        for open_c, close_c in (("{", "}"), ("[", "]")):
            i, j = s.find(open_c), s.rfind(close_c)
            if i != -1 and j > i:
                try:
                    return json.loads(s[i:j + 1])
                except json.JSONDecodeError:
                    continue
        raise ModelError("model returned no parseable JSON")


def _parse_response(response):
    """Check an Anthropic response for refusal or truncation, then parse its JSON."""
    if response.stop_reason == "refusal":
        raise ModelError("model declined the request (stop_reason=refusal)")
    if response.stop_reason == "max_tokens":
        raise ModelError("model output was truncated (stop_reason=max_tokens); raise max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ModelError("model returned no text block to parse")
    return _loads_json(text)


def complete_json(
    *,
    system: str,
    user: str,
    tier: str = "fast",
    max_tokens: int = 4000,
):
    """Text-only structured call: JSON (object or array) expected in the reply.

    The `user` prompt carries the required JSON shape; the caller validates the
    result against the schema. Raises ModelError on transport failure, refusal,
    truncation, or unparseable output.
    """
    client = _client()
    try:
        response = client.messages.create(
            model=model_for(tier),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
        )
    except Exception as e:  # boundary: wrap SDK/transport errors for the caller
        raise ModelError(f"model request failed: {e}") from e
    return _parse_response(response)
