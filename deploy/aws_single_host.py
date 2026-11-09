#!/usr/bin/env python3
"""Deploy the LLM stack to one AWS host and run the normal/failover/recovery tests."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGION = "us-east-1"
AMI = "ami-0820d0c589175bc01"
INSTANCE_TYPE = "c6i.2xlarge"
TAG_KEY = "temporary-llm-benchmark"
CAP_USD = 1.50
HOURLY_USD = 0.34
RUN_LIMIT_SECONDS = 45 * 60


def command(args: list[str], *, timeout: int = 60, check: bool = True, **kwargs):
    return subprocess.run(args, timeout=timeout, check=check, text=True, **kwargs)


def aws(*args: str, timeout: int = 60) -> object:
    result = command(
        ["aws", *args, "--region", REGION, "--no-cli-pager", "--output", "json"],
        timeout=timeout,
        capture_output=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def identity() -> str:
    return str(aws("sts", "get-caller-identity")["Account"])


def public_ipv4() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com/", timeout=10) as response:
        return response.read(100).decode("ascii").strip()


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def preflight() -> dict:
    for executable in ("aws", "ssh", "scp", "ssh-keygen", "tar", "git"):
        if not shutil.which(executable):
            raise RuntimeError(f"missing local command: {executable}")
    account = identity()
    images = aws("ec2", "describe-images", "--image-ids", AMI)["Images"]
    if len(images) != 1 or images[0]["State"] != "available":
        raise RuntimeError("the pinned AMI is unavailable")
    subnets = aws("ec2", "describe-subnets", "--filters", "Name=default-for-az,Values=true")
    candidates = [
        subnet
        for subnet in subnets["Subnets"]
        if subnet.get("MapPublicIpOnLaunch") and subnet["State"] == "available"
    ]
    if not candidates:
        raise RuntimeError("no default public-IP subnet is available")
    selected = sorted(candidates, key=lambda item: item["AvailabilityZone"])[0]
    active = aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag-key,Values={TAG_KEY}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    )
    if active.get("Reservations"):
        raise RuntimeError("active tagged instances exist; clean them up first")
    volumes = aws(
        "ec2",
        "describe-volumes",
        "--filters",
        f"Name=tag-key,Values={TAG_KEY}",
    )
    if volumes.get("Volumes"):
        raise RuntimeError("tagged volumes exist; clean them up first")
    address = public_ipv4()
    print(f"Account {account}, region {REGION}, instance {INSTANCE_TYPE}, AMI {AMI}")
    print(f"Subnet {selected['SubnetId']}, SSH ingress limited to {address}/32")
    print(f"Budget cap ${CAP_USD:.2f}; estimated {HOURLY_USD:.2f}/hour")
    print("Preflight does not prove launch, termination, data transfer, or cost.")
    return {
        "account": account,
        "region": REGION,
        "subnet": selected["SubnetId"],
        "vpc": selected["VpcId"],
        "public_ip": address,
    }


def user_data() -> str:
    return """#!/bin/bash
set -eux
if ! shutdown -h +60; then poweroff; exit 1; fi
dnf install -y docker tar gzip
systemctl enable --now docker
usermod -aG docker ec2-user
mkdir -p /usr/local/lib/docker/cli-plugins
COMPOSE_URL=https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64
curl -fsSL "$COMPOSE_URL" -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
BUILDX_URL=$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(next(a["browser_download_url"] for a in data["assets"] if a["name"].endswith("linux-amd64")))
')
curl -fsSL "$BUILDX_URL" -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
sudo docker buildx version
sudo docker compose version
"""


def ssh_options(key: Path, known_hosts: Path) -> list[str]:
    return [
        "-i",
        str(key),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
    ]


def wait_ssh(host: str, key: Path, known_hosts: Path, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = command(
            ["ssh", *ssh_options(key, known_hosts), f"ec2-user@{host}", "true"],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(5)
    raise RuntimeError(f"SSH did not become ready: {last}")


def wait_docker(host: str, key: Path, known_hosts: Path, timeout: float = 600.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = command(
            [
                "ssh",
                *ssh_options(key, known_hosts),
                f"ec2-user@{host}",
                "command -v docker && sudo docker info >/dev/null 2>&1 "
                "&& sudo docker compose version >/dev/null 2>&1",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(5)
    bootstrap = command(
        [
            "ssh",
            *ssh_options(key, known_hosts),
            f"ec2-user@{host}",
            "sudo tail -120 /var/log/cloud-init-output.log",
        ],
        check=False,
        capture_output=True,
    )
    raise RuntimeError(f"Docker did not become ready: {last}\n{bootstrap.stdout[-3000:]}")


def ssh(host: str, key: Path, known_hosts: Path, remote: str, timeout: int = 1800) -> str:
    result = command(
        ["ssh", *ssh_options(key, known_hosts), f"ec2-user@{host}", remote],
        timeout=timeout,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"remote command failed: {remote}")
    print(result.stdout, end="")
    return result.stdout


def scp_to(host: str, key: Path, known_hosts: Path, source: Path, target: str) -> None:
    last = ""
    for _attempt in range(3):
        result = command(
            ["scp", *ssh_options(key, known_hosts), str(source), f"ec2-user@{host}:{target}"],
            timeout=600,
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(3)
    raise RuntimeError(f"scp to {host} failed: {last}")


def scp_from(host: str, key: Path, known_hosts: Path, source: str, target: Path) -> None:
    last = ""
    for _attempt in range(3):
        result = command(
            ["scp", *ssh_options(key, known_hosts), "-r", f"ec2-user@{host}:{source}", str(target)],
            timeout=600,
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(3)
    raise RuntimeError(f"scp from {host} failed: {last}")


def create_payload(directory: Path) -> Path:
    archive = directory / "project.tar.gz"
    command(
        [
            "tar",
            "-czf",
            str(archive),
            "--exclude=.git",
            "--exclude=.venv",
            "--exclude=__pycache__",
            "--exclude=.ruff_cache",
            "--exclude=deploy/runs",
            "--exclude=deploy/results",
            "--exclude=results",
            ".",
        ],
        cwd=ROOT,
        timeout=300,
    )
    return archive


def create_security_group(run_id: str, vpc: str, address: str) -> str:
    result = aws(
        "ec2",
        "create-security-group",
        "--group-name",
        f"{run_id}-ssh",
        "--description",
        "Temporary LLM deployment SSH",
        "--vpc-id",
        vpc,
        "--tag-specifications",
        json.dumps(
            [
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": TAG_KEY, "Value": run_id}],
                }
            ]
        ),
    )
    group = result["GroupId"]
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group,
        "--protocol",
        "tcp",
        "--port",
        "22",
        "--cidr",
        f"{address}/32",
    )
    return group


def launch_instance(state: dict, state_path: Path, directory: Path, key: Path) -> None:
    key_name = f"{state['run_id']}-key"
    key_pair = aws(
        "ec2",
        "create-key-pair",
        "--key-name",
        key_name,
        "--query",
        "KeyMaterial",
        "--output",
        "text",
    )
    key.write_text(key_pair, encoding="utf-8")
    os.chmod(key, 0o600)
    state["key_name"] = key_name
    state["key_path"] = str(key)
    save_state(state_path, state)

    userdata = directory / "userdata.sh"
    userdata.write_text(user_data(), encoding="utf-8")
    response = aws(
        "ec2",
        "run-instances",
        "--image-id",
        AMI,
        "--instance-type",
        INSTANCE_TYPE,
        "--key-name",
        key_name,
        "--security-group-ids",
        state["security_group"],
        "--subnet-id",
        state["subnet"],
        "--associate-public-ip-address",
        "--user-data",
        f"file://{userdata}",
        "--block-device-mappings",
        json.dumps(
            [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {"VolumeSize": 40, "VolumeType": "gp3", "DeleteOnTermination": True},
                }
            ]
        ),
        "--tag-specifications",
        json.dumps(
            [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": TAG_KEY, "Value": state["run_id"]}],
                }
            ]
        ),
        timeout=120,
    )
    instance = response["Instances"][0]["InstanceId"]
    state["instance"] = instance
    save_state(state_path, state)
    aws("ec2", "wait", "instance-running", "--instance-ids", instance, timeout=300)
    description = aws("ec2", "describe-instances", "--instance-ids", instance)["Reservations"][0][
        "Instances"
    ][0]
    state["host"] = description["PublicIpAddress"]
    save_state(state_path, state)
    print(f"launched {instance} at {state['host']}")


def wait_proxy(host: str, key: Path, known_hosts: Path, timeout: float = 1200.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = command(
            [
                "ssh",
                *ssh_options(key, known_hosts),
                f"ec2-user@{host}",
                "curl -fsS --max-time 5 http://127.0.0.1:8180/stats",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if all(item["healthy"] for item in data["upstreams"]):
                    print(json.dumps(data), flush=True)
                    return
                last = "not all gateways are healthy"
            except (ValueError, KeyError) as exc:
                last = str(exc)
        else:
            last = result.stderr
        time.sleep(10)
    raise RuntimeError(f"proxy did not become healthy: {last}")


def run_bench(
    host: str,
    key: Path,
    known_hosts: Path,
    label: str,
    rate: float,
    duration: float,
    max_tokens: int,
) -> None:
    ssh(
        host,
        key,
        known_hosts,
        "cd ~/llm && sudo docker compose -f deploy/docker-compose.yml run --rm "
        f"bench python -m llm_engine.bench --target http://proxy:8180 "
        f"--rate {rate} --duration {duration} --max-tokens {max_tokens} "
        f"--prompt-prefix {label} --output /results/{label}",
        timeout=900,
    )


def cleanup(state: dict) -> bool:
    ok = True
    host = state.get("host")
    key_path = state.get("key_path")
    if host and key_path:
        key = Path(key_path)
        known_hosts = Path(state["directory"]) / "known_hosts"
        try:
            ssh(
                host,
                key,
                known_hosts,
                "cd ~/llm && sudo docker compose -f deploy/docker-compose.yml down -v || true",
                timeout=300,
            )
        except Exception as exc:
            print(f"warning: compose cleanup failed: {exc}", file=sys.stderr)
    instance = state.get("instance")
    if instance:
        try:
            aws("ec2", "terminate-instances", "--instance-ids", instance)
            aws("ec2", "wait", "instance-terminated", "--instance-ids", instance, timeout=300)
        except Exception as exc:
            print(f"ERROR terminating instance: {exc}", file=sys.stderr)
            ok = False
    group = state.get("security_group")
    if group:
        for _ in range(12):
            try:
                aws("ec2", "delete-security-group", "--group-id", group)
                break
            except subprocess.CalledProcessError as exc:
                if "InvalidGroup.NotFound" in (exc.stderr or ""):
                    break
                time.sleep(5)
        else:
            print("ERROR deleting security group", file=sys.stderr)
            ok = False
    key_name = state.get("key_name")
    if key_name:
        try:
            aws("ec2", "delete-key-pair", "--key-name", key_name)
        except Exception as exc:
            print(f"warning: key pair cleanup failed: {exc}", file=sys.stderr)
    return ok


def run(args: argparse.Namespace) -> int:
    context = preflight()
    run_id = "llm-" + secrets.token_hex(6)
    directory = HERE / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    state_path = directory / "state.json"
    state = {
        "run_id": run_id,
        "account": context["account"],
        "region": REGION,
        "subnet": context["subnet"],
        "vpc": context["vpc"],
        "public_ip": context["public_ip"],
        "started_at": datetime.now(UTC).isoformat(),
        "directory": str(directory),
        "instance": None,
        "host": None,
        "security_group": None,
        "key_name": None,
        "key_path": None,
    }
    save_state(state_path, state)
    success = False
    try:
        state["security_group"] = create_security_group(
            run_id, context["vpc"], context["public_ip"]
        )
        save_state(state_path, state)
        key = directory / "key.pem"
        known_hosts = directory / "known_hosts"
        known_hosts.touch()
        launch_instance(state, state_path, directory, key)
        wait_ssh(state["host"], key, known_hosts)
        wait_docker(state["host"], key, known_hosts)
        print("SSH and Docker ready", flush=True)

        payload = create_payload(directory)
        scp_to(state["host"], key, known_hosts, payload, "~/project.tar.gz")
        ssh(
            state["host"],
            key,
            known_hosts,
            "rm -rf ~/llm && mkdir -p ~/llm && tar -xzf ~/project.tar.gz -C ~/llm",
            timeout=300,
        )
        print("building the Docker stack; this takes several minutes", flush=True)
        ssh(
            state["host"],
            key,
            known_hosts,
            "cd ~/llm && mkdir -p deploy/results && "
            "sudo docker compose -f deploy/docker-compose.yml up -d --build "
            "worker-1 worker-2 gateway-1 gateway-2 proxy",
            timeout=1800,
        )
        wait_proxy(state["host"], key, known_hosts)
        print("proxy healthy", flush=True)

        run_bench(state["host"], key, known_hosts, "normal", 2, 10, 8)
        ssh(
            state["host"],
            key,
            known_hosts,
            "cd ~/llm && sudo docker compose -f deploy/docker-compose.yml stop gateway-1",
            timeout=120,
        )
        time.sleep(2)
        run_bench(state["host"], key, known_hosts, "failover", 2, 10, 8)
        ssh(
            state["host"],
            key,
            known_hosts,
            "cd ~/llm && sudo docker compose -f deploy/docker-compose.yml start gateway-1",
            timeout=120,
        )
        time.sleep(8)
        wait_proxy(state["host"], key, known_hosts)
        run_bench(state["host"], key, known_hosts, "recovery", 2, 10, 8)

        ssh(
            state["host"],
            key,
            known_hosts,
            "cd ~/llm && sudo docker compose -f deploy/docker-compose.yml logs "
            "> deploy/results/container.log && "
            "sudo chown -R ec2-user:ec2-user deploy/results",
            timeout=120,
        )
        scp_from(
            state["host"],
            key,
            known_hosts,
            "~/llm/deploy/results",
            directory / "results",
        )
        success = True
        print(f"evidence collected in {directory / 'results'}", flush=True)
    finally:
        print("cleaning up", flush=True)
        if not cleanup(state):
            success = False
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(state["started_at"])).total_seconds()
        cost = round(elapsed / 3600.0 * HOURLY_USD, 6)
        state["elapsed_seconds"] = round(elapsed, 1)
        state["estimated_cost_usd"] = cost
        save_state(state_path, state)
        print(f"elapsed {elapsed:.0f}s, estimated cost ${cost:.6f}", flush=True)
        if not success:
            print(f"evidence retained at {directory}", file=sys.stderr)
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="create resources and run")
    args = parser.parse_args(argv)
    if not args.run:
        context = preflight()
        print(json.dumps(context, indent=2))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
