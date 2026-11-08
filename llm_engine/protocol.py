"""Request validation shared by the gateway and the worker."""

from __future__ import annotations

from .backend import GenerateRequest


def parse_generate_request(data: dict) -> GenerateRequest:
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4096:
        raise ValueError("prompt must be a string of 1-4096 characters")
    max_tokens = data.get("max_tokens", 32)
    temperature = data.get("temperature", 0.0)
    top_p = data.get("top_p", 1.0)
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not 1 <= max_tokens <= 256
    ):
        raise ValueError("max_tokens must be an integer of 1-256")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise ValueError("temperature must be a number")
    if not 0.0 <= float(temperature) <= 2.0:
        raise ValueError("temperature must be between 0 and 2")
    if not isinstance(top_p, (int, float)) or isinstance(top_p, bool):
        raise ValueError("top_p must be a number")
    if not 0.0 < float(top_p) <= 1.0:
        raise ValueError("top_p must be between 0 and 1")
    return GenerateRequest(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=float(temperature),
        top_p=float(top_p),
    )
