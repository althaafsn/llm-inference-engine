#!/usr/bin/env python3
"""Deploy two gateways and two workers behind an internal ALB and test HA."""

from __future__ import annotations

import argparse
import concurrent.futures
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
sys.path.insert(0, str(HERE))

from aws_single_host import (  # noqa: E402
    aws,
    command,
    create_payload,
    identity,
    save_state,
    scp_from,
    scp_to,
    ssh,
    ssh_options,
    wait_ssh,
)

REGION = "us-east-1"
AMI = "ami-0820d0c589175bc01"
TAG_KEY = "temporary-llm-alb"
CAP_USD = 1.50
GATEWAY_TYPE = "c6i.large"
WORKER_TYPE = "c6i.xlarge"
LOAD_TYPE = "c6i.large"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
HOURLY_USD = 0.625


def public_ipv4() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com/", timeout=10) as response:
        return response.read(100).decode("ascii").strip()


def preflight() -> dict:
    for executable in ("aws", "ssh", "scp", "ssh-keygen", "tar"):
        if not shutil.which(executable):
            raise RuntimeError(f"missing local command: {executable}")
    account = identity()
    images = aws("ec2", "describe-images", "--image-ids", AMI)["Images"]
    if len(images) != 1 or images[0]["State"] != "available":
        raise RuntimeError("the pinned AMI is unavailable")
    subnets = aws("ec2", "describe-subnets", "--filters", "Name=default-for-az,Values=true")[
        "Subnets"
    ]
    by_az = {}
    for subnet in sorted(subnets, key=lambda item: item["SubnetId"]):
        if subnet.get("MapPublicIpOnLaunch") and subnet["State"] == "available":
            by_az.setdefault(subnet["AvailabilityZone"], subnet)
    if len(by_az) < 2:
        raise RuntimeError("need two public subnets in different AZs")
    chosen = [by_az[az] for az in sorted(by_az)[:2]]
    vpc = chosen[0]["VpcId"]
    try:
        load_balancers = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
        target_groups = aws("elbv2", "describe-target-groups")["TargetGroups"]
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("ELB describe permission is missing") from exc
    if load_balancers or target_groups:
        raise RuntimeError("load balancers or target groups already exist")
    active = aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag-key,Values={TAG_KEY}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    )
    if active.get("Reservations"):
        raise RuntimeError("active tagged instances exist")
    volumes = aws("ec2", "describe-volumes", "--filters", f"Name=tag-key,Values={TAG_KEY}")
    if volumes.get("Volumes"):
        raise RuntimeError("tagged volumes exist")
    address = public_ipv4()
    print(f"Account {account}, region {REGION}")
    print(f"Subnet A {chosen[0]['SubnetId']} ({chosen[0]['AvailabilityZone']})")
    print(f"Subnet B {chosen[1]['SubnetId']} ({chosen[1]['AvailabilityZone']})")
    print(f"Gateways {GATEWAY_TYPE}, workers {WORKER_TYPE}, load {LOAD_TYPE}")
    print(f"Budget cap ${CAP_USD:.2f}; estimated {HOURLY_USD:.3f}/hour")
    return {
        "account": account,
        "region": REGION,
        "vpc": vpc,
        "subnet": chosen[0]["SubnetId"],
        "subnets": [item["SubnetId"] for item in chosen],
        "public_ip": address,
    }


def user_data() -> str:
    return """#!/bin/bash
set -eu
if ! shutdown -h +60; then poweroff; exit 1; fi
"""


def create_group(name: str, description: str, run_id: str, vpc: str) -> str:
    result = aws(
        "ec2",
        "create-security-group",
        "--group-name",
        f"{run_id}-{name}",
        "--description",
        description,
        "--vpc-id",
        vpc,
        "--tag-specifications",
        json.dumps(
            [{"ResourceType": "security-group", "Tags": [{"Key": TAG_KEY, "Value": run_id}]}]
        ),
    )
    return result["GroupId"]


def authorize_ip(group: str, port: int, cidr: str) -> None:
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group,
        "--protocol",
        "tcp",
        "--port",
        str(port),
        "--cidr",
        cidr,
    )


def authorize_group(group: str, port: int, source: str) -> None:
    aws(
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group,
        "--ip-permissions",
        json.dumps(
            [
                {
                    "IpProtocol": "tcp",
                    "FromPort": port,
                    "ToPort": port,
                    "UserIdGroupPairs": [{"GroupId": source}],
                }
            ]
        ),
    )


def create_groups(state: dict, state_path: Path) -> None:
    groups = {}
    for name, description in (
        ("ssh", "SSH from owner"),
        ("alb", "Internal ALB HTTP"),
        ("gateway", "Gateway app port"),
        ("worker", "Worker app port"),
        ("load", "Load generator"),
    ):
        groups[name] = create_group(name, description, state["run_id"], state["vpc"])
        state["groups"] = groups
        save_state(state_path, state)
    authorize_ip(groups["ssh"], 22, state["public_ip"] + "/32")
    authorize_group(groups["alb"], 8100, groups["load"])
    authorize_group(groups["gateway"], 8100, groups["alb"])
    authorize_group(groups["worker"], 8101, groups["gateway"])


def launch(
    state: dict,
    state_path: Path,
    role: str,
    instance_type: str,
    groups: list[str],
    key_name: str,
    userdata: Path,
) -> str:
    response = aws(
        "ec2",
        "run-instances",
        "--image-id",
        AMI,
        "--instance-type",
        instance_type,
        "--subnet-id",
        state["subnet"],
        "--security-group-ids",
        *groups,
        "--key-name",
        key_name,
        "--associate-public-ip-address",
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--metadata-options",
        "HttpTokens=required,HttpEndpoint=enabled",
        "--block-device-mappings",
        json.dumps(
            [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": 30,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                        "Encrypted": True,
                    },
                }
            ]
        ),
        "--tag-specifications",
        json.dumps(
            [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": TAG_KEY, "Value": state["run_id"]},
                        {"Key": "Name", "Value": f"{state['run_id']}-{role}"},
                    ],
                }
            ]
        ),
        "--user-data",
        f"file://{userdata}",
        timeout=120,
    )
    instance = response["Instances"][0]["InstanceId"]
    state["instances"][role] = {"id": instance, "type": instance_type}
    save_state(state_path, state)
    aws("ec2", "wait", "instance-running", "--instance-ids", instance, timeout=300)
    description = aws("ec2", "describe-instances", "--instance-ids", instance)["Reservations"][0][
        "Instances"
    ][0]
    state["instances"][role]["public_ip"] = description["PublicIpAddress"]
    state["instances"][role]["private_ip"] = description["PrivateIpAddress"]
    save_state(state_path, state)
    print(f"launched {role} {instance}", flush=True)
    return instance


def setup_host(host: str, key: Path, known_hosts: Path, role: str) -> None:
    base = (
        "sudo dnf install -y python3.12 python3.12-pip tar gzip && "
        "python3.12 -m venv ~/venv && "
        "~/venv/bin/pip install --upgrade pip"
    )
    if role.startswith("worker"):
        model = (
            '~/venv/bin/python -c "from transformers import AutoModelForCausalLM, '
            f"AutoTokenizer; m='{MODEL}'; AutoTokenizer.from_pretrained(m); "
            'AutoModelForCausalLM.from_pretrained(m)"'
        )
        remote = (
            base
            + " && ~/venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch"
            + " && ~/venv/bin/pip install transformers aiohttp numpy"
            + " && "
            + model
        )
    else:
        remote = base + " && ~/venv/bin/pip install aiohttp"
    ssh(host, key, known_hosts, remote, timeout=1800)
    print(f"{role} ready", flush=True)


def wait_http(host: str, key: Path, known_hosts: Path, url: str, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = command(
            [
                "ssh",
                *ssh_options(key, known_hosts),
                f"ec2-user@{host}",
                f"curl -fsS --max-time 5 {url}",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(5)
    raise RuntimeError(f"URL did not become ready: {url}: {last}")


def start_background(host: str, key: Path, known_hosts: Path, remote: str) -> None:
    subprocess.Popen(
        ["ssh", "-f", *ssh_options(key, known_hosts), f"ec2-user@{host}", remote],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(2)


def start_worker(host: str, key: Path, known_hosts: Path, role: str) -> None:
    start_background(
        host,
        key,
        known_hosts,
        "cd ~/llm && setsid ~/venv/bin/python -m llm_engine.worker "
        f"--host 0.0.0.0 --port 8101 --id {role} --model {MODEL} --threads 4 "
        "> ~/worker.log 2>&1 < /dev/null & echo $! > ~/worker.pid",
    )


def start_gateway(state: dict, host: str, key: Path, known_hosts: Path, role: str) -> None:
    workers = " ".join(
        f"http://{state['instances'][worker]['private_ip']}:8101"
        for worker in ("worker-1", "worker-2")
    )
    start_background(
        host,
        key,
        known_hosts,
        "cd ~/llm && setsid ~/venv/bin/python -m llm_engine.gateway "
        f"--host 0.0.0.0 --port 8100 --id {role} --workers {workers} "
        "--admission-max 2 > ~/gateway.log 2>&1 < /dev/null & echo $! > ~/gateway.pid",
    )


def create_alb(state: dict, state_path: Path) -> None:
    name = f"{state['run_id']}-gw"
    group = aws(
        "elbv2",
        "create-target-group",
        "--name",
        name,
        "--protocol",
        "HTTP",
        "--port",
        "8100",
        "--vpc-id",
        state["vpc"],
        "--health-check-path",
        "/health",
        "--health-check-interval-seconds",
        "5",
        "--health-check-timeout-seconds",
        "3",
        "--healthy-threshold-count",
        "2",
        "--unhealthy-threshold-count",
        "2",
        "--matcher",
        "HttpCode=200",
        "--target-type",
        "instance",
        "--tags",
        f"Key={TAG_KEY},Value={state['run_id']}",
    )
    state["alb"] = {"target_group_arn": group["TargetGroups"][0]["TargetGroupArn"]}
    save_state(state_path, state)
    load_balancer = aws(
        "elbv2",
        "create-load-balancer",
        "--name",
        f"{state['run_id']}-alb",
        "--subnets",
        *state["subnets"],
        "--security-groups",
        state["groups"]["alb"],
        "--scheme",
        "internal",
        "--type",
        "application",
        "--tags",
        f"Key={TAG_KEY},Value={state['run_id']}",
    )["LoadBalancers"][0]
    state["alb"]["arn"] = load_balancer["LoadBalancerArn"]
    state["alb"]["dns"] = load_balancer["DNSName"]
    save_state(state_path, state)
    listener = aws(
        "elbv2",
        "create-listener",
        "--load-balancer-arn",
        state["alb"]["arn"],
        "--protocol",
        "HTTP",
        "--port",
        "8100",
        "--default-actions",
        f"Type=forward,TargetGroupArn={state['alb']['target_group_arn']}",
    )["Listeners"][0]
    state["alb"]["listener_arn"] = listener["ListenerArn"]
    save_state(state_path, state)
    register_targets(state, ["gateway-1", "gateway-2"])
    wait_alb_active(state)
    wait_targets_healthy(state, {"gateway-1", "gateway-2"})


def register_targets(state: dict, roles: list[str]) -> None:
    targets = [
        f"Id={state['instances'][role]['id']},Port=8100"
        for role in roles
        if state["instances"].get(role, {}).get("id")
    ]
    if targets:
        aws(
            "elbv2",
            "register-targets",
            "--target-group-arn",
            state["alb"]["target_group_arn"],
            "--targets",
            *targets,
        )


def target_states(state: dict) -> dict[str, str]:
    response = aws(
        "elbv2",
        "describe-target-health",
        "--target-group-arn",
        state["alb"]["target_group_arn"],
    )
    return {
        item["Target"]["Id"]: item["TargetHealth"]["State"]
        for item in response["TargetHealthDescriptions"]
    }


def wait_alb_active(state: dict) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = aws(
            "elbv2", "describe-load-balancers", "--load-balancer-arns", state["alb"]["arn"]
        )["LoadBalancers"][0]
        if response["State"]["Code"] == "active":
            return
        time.sleep(3)
    raise RuntimeError("ALB did not become active")


def wait_targets_healthy(state: dict, roles: set[str], timeout: float = 240.0) -> None:
    ids = {state["instances"][role]["id"] for role in roles}
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = target_states(state)
        if ids and all(last.get(instance) == "healthy" for instance in ids):
            return
        time.sleep(5)
    raise RuntimeError(f"targets did not become healthy: {last}")


def run_bench(
    load_host: str, key: Path, known_hosts: Path, target: str, label: str, duration: float
) -> None:
    ssh(
        load_host,
        key,
        known_hosts,
        "cd ~/llm && mkdir -p results && ~/venv/bin/python -m llm_engine.bench "
        f"--target {target} --rate 2 --duration {duration} --max-tokens 8 "
        f"--prompt-prefix {label} --retry-attempts 3 --retry-backoff-ms 100 "
        "--retry-max-backoff-ms 1000 --retry-jitter 0.2 "
        f"--output ~/llm/results/{label}",
        timeout=600,
    )


def run_bench_background(
    load_host: str, key: Path, known_hosts: Path, target: str, label: str, duration: float
) -> None:
    start_background(
        load_host,
        key,
        known_hosts,
        "cd ~/llm && mkdir -p results && setsid ~/venv/bin/python -m llm_engine.bench "
        f"--target {target} --rate 2 --duration {duration} --max-tokens 8 "
        f"--prompt-prefix {label} --retry-attempts 3 --retry-backoff-ms 100 "
        "--retry-max-backoff-ms 1000 --retry-jitter 0.2 "
        f"--output ~/llm/results/{label} "
        "> ~/failover.log 2>&1 < /dev/null & echo $! > ~/bench.pid",
    )


def wait_bench(load_host: str, key: Path, known_hosts: Path) -> None:
    ssh(
        load_host,
        key,
        known_hosts,
        "while kill -0 $(cat ~/bench.pid) 2>/dev/null; do sleep 1; done",
        timeout=300,
    )


def delete_alb(state: dict) -> bool:
    alb = state.get("alb", {})
    if not alb:
        return True
    ok = True
    load_balancer_deleted = False
    for key, command_name, arg in (
        ("listener_arn", "delete-listener", "--listener-arn"),
        ("target_group_arn", "delete-target-group", "--target-group-arn"),
        ("arn", "delete-load-balancer", "--load-balancer-arn"),
    ):
        value = alb.get(key)
        if not value:
            continue
        deleted = False
        for attempt in range(3):
            try:
                aws("elbv2", command_name, arg, value)
                deleted = True
                break
            except subprocess.CalledProcessError as exc:
                if "NotFound" in (exc.stderr or ""):
                    deleted = True
                    break
                if attempt == 2:
                    print(f"ERROR deleting ALB {key}: {exc.stderr}", file=sys.stderr)
                    ok = False
                else:
                    time.sleep(3)
        if key == "arn" and deleted:
            load_balancer_deleted = True
    if not alb.get("arn") or not load_balancer_deleted:
        return ok
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            aws("elbv2", "describe-load-balancers", "--load-balancer-arns", alb["arn"])
        except subprocess.CalledProcessError as exc:
            if "LoadBalancerNotFound" in (exc.stderr or ""):
                return ok
        time.sleep(3)
    return False


def cleanup(state: dict) -> bool:
    try:
        delete_alb(state)
    except Exception as exc:
        print(f"ERROR deleting ALB: {exc}", file=sys.stderr)
    instances = [item["id"] for item in state.get("instances", {}).values() if item.get("id")]
    if instances:
        try:
            aws("ec2", "terminate-instances", "--instance-ids", *instances)
            aws("ec2", "wait", "instance-terminated", "--instance-ids", *instances, timeout=300)
        except Exception as exc:
            print(f"ERROR terminating instances: {exc}", file=sys.stderr)
    groups = [
        state.get("groups", {}).get(name) for name in ("worker", "gateway", "alb", "load", "ssh")
    ]
    for group in [group for group in groups if group]:
        for _ in range(12):
            try:
                aws("ec2", "delete-security-group", "--group-id", group)
                break
            except subprocess.CalledProcessError as exc:
                if "InvalidGroup.NotFound" in (exc.stderr or ""):
                    break
                time.sleep(5)
    key_name = state.get("key_name")
    if key_name:
        try:
            aws("ec2", "delete-key-pair", "--key-name", key_name)
        except Exception:
            pass
    return True


def verify_cleanup() -> bool:
    active = aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag-key,Values={TAG_KEY}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    )
    volumes = aws("ec2", "describe-volumes", "--filters", f"Name=tag-key,Values={TAG_KEY}")
    groups = aws("ec2", "describe-security-groups", "--filters", f"Name=tag-key,Values={TAG_KEY}")
    load_balancers = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    target_groups = aws("elbv2", "describe-target-groups")["TargetGroups"]
    ok = (
        not active.get("Reservations")
        and not volumes.get("Volumes")
        and not groups.get("SecurityGroups")
        and not load_balancers
        and not target_groups
    )
    if not ok:
        print("ERROR: cleanup is incomplete", file=sys.stderr)
    return ok


def run(args: argparse.Namespace) -> int:
    context = preflight()
    run_id = "llmha-" + secrets.token_hex(6)
    directory = HERE / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    state_path = directory / "state.json"
    state = {
        "run_id": run_id,
        "account": context["account"],
        "region": REGION,
        "vpc": context["vpc"],
        "subnet": context["subnet"],
        "subnets": context["subnets"],
        "public_ip": context["public_ip"],
        "started_at": datetime.now(UTC).isoformat(),
        "directory": str(directory),
        "instances": {},
        "groups": {},
        "alb": {},
        "key_name": None,
    }
    save_state(state_path, state)
    success = False
    try:
        key_name = f"{run_id}-key"
        key = directory / "key.pem"
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
        known_hosts = directory / "known_hosts"
        known_hosts.touch()
        state["key_name"] = key_name
        save_state(state_path, state)
        create_groups(state, state_path)
        userdata = directory / "userdata.sh"
        userdata.write_text(user_data(), encoding="utf-8")

        roles = [
            ("gateway-1", GATEWAY_TYPE, ["ssh", "gateway"]),
            ("gateway-2", GATEWAY_TYPE, ["ssh", "gateway"]),
            ("worker-1", WORKER_TYPE, ["ssh", "worker"]),
            ("worker-2", WORKER_TYPE, ["ssh", "worker"]),
            ("load", LOAD_TYPE, ["ssh", "load"]),
        ]
        for role, instance_type, group_names in roles:
            launch(
                state,
                state_path,
                role,
                instance_type,
                [state["groups"][name] for name in group_names],
                key_name,
                userdata,
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(
                pool.map(
                    lambda role: wait_ssh(state["instances"][role]["public_ip"], key, known_hosts),
                    [role for role, _, _ in roles],
                )
            )
        print("SSH ready", flush=True)

        payload = create_payload(directory)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(
                pool.map(
                    lambda role: scp_to(
                        state["instances"][role]["public_ip"],
                        key,
                        known_hosts,
                        payload,
                        "~/project.tar.gz",
                    ),
                    [role for role, _, _ in roles],
                )
            )
        for role, _, _ in roles:
            ssh(
                state["instances"][role]["public_ip"],
                key,
                known_hosts,
                "rm -rf ~/llm && mkdir -p ~/llm && tar -xzf ~/project.tar.gz -C ~/llm",
                timeout=300,
            )
        print("payload copied", flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(
                    setup_host, state["instances"][role]["public_ip"], key, known_hosts, role
                )
                for role, _, _ in roles
            ]
            for future in futures:
                future.result()
        print("hosts ready", flush=True)

        for role in ("worker-1", "worker-2"):
            start_worker(state["instances"][role]["public_ip"], key, known_hosts, role)
        for role in ("worker-1", "worker-2"):
            wait_http(
                state["instances"][role]["public_ip"],
                key,
                known_hosts,
                "http://127.0.0.1:8101/health",
                timeout=300,
            )
        print("workers healthy", flush=True)

        for role in ("gateway-1", "gateway-2"):
            start_gateway(state, state["instances"][role]["public_ip"], key, known_hosts, role)
        for role in ("gateway-1", "gateway-2"):
            wait_http(
                state["instances"][role]["public_ip"],
                key,
                known_hosts,
                "http://127.0.0.1:8100/health",
                timeout=120,
            )
        print("gateways healthy", flush=True)

        create_alb(state, state_path)
        print(f"ALB ready: {state['alb']['dns']}", flush=True)
        target = f"http://{state['alb']['dns']}:8100"
        load_host = state["instances"]["load"]["public_ip"]

        run_bench(load_host, key, known_hosts, target, "normal", 20)
        print("normal complete", flush=True)

        run_bench_background(load_host, key, known_hosts, target, "failover", 20)
        time.sleep(3)
        gateway_id = state["instances"]["gateway-1"]["id"]
        killed_at = time.monotonic()
        aws("ec2", "terminate-instances", "--instance-ids", gateway_id)
        states = {}
        unhealthy_after = None
        while time.monotonic() - killed_at < 60:
            states = target_states(state)
            if states.get(gateway_id) == "unhealthy":
                unhealthy_after = round(time.monotonic() - killed_at, 3)
                break
            time.sleep(2)
        (directory / "failover-target-health.json").write_text(
            json.dumps({"states": states, "unhealthy_after_seconds": unhealthy_after}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        wait_bench(load_host, key, known_hosts)
        print(f"failover complete; unhealthy after {unhealthy_after}s", flush=True)

        launch(
            state,
            state_path,
            "gateway-1-recovery",
            GATEWAY_TYPE,
            [state["groups"]["ssh"], state["groups"]["gateway"]],
            key_name,
            userdata,
        )
        wait_ssh(state["instances"]["gateway-1-recovery"]["public_ip"], key, known_hosts)
        scp_to(
            state["instances"]["gateway-1-recovery"]["public_ip"],
            key,
            known_hosts,
            payload,
            "~/project.tar.gz",
        )
        ssh(
            state["instances"]["gateway-1-recovery"]["public_ip"],
            key,
            known_hosts,
            "rm -rf ~/llm && mkdir -p ~/llm && tar -xzf ~/project.tar.gz -C ~/llm",
            timeout=300,
        )
        setup_host(
            state["instances"]["gateway-1-recovery"]["public_ip"],
            key,
            known_hosts,
            "gateway-1-recovery",
        )
        start_gateway(
            state,
            state["instances"]["gateway-1-recovery"]["public_ip"],
            key,
            known_hosts,
            "gateway-1-recovery",
        )
        wait_http(
            state["instances"]["gateway-1-recovery"]["public_ip"],
            key,
            known_hosts,
            "http://127.0.0.1:8100/health",
            timeout=120,
        )
        register_targets(state, ["gateway-1-recovery"])
        wait_targets_healthy(state, {"gateway-1-recovery"})
        run_bench(load_host, key, known_hosts, target, "recovery", 20)
        print("recovery complete", flush=True)

        scp_from(load_host, key, known_hosts, "~/llm/results", directory / "results")
        success = True
    finally:
        print("cleaning up", flush=True)
        cleanup(state)
        if not verify_cleanup():
            success = False
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(state["started_at"])).total_seconds()
        state["elapsed_seconds"] = round(elapsed, 1)
        state["estimated_cost_usd"] = round(elapsed / 3600.0 * HOURLY_USD, 6)
        save_state(state_path, state)
        print(f"elapsed {elapsed:.0f}s, estimated ${state['estimated_cost_usd']:.6f}", flush=True)
        if not success:
            print(f"evidence retained at {directory}", file=sys.stderr)
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps(preflight(), indent=2))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
