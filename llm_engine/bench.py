"""Local LLM benchmark: normal, admission, and gateway failover."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
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


async def run_load(
    url: str,
    rate: float,
    duration: float,
    concurrency: int,
    max_tokens: int,
    repeat: bool,
    timeout: float,
) -> tuple[list[dict], float]:
    started = time.monotonic()
    semaphore = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    async with aiohttp.ClientSession() as session:

        async def one(index: int) -> None:
            async with semaphore:
                prompt = PROMPTS[0] if repeat else PROMPTS[index % len(PROMPTS)]
                request_id = f"bench-{index}"
                row = {"request_id": request_id, "status": None, "success": False}
                begin = time.monotonic()
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
                        row["status"] = response.status
                        body = await response.json()
                    if response.status == 200:
                        row["success"] = True
                        row["tokens"] = body.get("tokens", 0)
                        row["cached"] = body.get("cached", False)
                        row["worker"] = body.get("worker")
                        row["gateway"] = body.get("gateway")
                except Exception as exc:
                    row["error"] = type(exc).__name__
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
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--rate", type=float, default=4.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--admission-max", type=int, default=0)
    parser.add_argument("--routing", choices=("hash", "least_loaded"), default="least_loaded")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--kill-after", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    processes: list[subprocess.Popen] = []
    workers: list[str] = []
    gateways: list[str] = []
    try:
        used_ports: set[int] = set()
        worker_ports = [free_port(used_ports) for _ in range(args.workers)]
        worker_processes = []
        for index, port in enumerate(worker_ports):
            process = start_process(
                f"worker-{index + 1}",
                [
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
                ],
                output,
            )
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
                    "--routing",
                    args.routing,
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
        )
        if killer_task:
            await killer_task
        summary = summarize(rows, elapsed)
        summary["mode"] = args.mode
        summary["config"] = {
            "model": args.model,
            "backend": args.backend,
            "workers": args.workers,
            "threads": args.threads,
            "rate": args.rate,
            "duration": args.duration,
            "concurrency": args.concurrency,
            "max_tokens": args.max_tokens,
            "admission_max": args.admission_max,
            "routing": args.routing,
            "repeat": args.repeat,
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
