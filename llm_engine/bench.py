"""Local LLM benchmark: normal, admission, and gateway failover."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]

PROMPTS = [
    "Explain why a queue can grow without bound.",
    "Give one reason to cap in-flight requests.",
    "What is the difference between p50 and p95 latency?",
    "Describe a circuit breaker in one sentence.",
    "Why does batching improve throughput?",
    "What is a cache hit rate?",
    "Explain consistent hashing briefly.",
    "Why is a health check interval important?",
    "What does HTTP 503 mean?",
    "Why add jitter to retries?",
    "Describe a load balancer in one sentence.",
    "What is tail latency?",
    "Why measure accepted throughput?",
    "What is the risk of an unbounded queue?",
    "Explain graceful shutdown briefly.",
    "What is a token in an LLM?",
    "Why can a larger batch hurt latency?",
    "What is the purpose of a retry budget?",
    "Describe admission control in one sentence.",
    "Why is a cold cache slower?",
]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 3)


def free_port(used: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in used:
            used.add(port)
            return port


def start_process(name: str, args: list[str], output: Path) -> subprocess.Popen:
    log_dir = output / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    process = subprocess.Popen(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    process._log = log  # type: ignore[attr-defined]
    process._log_path = log_path  # type: ignore[attr-defined]
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    log = getattr(process, "_log", None)
    if log:
        log.close()


async def wait_health(
    url: str, timeout: float = 300.0, process: subprocess.Popen | None = None
) -> None:
    deadline = time.monotonic() + timeout
    last = None
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                path = getattr(process, "_log_path", None)
                tail = path.read_text()[-1200:] if path and path.exists() else ""
                raise RuntimeError(f"process for {url} exited with {process.returncode}: {tail}")
            try:
                async with session.get(url + "/health", timeout=3) as response:
                    if response.status == 200:
                        return
                    last = f"status {response.status}"
            except Exception as exc:
                last = str(exc)
            await asyncio.sleep(1.0)
    raise RuntimeError(f"health check failed for {url}: {last}")


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


def retry_delay_seconds(
    attempt: int,
    retry_after: float | None,
    backoff_ms: float,
    max_backoff_ms: float,
    jitter: float,
) -> float:
    base_ms = min(max_backoff_ms, backoff_ms * (2 ** max(0, attempt - 1)))
    delay_ms = max(base_ms, (retry_after or 0.0) * 1000.0)
    if jitter > 0:
        delay_ms *= 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, delay_ms / 1000.0)


async def fetch_stats(url: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url.rstrip("/") + "/stats", timeout=10) as response:
                if response.status == 200:
                    return await response.json()
    except Exception:
        return None
    return None


async def run_load(
    url: str,
    rate: float,
    duration: float,
    concurrency: int,
    max_tokens: int,
    repeat: bool,
    timeout: float,
    prompt_prefix: str = "",
    retry_attempts: int = 0,
    retry_backoff_ms: float = 100.0,
    retry_max_backoff_ms: float = 1000.0,
    retry_jitter: float = 0.2,
) -> tuple[list[dict], float]:
    started = time.monotonic()
    semaphore = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    async with aiohttp.ClientSession() as session:

        async def one(index: int) -> None:
            async with semaphore:
                base_prompt = PROMPTS[0] if repeat else PROMPTS[index % len(PROMPTS)]
                prompt = f"{prompt_prefix} {base_prompt}".strip()
                request_id = f"bench-{index}"
                row = {
                    "request_id": request_id,
                    "status": None,
                    "success": False,
                    "attempts": 0,
                }
                begin = time.monotonic()
                retried = False
                attempt = 0
                while True:
                    attempt += 1
                    row["attempts"] = attempt
                    status = None
                    retry_after = None
                    try:
                        async with session.post(
                            url + "/generate",
                            json={
                                "request_id": request_id,
                                "prompt": prompt,
                                "max_tokens": max_tokens,
                                "temperature": 0.0,
                            },
                            timeout=timeout,
                        ) as response:
                            status = response.status
                            retry_after = parse_retry_after(response.headers.get("Retry-After"))
                            text = await response.text()
                        row["status"] = status
                        if status == 200:
                            body = json.loads(text)
                            row["success"] = True
                            row["tokens"] = body.get("tokens", 0)
                            row["cached"] = body.get("cached", False)
                            row["worker"] = body.get("worker")
                            row["gateway"] = body.get("gateway")
                            break
                        if status not in (502, 503, 504) or attempt > retry_attempts:
                            row["error"] = f"http_{status}"
                            break
                    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                        row["status"] = None
                        row["error"] = type(exc).__name__
                        if attempt > retry_attempts:
                            break
                    retried = True
                    row["retried"] = True
                    await asyncio.sleep(
                        retry_delay_seconds(
                            attempt,
                            retry_after,
                            retry_backoff_ms,
                            retry_max_backoff_ms,
                            retry_jitter,
                        )
                    )
                row["retry_success"] = retried and bool(row.get("success"))
                row["retry_exhausted"] = retried and not bool(row.get("success"))
                row["latency_ms"] = round((time.monotonic() - begin) * 1000.0, 3)
                rows.append(row)

        tasks = []
        count = max(1, int(rate * duration))
        for index in range(count):
            target = started + index / rate
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(index)))
        await asyncio.gather(*tasks)
    elapsed = time.monotonic() - started
    return rows, elapsed


def summarize(rows: list[dict], elapsed: float) -> dict:
    successes = [row for row in rows if row.get("success")]
    statuses = Counter(str(row.get("status")) for row in rows)
    latency = [row["latency_ms"] for row in successes]
    tokens = sum(int(row.get("tokens") or 0) for row in successes)
    return {
        "requests": len(rows),
        "successes": len(successes),
        "failures": len(rows) - len(successes),
        "status_counts": dict(statuses),
        "wall_seconds": round(elapsed, 3),
        "successful_requests_per_second": round(len(successes) / elapsed, 3) if elapsed else 0.0,
        "tokens_per_second": round(tokens / elapsed, 3) if elapsed else 0.0,
        "cache_hits": sum(1 for row in successes if row.get("cached")),
        "retry": {
            "retried_requests": sum(1 for row in rows if row.get("retried")),
            "retry_successes": sum(1 for row in rows if row.get("retry_success")),
            "retry_exhausted": sum(1 for row in rows if row.get("retry_exhausted")),
            "attempts_total": sum(int(row.get("attempts", 1)) for row in rows),
        },
        "latency_ms": {
            "p50": percentile(latency, 0.50),
            "p95": percentile(latency, 0.95),
            "p99": percentile(latency, 0.99),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("normal", "admission", "failover"), default="normal")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--backend", choices=("hf", "fake"), default="hf")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--quantize", action="store_true")
    parser.add_argument("--attn", choices=("sdpa", "eager"), default="sdpa")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--interop-threads", type=int, default=1)
    parser.add_argument("--batch-window-ms", type=float, default=10.0)
    parser.add_argument("--max-batch-size", type=int, default=8)
    parser.add_argument("--cache-capacity", type=int, default=256)
    parser.add_argument("--rate", type=float, default=4.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--admission-max", type=int, default=0)
    parser.add_argument("--admission-adaptive", action="store_true")
    parser.add_argument("--admission-min", type=int, default=1)
    parser.add_argument("--admission-target-p95-ms", type=float, default=400.0)
    parser.add_argument("--admission-interval-ms", type=float, default=1000.0)
    parser.add_argument("--routing", choices=("hash", "least_loaded"), default="least_loaded")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--prompt-prefix", default="")
    parser.add_argument("--kill-after", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retry-attempts", type=int, default=0)
    parser.add_argument("--retry-backoff-ms", type=float, default=100.0)
    parser.add_argument("--retry-max-backoff-ms", type=float, default=1000.0)
    parser.add_argument("--retry-jitter", type=float, default=0.2)
    parser.add_argument("--target", help="existing base URL; skip process startup")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.retry_attempts < 0:
        parser.error("--retry-attempts must be non-negative")
    if args.retry_backoff_ms < 0 or args.retry_max_backoff_ms < 0:
        parser.error("retry backoff must be non-negative")
    if not 0 <= args.retry_jitter <= 1:
        parser.error("--retry-jitter must be between 0 and 1")
    if args.batch_window_ms < 0:
        parser.error("--batch-window-ms must be non-negative")
    if args.max_batch_size < 1:
        parser.error("--max-batch-size must be at least 1")
    if args.cache_capacity < 0:
        parser.error("--cache-capacity must be non-negative")
    if args.admission_min < 0:
        parser.error("--admission-min must be non-negative")
    return args


async def run(args: argparse.Namespace) -> int:
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    if args.target:
        rows, elapsed = await run_load(
            args.target.rstrip("/"),
            args.rate,
            args.duration,
            args.concurrency,
            args.max_tokens,
            args.repeat,
            args.timeout,
            args.prompt_prefix,
            args.retry_attempts,
            args.retry_backoff_ms,
            args.retry_max_backoff_ms,
            args.retry_jitter,
        )
        summary = summarize(rows, elapsed)
        summary["gateway_stats"] = await fetch_stats(args.target)
        summary["mode"] = args.mode
        summary["config"] = {
            "target": args.target,
            "backend": args.backend,
            "rate": args.rate,
            "duration": args.duration,
            "concurrency": args.concurrency,
            "max_tokens": args.max_tokens,
            "routing": args.routing,
            "prompt_prefix": args.prompt_prefix,
            "retry": {
                "attempts": args.retry_attempts,
                "backoff_ms": args.retry_backoff_ms,
                "max_backoff_ms": args.retry_max_backoff_ms,
                "jitter": args.retry_jitter,
            },
        }
        (output / "requests.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if summary["successes"] > 0 else 1
    processes: list[subprocess.Popen] = []
    workers: list[str] = []
    gateways: list[str] = []
    try:
        used_ports: set[int] = set()
        worker_ports = [free_port(used_ports) for _ in range(args.workers)]
        worker_processes = []
        for index, port in enumerate(worker_ports):
            worker_command = [
                "llm_engine.worker",
                "--port",
                str(port),
                "--id",
                f"worker-{index + 1}",
                "--model",
                args.model,
                "--backend",
                args.backend,
                "--threads",
                str(args.threads),
                "--attn",
                args.attn,
                "--interop-threads",
                str(args.interop_threads),
                "--batch-window-ms",
                str(args.batch_window_ms),
                "--max-batch-size",
                str(args.max_batch_size),
                "--cache-capacity",
                str(args.cache_capacity),
            ]
            if args.quantize:
                worker_command.append("--quantize")
            if args.warmup:
                worker_command.append("--warmup")
            process = start_process(f"worker-{index + 1}", worker_command, output)
            processes.append(process)
            worker_processes.append(process)
            workers.append(f"http://127.0.0.1:{port}")
        for process, worker in zip(worker_processes, workers, strict=True):
            await wait_health(
                worker,
                timeout=300 if args.backend == "hf" else 30,
                process=process,
            )
        print(f"workers ready: {workers}", flush=True)

        gateway_ports = [free_port(used_ports) for _ in range(2 if args.mode == "failover" else 1)]
        gateway_processes = []
        for index, port in enumerate(gateway_ports):
            process = start_process(
                f"gateway-{index + 1}",
                [
                    "llm_engine.gateway",
                    "--port",
                    str(port),
                    "--id",
                    f"gateway-{index + 1}",
                    "--workers",
                    *workers,
                    "--admission-max",
                    str(args.admission_max),
                    "--admission-min",
                    str(args.admission_min),
                    "--admission-target-p95-ms",
                    str(args.admission_target_p95_ms),
                    "--admission-interval-ms",
                    str(args.admission_interval_ms),
                    "--routing",
                    args.routing,
                    *(["--admission-adaptive"] if args.admission_adaptive else []),
                ],
                output,
            )
            processes.append(process)
            gateway_processes.append(process)
            gateways.append(f"http://127.0.0.1:{port}")
        for process, gateway in zip(gateway_processes, gateways, strict=True):
            await wait_health(gateway, timeout=30, process=process)
        print(f"gateways ready: {gateways}", flush=True)

        target = gateways[0]
        if args.mode == "failover":
            proxy_port = free_port(used_ports)
            proxy_process = start_process(
                "proxy",
                [
                    "llm_engine.ha_proxy",
                    "--port",
                    str(proxy_port),
                    "--upstreams",
                    *gateways,
                    "--health-interval",
                    "1.0",
                ],
                output,
            )
            processes.append(proxy_process)
            await wait_health(f"http://127.0.0.1:{proxy_port}", timeout=30, process=proxy_process)
            target = f"http://127.0.0.1:{proxy_port}"
            print("proxy ready", flush=True)

        async def killer() -> None:
            await asyncio.sleep(args.kill_after)
            print("killing gateway-1", flush=True)
            gateway_processes[0].send_signal(signal.SIGTERM)

        killer_task = asyncio.create_task(killer()) if args.mode == "failover" else None
        rows, elapsed = await run_load(
            target,
            args.rate,
            args.duration,
            args.concurrency,
            args.max_tokens,
            args.repeat,
            args.timeout,
            args.prompt_prefix,
            args.retry_attempts,
            args.retry_backoff_ms,
            args.retry_max_backoff_ms,
            args.retry_jitter,
        )
        if killer_task:
            await killer_task
        summary = summarize(rows, elapsed)
        summary["gateway_stats"] = await fetch_stats(target)
        summary["mode"] = args.mode
        summary["config"] = {
            "model": args.model,
            "backend": args.backend,
            "workers": args.workers,
            "threads": args.threads,
            "quantize": args.quantize,
            "attn": args.attn,
            "warmup": args.warmup,
            "interop_threads": args.interop_threads,
            "rate": args.rate,
            "duration": args.duration,
            "concurrency": args.concurrency,
            "max_tokens": args.max_tokens,
            "admission_max": args.admission_max,
            "admission_adaptive": args.admission_adaptive,
            "admission_min": args.admission_min,
            "admission_target_p95_ms": args.admission_target_p95_ms,
            "admission_interval_ms": args.admission_interval_ms,
            "retry": {
                "attempts": args.retry_attempts,
                "backoff_ms": args.retry_backoff_ms,
                "max_backoff_ms": args.retry_max_backoff_ms,
                "jitter": args.retry_jitter,
            },
            "routing": args.routing,
            "repeat": args.repeat,
            "batch_window_ms": args.batch_window_ms,
            "max_batch_size": args.max_batch_size,
            "cache_capacity": args.cache_capacity,
        }
        (output / "requests.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if summary["successes"] > 0 else 1
    finally:
        for process in reversed(processes):
            stop_process(process)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
