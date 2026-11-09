# AWS cost ledger

Budget: **$10 shared with the C++ ONNX project.** Costs are estimates from recorded runtime and the hourly rates. They are not a billing statement. Tax, credits, discounts, and data-transfer differences are excluded.

| Run | Purpose | Seconds | Estimated |
| --- | --- | ---: | ---: |
| Single host, successful | Docker Compose normal/failover/recovery | 256 | $0.024164 |
| Single host, failed attempts | Docker bootstrap and one invalid failover | 2,458 | $0.232165 |
| ALB, no retry, valid | 2 gateways, 2 workers, 15.8 s health state | 513 | $0.089016 |
| ALB, retry, valid | 3 retries, 38/40 failover | 530 | $0.092016 |
| ALB, earlier attempts | SSH hangs, invalid failover, SCP failure | 2,312 | $0.401329 |
| 8-worker capacity | 2 gateways, 8 workers, rates 4/8/12 | 762 | $0.348115 |
| **LLM total** | | | **$1.186805** |
| C++ ONNX project | Earlier campaign | | $3.565888 |
| **Combined** | | | **$4.752693** |

Remaining shared budget: about **$5.25**.

## Method

- Single-host: `c6i.2xlarge`, $0.34/hour, plus EBS and IPv4.
- Multi-host: 2 gateways `c6i.large`, N workers `c6i.xlarge`, 1 load `c6i.large`, internal ALB. The 2-worker rate is $0.625/hour; the 8-worker rate is $1.645/hour.
- Runtime is from the controller start to cleanup. It includes provisioning, dependency install, model download, benchmarks, and teardown.
- Failed attempts are included because they consumed real instance time.

## Cleanup

Read-only checks after every run confirmed:

- no active tagged instances,
- no tagged volumes,
- no tagged security groups,
- no load balancers,
- no target groups.

The controllers terminate instances, delete security groups and key pairs, and wait for termination before reporting success.
