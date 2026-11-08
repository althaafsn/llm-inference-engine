# Deployment

## Local Docker Compose

The stack has two workers, two gateways, and one health-checked proxy. Only the proxy port `8180` is published.

```sh
docker compose -f deploy/docker-compose.yml up -d --build
curl -s http://127.0.0.1:8180/health
docker compose -f deploy/docker-compose.yml run --rm bench \
  python -m llm_engine.bench --target http://proxy:8180 \
  --rate 2 --duration 10 --max-tokens 8 --output /results/manual
docker compose -f deploy/docker-compose.yml down -v
```

The `hf-cache` volume keeps the model between runs. The first start downloads Qwen2.5-0.5B-Instruct; later starts load from the volume.

## AWS single host

`aws_single_host.py` provisions one `c6i.2xlarge` in `us-east-1`, installs Docker, copies the project, builds the stack, runs normal/failover/recovery, collects evidence, and terminates the host.

```sh
python3 deploy/aws_single_host.py          # preflight only
python3 deploy/aws_single_host.py --run    # deploy, test, clean up
```

What it creates:

- One tagged EC2 instance with a 40 GB gp3 root volume.
- One security group that allows SSH only from the owner's public IPv4 `/32`.
- One temporary key pair.
- No public application port, load balancer, or DNS record.

The script always tries to terminate the instance and delete the group and key pair. Evidence is written to `deploy/runs/<run-id>/` and is ignored by Git because it contains the temporary private key and instance state.

### Cost

The instance is about $0.34/hour. The default cap is $1.50. The measured successful run took 256 seconds and cost **$0.024164**. Earlier bootstrap attempts cost **$0.232165**. Total: **$0.256329**.

### Bootstrap notes

The first attempts failed for environment reasons, not application reasons:

- `get.docker.com` does not support Amazon Linux (`Unsupported distribution 'amzn'`). Use the AL2023 `docker` package.
- Installing `curl` conflicts with the preinstalled `curl-minimal`.
- The Compose plugin requires buildx, so the controller installs the buildx plugin binary.
- `docker compose run bench` must not depend on the gateways, because Compose would restart a gateway that the failover test intentionally stopped.

The controller now includes these fixes and verifies `docker compose version` before building.

## Limits

- One host. The workers, gateways, proxy, and benchmark share the same 8 vCPUs and 16 GB of RAM.
- No ALB, multi-AZ, or separate load-generator host.
- No TLS, authentication, or public endpoint.
- The host is temporary and all state is discarded after the run.
