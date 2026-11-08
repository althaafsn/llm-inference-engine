"""Model backends: a real Hugging Face causal LM and a deterministic fake."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerateRequest:
    prompt: str
    max_tokens: int = 32
    temperature: float = 0.0
    top_p: float = 1.0


@dataclass(frozen=True)
class GenerateResult:
    text: str
    tokens: int


class HFBackend:
    """A small causal LM loaded in-process with transformers."""

    def __init__(self, model_name: str, threads: int = 8):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        torch.set_num_threads(max(1, threads))
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.tokenizer.padding_side = "left"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        self.model = AutoModelForCausalLM.from_pretrained(model_name, dtype=torch.float32)
        self.model.eval()
        self.model_name = model_name

    def _prompt(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def generate_batch(self, requests: list[GenerateRequest]) -> list[GenerateResult]:
        if not requests:
            return []
        first = requests[0]
        prompts = [self._prompt(request.prompt) for request in requests]
        inputs = self.tokenizer(prompts, return_tensors="pt", padding=True)
        kwargs = {
            "max_new_tokens": first.max_tokens,
            "attention_mask": inputs.attention_mask,
            "pad_token_id": self.tokenizer.pad_token_id,
        }
        if first.temperature > 0:
            kwargs.update(
                do_sample=True,
                temperature=max(0.01, first.temperature),
                top_p=first.top_p,
            )
        else:
            kwargs["do_sample"] = False
        with self.torch.inference_mode():
            output = self.model.generate(input_ids=inputs.input_ids, **kwargs)
        width = inputs.input_ids.shape[1]
        results = []
        for row in range(len(requests)):
            generated = output[row, width:]
            text = self.tokenizer.decode(generated, skip_special_tokens=True).strip()
            results.append(GenerateResult(text=text, tokens=int(generated.numel())))
        return results


class FakeBackend:
    """Deterministic backend for tests and for a no-model smoke test."""

    def __init__(self, delay_ms: float = 2.0, fail: bool = False):
        self.delay_ms = delay_ms
        self.fail = fail
        self.calls = 0
        self.batch_sizes: list[int] = []

    def generate_batch(self, requests: list[GenerateRequest]) -> list[GenerateResult]:
        if self.fail:
            raise RuntimeError("fake backend failure")
        time.sleep(self.delay_ms / 1000.0)
        self.calls += 1
        self.batch_sizes.append(len(requests))
        results = []
        for request in requests:
            words = request.prompt.split()
            tokens = max(1, min(request.max_tokens, len(words) + 1))
            results.append(GenerateResult(text=f"fake: {request.prompt}", tokens=tokens))
        return results


def build_backend(name: str, model: str, threads: int = 8):
    if name == "fake":
        return FakeBackend()
    if name == "hf":
        return HFBackend(model, threads=threads)
    raise ValueError(f"unknown backend: {name}")
