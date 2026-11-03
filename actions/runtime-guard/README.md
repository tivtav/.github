# Runtime guard

Keeps every tivtav repository on the org runtime standard
([decision 0018](https://github.com/tivtav/tivtav-documentation/blob/main/decisions/0018-runtime-standard.md)):
the newest Python and Node.js that AWS Lambda supports (GA, not preview), on arm64.
The versions live in one file, [`runtime-standards.json`](../../runtime-standards.json).

## Use it

Add a step **after `cdk synth`** in the job that synthesizes your stacks:

```yaml
      - run: npx cdk synth > /dev/null
      - uses: tivtav/.github/actions/runtime-guard@main
```

A repository without CDK (a front-end, a library) can run it on its own; it then checks
only the toolchain files. Inputs: `cdk-out` (directories, one per line; default every
`cdk.out` in the checkout), `exempt` (regexes for Lambdas a third-party construct owns,
one per line), `toolchain` (default `true`), `fail-on-warning` (default `false`).

## What it checks

| Where | Check |
|---|---|
| Lambda functions | `Architectures` includes `arm64`; `pythonX.Y` / `nodejsNN.x` runtime vs the standard |
| Lambda layers | `CompatibleArchitectures` includes `arm64`; compatible runtimes |
| EC2 instances, launch templates | Graviton instance type (`t4g`, `m7g`, …); no x86_64 AMI parameter |
| Fargate task definitions | `RuntimePlatform.CpuArchitecture: ARM64` |
| `.python-version`, `requires-python`, ruff `target-version` | Python version |
| `.nvmrc`, `.node-version`, `engines.node` | Node version (`lts/*` is fine) |
| Workflows (`python-version`, `node-version`) | CI versions |
| Dockerfiles | `python`/`node` base image versions; no `--platform=linux/amd64` |

Below `minimum` is an **error** (fails the job). Below `latest` is a **warning**. A
Python newer than `latest` is a warning (a preview runtime, or CI not running what the
Lambda runs); a newer Node in CI is fine.

Lambdas that aws-cdk-lib creates for itself (log retention, custom-resource providers,
S3 auto-delete, …) are exempt: CDK sets their runtime, and they only run at deploy time.

## Moving to a new runtime

1. AWS makes a runtime generally available (check the
   [Lambda runtimes page](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html);
   preview doesn't count).
2. Bump `latest` in `runtime-standards.json`. Every repository behind it gets a warning,
   which is the to-do list. tivtav-block-tools moves first (decision 0018).
3. When the repositories have moved, raise `minimum`. Anything still behind now fails CI.

## Tests

```sh
python3 -I -m unittest discover -s actions/runtime-guard/tests
```
