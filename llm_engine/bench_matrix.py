"""Run the local LLM optimization matrix and write one combined JSON."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CONFIGS = [
    {"name": "base", "threads": 4, "batch_window_ms": 10, "admission_max": 2},
    {"name": "threads-2", "threads": 2, "batch_window_ms": 10, "admission_max": 2},
    {"name": "threads-8", "threads": 8, "batch_window_ms": 10, "admission_max": 2},
    {"name": "batch-0", "threads": 4, "batch_window_ms": 0, "admission_max": 2},
    {"name": "batch-25", "threads": 4, "batch_window_ms": 25, "admission_max": 2},
    {"name": "max-batch-4", "threads": 4, "max_batch_size": 4, "admission_max": 2},
    {"name": "admission-0", "threads": 4, "admission_max": 0},
    {"name": "admission-1", "threads": 4, "admission_max": 1},
    {"name": "admission-4", "threads": 4, "admission_max": 4},
    {"name": "routing-hash", "threads": 4, "admission_max": 2, "routing": "hash"},
    {
        "name": "cache-repeat",
        "threads": 4,
        "admission_max": 2,
        "routing": "hash",
        "repeat": True,
    },
    {
        "name": "model-135m",
        "threads": 4,
        "admission_max": 2,
        "model": "HuggingFaceTB/SmolLM2-135M-Instruct",
    },
    {
        "name": "adaptive",
        "threads": 4,
        "admission_max": 4,
        "admission_adaptive": True,
        "admission_min": 1,
        "admission_target_p95_ms": 800,
    },
    {
        "name": "retry",
        "threads": 4,
        "admission_max": 2,
        "retry_attempts": 3,
    },
]


def build_command(config: dict, output: Path, rate: float, duration: float) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "llm_engine.bench",
        "--mode",
        "normal",
        "--workers",
        "2",
        "--rate",
        str(rate),
        "--duration",
        str(duration),
        "--concurrency",
        "8",
        "--max-tokens",
        "8",
        "--threads",
        str(config.get("threads", 4)),
        "--batch-window-ms",
        str(config.get("batch_window_ms", 10)),
        "--max-batch-size",
        str(config.get("max_batch_size", 8)),
        "--cache-capacity",
        str(config.get("cache_capacity", 256)),
        "--admission-max",
        str(config.get("admission_max", 0)),
        "--routing",
        config.get("routing", "least_loaded"),
        "--model",
        config.get("model", "Qwen/Qwen2.5-0.5B-Instruct"),
        "--prompt-prefix",
        config["name"],
        "--retry-attempts",
        str(config.get("retry_attempts", 0)),
        "--output",
        str(output),
    ]
    if config.get("repeat"):
        command.append("--repeat")
    if config.get("admission_adaptive"):
        command += [
            "--admission-adaptive",
            "--admission-min",
            str(config.get("admission_min", 1)),
            "--admission-target-p95-ms",
            str(config.get("admission_target_p95_ms", 400)),
        ]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("/tmp/opencode/llm-matrix"))
    parser.add_argument("--json", type=Path, default=ROOT / "benchmarks/llm-optimization.json")
    parser.add_argument("--only", nargs="*", help="run only these named configs")
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=5.0)
    args = parser.parse_args(argv)

    configs = CONFIGS
    if args.only:
        configs = [config for config in CONFIGS if config["name"] in set(args.only)]
        if not configs:
            parser.error("no matching configs")

    args.output_root.mkdir(parents=True, exist_ok=True)
    results = []
    for config in configs:
        name = config["name"]
        output = args.output_root / name
        print(f"=== {name} ===", flush=True)
        started = time.monotonic()
        completed = subprocess.run(
            build_command(config, output, args.rate, args.duration),
            cwd=ROOT,
            check=False,
        )
        elapsed = time.monotonic() - started
        summary_path = output / "summary.json"
        if completed.returncode != 0 or not summary_path.exists():
            print(f"FAILED: {name} (exit {completed.returncode})", file=sys.stderr)
            results.append({"name": name, "config": config, "error": "run failed"})
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["name"] = name
        summary["config"].update(config)
        summary["local_wall_seconds"] = round(elapsed, 3)
        results.append(summary)
        print(json.dumps(summary, indent=2), flush=True)

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"results": results}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.json}", flush=True)
    return 0 if all("error" not in result for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
