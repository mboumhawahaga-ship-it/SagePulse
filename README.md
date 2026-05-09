[![CI/CD](https://github.com/mboumhawahaga-ship-it/SagePulse/actions/workflows/ci.yml/badge.svg)](https://github.com/mboumhawahaga-ship-it/SagePulse/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-88%25-brightgreen)](https://github.com/mboumhawahaga-ship-it/SagePulse)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org)
[![Terraform](https://img.shields.io/badge/terraform-%3E%3D1.0-purple)](https://www.terraform.io)

# SagePulse

Stop paying for idle resources. The intelligent FinOps scanner for AWS SageMaker.

---

## The Problem

SageMaker is a money pit when resources stay active without being used. A forgotten endpoint or a KernelGateway running all weekend can cost hundreds of dollars before you even receive your AWS billing alert.

---

## What SagePulse Does

Every 4 hours, SagePulse scans your infrastructure and identifies unused resources using cross-referenced analysis — CloudWatch metrics and real billing data combined.

| Resource | Detection method | Action |
|---|---|---|
| Notebook Instances | CPU < 5% for 4h | Auto-stop (state preserved) |
| Inference Endpoints | 0 invocations over 24h | Alert — human approval required |
| Studio Apps (KernelGateway) | Active without user | Alert |
| Training Jobs | Stuck or abnormally long | Alert |

Nothing critical is deleted automatically. You decide what to act on.

---

## Architecture

100% serverless — costs nothing when idle.

```
EventBridge (every 4 hours)
        ↓
Step Functions Workflow
        ↓
Lambda Scanner
  Scans all SageMaker resources
  Detects idle via CloudWatch (CPU, Invocations)
  Calculates real costs via Pricing API + Cost Explorer
        ↓
DynamoDB
  Stores each idle resource detected
  Prevents duplicate alerts (1 alert per resource per 4h window)
        ↓
SNS Notification
  Sends actionable alert to MLOps team
        ↓
Human Approval (waitForTaskToken)
  Workflow pauses — resumes only after explicit approval
        ↓
Lambda Action
  Stops notebooks
  Notifies about idle endpoints (no auto-deletion)
        ↓
S3
  Archives JSON + Markdown reports for FinOps teams
```

---

## Business Impact

- **Visibility** — precise cost breakdown per resource, updated every 4 hours
- **Savings** — up to 40% reduction on SageMaker Dev/Test environments
- **No alert fatigue** — DynamoDB deduplication ensures one alert per resource, not one per scan

---

## Security

- **Safe-stop** — notebooks are stopped (state preserved on EFS), never deleted
- **Human approval** — critical resources (endpoints) alert but never act without confirmation
- **Least-privilege IAM** — separate roles for Lambda and Step Functions, scoped per action
- **No long-term AWS keys** — GitHub Actions authenticates via OIDC

---

## Setup

```bash
git clone https://github.com/mboumhawahaga-ship-it/SagePulse
cd SagePulse
bash setup.sh
```

The script asks for your email and deploys everything. Requires AWS CLI configured and Terraform installed.

**Infrastructure cost: under $2/month.**

---

## Local Development

```bash
pip install -r requirements.txt -r requirements-dev.txt -r lambda/requirements.txt
pytest tests/ --cov=lambda --cov-fail-under=80 -v
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12 · AWS Lambda |
| Orchestration | AWS Step Functions (JSONata, waitForTaskToken) |
| Storage | DynamoDB (alert deduplication) · S3 (reports) |
| Infrastructure | Terraform · S3 remote state · DynamoDB locking |
| CI/CD | GitHub Actions · OIDC auth |
| Observability | AWS Lambda Powertools · CloudWatch custom metrics |
| Testing | pytest · unittest.mock · moto · 88% coverage |
| AI Integration | AWS Labs SageMaker MCP server |

---

## Why I Built This

I built SagePulse during my transition into cloud engineering. SageMaker costs are a real pain point for ML teams — resources stay running, nobody notices, and the bill arrives at the end of the month.

The goal was to build something end-to-end: real AWS infrastructure, real cost data, real notifications, with a human-in-the-loop approval pattern so nothing is ever deleted by accident.

Every technical decision came from hitting a real problem: the `waitForTaskToken` pattern to avoid accidental deletions, DynamoDB deduplication to prevent alert fatigue, OIDC authentication to avoid storing AWS keys in GitHub.

---

Full architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

MIT — see [LICENSE](LICENSE)
