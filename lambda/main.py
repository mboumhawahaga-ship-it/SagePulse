import json
import os
from datetime import date, datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from discovery import run_discovery

logger = Logger(service="ml-cost-optimizer")


def get_sns_client():
    return boto3.client("sns", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_s3_client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_ce_client():
    return boto3.client("ce", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_cloudwatch_client():
    return boto3.client(
        "cloudwatch", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    )


def get_dynamodb_table():
    table_name = os.environ.get("AUDIT_TABLE")
    if not table_name:
        return None
    return boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    ).Table(table_name)


def write_audit_report(
    report_date, total_cost, total_savings, savings_pct, rec_count, carbon_kg
):
    """Enregistre le résumé du rapport dans la table DynamoDB d'audit."""
    table = get_dynamodb_table()
    if not table:
        return
    try:
        table.put_item(
            Item={
                "resource": f"report-{report_date}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "cost_report",
                "status": "generated",
                "detail": json.dumps(
                    {
                        "total_cost": total_cost,
                        "total_savings": total_savings,
                        "savings_pct": savings_pct,
                        "recommendation_count": rec_count,
                        "carbon_kg_month": carbon_kg,
                    }
                ),
            }
        )
    except ClientError as e:
        logger.warning(f"⚠️ Audit DynamoDB échoué : {e}")


def publish_metrics(
    total_savings, idle_notebooks_count, idle_endpoints_count, carbon_kg
):
    try:
        get_cloudwatch_client().put_metric_data(
            Namespace="MLCostOptimizer",
            MetricData=[
                {
                    "MetricName": "SavingsIdentifiedUSD",
                    "Value": float(total_savings),
                    "Unit": "None",
                },
                {
                    "MetricName": "IdleNotebooksCount",
                    "Value": float(idle_notebooks_count),
                    "Unit": "Count",
                },
                {
                    "MetricName": "IdleEndpointsCount",
                    "Value": float(idle_endpoints_count),
                    "Unit": "Count",
                },
                {
                    "MetricName": "CarbonFootprintKgMonth",
                    "Value": float(carbon_kg),
                    "Unit": "None",
                },
            ],
        )
        logger.info(
            f"✅ CloudWatch metrics published — savings=${total_savings:,.2f}, "
            f"idle_notebooks={idle_notebooks_count}, idle_endpoints={idle_endpoints_count}, "
            f"carbon={carbon_kg:.1f}kg"
        )
    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch metrics failed (non-blocking): {e}")


MOCK_DATA = {
    "total_cost": 850.00,
    "cost_by_resource": {
        "notebooks": 212.00,
        "training": 297.00,
        "endpoints": 170.00,
        "storage": 42.50,
        "other": 128.50,
    },
}


def build_cost_from_discovery(discovery):
    notebooks_cost = sum(
        n.get("monthly_cost_estimate", 0)
        for n in discovery.get("notebooks", [])
        if n.get("is_running")
    )
    endpoints_cost = sum(
        e.get("monthly_cost_estimate", 0)
        for e in discovery.get("endpoints", [])
        if e.get("is_running")
    )
    total = round(notebooks_cost + endpoints_cost, 2)
    return {
        "total_cost": total,
        "cost_by_resource": {
            "notebooks": round(notebooks_cost, 2),
            "training": 0.0,
            "endpoints": round(endpoints_cost, 2),
            "storage": 0.0,
            "other": 0.0,
        },
    }


def generate_recommendations(cost_by_resource, discovery=None):
    recs = []
    idle_notebooks = []
    idle_endpoints = []
    stuck_jobs = []

    if discovery:
        idle_notebooks = [
            n
            for n in discovery.get("notebooks", [])
            if n.get("is_idle") and n.get("is_running")
        ]
        idle_endpoints = [
            e
            for e in discovery.get("endpoints", [])
            if e.get("is_idle") and e.get("is_running")
        ]
        stuck_jobs = [
            j for j in discovery.get("training_jobs", []) if j.get("is_stuck")
        ]

    rules = [
        ("notebooks", 0.75, "Notebooks", 20, "Low", "High"),
        ("training", 0.70, "Training", 50, "Medium", "Critical"),
        ("endpoints", 0.30, "Endpoints", 50, "Medium", "High"),
        ("storage", 0.75, "Storage", 10, "Low", "Medium"),
    ]

    for key, pct, name, seuil, effort, priority in rules:
        cost = cost_by_resource.get(key, 0)
        if cost > seuil:
            if name == "Notebooks" and idle_notebooks:
                priority = "Critical"
                issue = f"Auto-stop {len(idle_notebooks)} idle notebook(s) (avg CPU < 5% over 24h)"
            elif name == "Endpoints" and idle_endpoints:
                priority = "Critical"
                issue = f"Flag {len(idle_endpoints)} idle endpoint(s) for review (0 invocations over 24h)"
            elif name == "Training" and stuck_jobs:
                priority = "Critical"
                issue = (
                    f"Stop {len(stuck_jobs)} stuck training job(s) (InProgress > 24h)"
                )
            else:
                issue = get_optimization_issue(name)

            recs.append(
                {
                    "type": name,
                    "cost": cost,
                    "savings": round(cost * pct, 2),
                    "savings_pct": round(pct * 100),
                    "effort": effort,
                    "priority": priority,
                    "issue": issue,
                    "idle_count": (
                        len(idle_notebooks)
                        if name == "Notebooks"
                        else len(idle_endpoints)
                        if name == "Endpoints"
                        else len(stuck_jobs)
                        if name == "Training"
                        else 0
                    ),
                }
            )

    priority_score = {"Critical": 1, "High": 2, "Medium": 3}
    recs.sort(key=lambda x: (priority_score.get(x["priority"], 99), -x["savings"]))
    return recs


def get_optimization_issue(resource_type):
    issues = {
        "Notebooks": "Enable auto-stop for idle notebooks (detect no activity per 24h)",
        "Training": "Use Spot instances for training jobs (70% cheaper)",
        "Endpoints": "Implement auto-scaling for endpoints with low off-hours traffic",
        "Storage": "Apply S3 Lifecycle policies to move old data to Glacier",
    }
    return issues.get(resource_type, "Review resource configuration")


def generate_markdown_report(
    total_cost,
    total_savings,
    savings_pct,
    recs,
    report_date,
    carbon_kg=0.0,
    stuck_jobs=None,
):
    stuck_jobs = stuck_jobs or []
    markdown = f"""# ML Cost Analysis Report

**Generated:** {report_date}

## Executive Summary

| Metric | Value |
|--------|-------|
| **Total Monthly Spend** | ${total_cost:,.2f} |
| **Identified Savings** | ${total_savings:,.2f} |
| **Savings Potential** | **{savings_pct}%** |
| **Recommendations** | {len(recs)} items |
| **Carbon Footprint** | {carbon_kg:.1f} kg CO₂/month |

---

## Optimization Recommendations

| Category | Issue | Monthly Savings | Effort | Priority |
|----------|-------|-----------------|--------|----------|
"""
    for rec in recs:
        markdown += (
            f"| {rec['type']} | {rec['issue']} | "
            f"${rec['savings']:,.2f} | {rec['effort']} | {rec['priority']} |\n"
        )

    markdown += "\n---\n\n## Next Steps (Sorted by ROI)\n\n"
    for idx, rec in enumerate(recs, 1):
        markdown += f"{idx}. **{rec['type']}** - {rec['issue']}\n"
        markdown += f"   - Potential Savings: ${rec['savings']:,.2f}/month\n"
        markdown += f"   - Effort: {rec['effort']} | Priority: {rec['priority']}\n\n"

    if stuck_jobs:
        markdown += "\n---\n\n## ⚠️ Stuck Training Jobs\n\n"
        markdown += "| Job Name | Hours Running |\n|----------|---------------|\n"
        for name, hours in stuck_jobs:
            markdown += f"| {name} | {hours}h |\n"

    if carbon_kg > 0:
        markdown += "\n---\n\n## 🌱 Carbon Footprint\n\n"
        markdown += f"Estimated CO₂ from running SageMaker notebooks: **{carbon_kg:.1f} kg/month**\n\n"
        markdown += (
            "> Stopping idle notebooks reduces both cost and carbon emissions.\n"
        )

    return markdown


def save_json_report(
    bucket_name,
    total_cost,
    total_savings,
    savings_pct,
    recs,
    report_date,
    carbon_kg=0.0,
):
    try:
        now = datetime.now(timezone.utc)
        report_data = {
            "metadata": {
                "report_date": report_date,
                "generated_at": now.isoformat(),
                "version": "2.0.0",
            },
            "summary": {
                "total_monthly_spend": float(total_cost),
                "identified_savings": float(total_savings),
                "savings_percentage": float(savings_pct),
                "recommendation_count": len(recs),
                "carbon_kg_month": float(carbon_kg),
            },
            "optimizations": [
                {
                    "category": rec["type"],
                    "issue": rec["issue"],
                    "monthly_savings": float(rec["savings"]),
                    "effort": rec["effort"],
                    "priority": rec["priority"],
                }
                for rec in recs
            ],
        }
        json_key = f"reports/report_{report_date}.json"
        get_s3_client().put_object(
            Bucket=bucket_name,
            Key=json_key,
            Body=json.dumps(report_data, indent=2),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        s3_url = f"s3://{bucket_name}/{json_key}"
        logger.info(f"✅ JSON report saved: {s3_url}")
        return s3_url
    except ClientError as e:
        logger.error(f"❌ Error saving JSON report to S3: {e}")
        raise


def save_markdown_report(bucket_name, markdown_content, report_date):
    try:
        md_key = f"reports/report_{report_date}.md"
        get_s3_client().put_object(
            Bucket=bucket_name,
            Key=md_key,
            Body=markdown_content.encode("utf-8"),
            ContentType="text/markdown",
            ServerSideEncryption="AES256",
        )
        s3_url = f"s3://{bucket_name}/{md_key}"
        logger.info(f"✅ Markdown report saved: {s3_url}")
        return s3_url
    except ClientError as e:
        logger.error(f"❌ Error saving Markdown report to S3: {e}")
        raise


def send_sns_notification(
    sns_topic_arn,
    total_savings,
    savings_pct,
    recommendation_count,
    markdown_s3_url,
    carbon_kg=0.0,
    stuck_jobs_count=0,
):
    try:
        message = (
            f"ML Cost Analysis — ${total_savings:,.2f} identified in savings "
            f"({savings_pct}%) across {recommendation_count} recommendations.\n\n"
        )
        if carbon_kg > 0:
            message += f"🌱 Carbon footprint: {carbon_kg:.1f} kg CO₂/month from running notebooks\n"
        if stuck_jobs_count > 0:
            message += (
                f"⚠️ {stuck_jobs_count} training job(s) stuck in InProgress > 24h\n"
            )
        message += f"\nFull report: {markdown_s3_url}"

        response = get_sns_client().publish(
            TopicArn=sns_topic_arn,
            Subject="ML Cost Analysis Report - Weekly Summary",
            Message=message,
        )
        logger.info(f"✅ SNS notification sent: {response['MessageId']}")
        return response
    except ClientError as e:
        logger.warning(f"⚠️ SNS notification failed (non-blocking): {e}")
        return None


def _zero_costs(cost_explorer_available=True):
    return {
        "total_cost": 0.0,
        "cost_by_resource": {
            "notebooks": 0.0,
            "training": 0.0,
            "endpoints": 0.0,
            "storage": 0.0,
            "other": 0.0,
        },
        "cost_explorer_available": cost_explorer_available,
    }


def get_real_costs():
    """
    Récupère les coûts SageMaker réels via Cost Explorer groupés par USAGE_TYPE.
    Remplace la répartition par pourcentages fixes.
    """
    try:
        today = date.today()
        start_date = today.replace(day=1)
        if start_date == today:
            import datetime as dt

            prev_month = today.replace(day=1) - dt.timedelta(days=1)
            start_date = prev_month.replace(day=1)

        start = start_date.strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        response = get_ce_client().get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Metrics=["UnblendedCost"],
        )

        results = response.get("ResultsByTime", [])
        if not results:
            logger.info("ℹ️ Cost Explorer : aucun résultat ce mois-ci.")
            return _zero_costs(cost_explorer_available=True)

        cost_by_resource = {
            "notebooks": 0.0,
            "training": 0.0,
            "endpoints": 0.0,
            "storage": 0.0,
            "other": 0.0,
        }

        for result in results:
            for group in result.get("Groups", []):
                usage_type = group["Keys"][0].lower()
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if "notebook" in usage_type:
                    cost_by_resource["notebooks"] += amount
                elif "training" in usage_type:
                    cost_by_resource["training"] += amount
                elif "endpoint" in usage_type or "inference" in usage_type:
                    cost_by_resource["endpoints"] += amount
                elif "storage" in usage_type or "s3" in usage_type:
                    cost_by_resource["storage"] += amount
                else:
                    cost_by_resource["other"] += amount

        cost_by_resource = {k: round(v, 2) for k, v in cost_by_resource.items()}
        total_cost = round(sum(cost_by_resource.values()), 2)

        if total_cost == 0:
            logger.info("ℹ️ Cost Explorer : coût SageMaker = $0 ce mois-ci.")
            return _zero_costs(cost_explorer_available=True)

        logger.info(
            f"✅ Cost Explorer : coût SageMaker = ${total_cost:,.2f} (répartition réelle par USAGE_TYPE)"
        )
        return {"total_cost": total_cost, "cost_by_resource": cost_by_resource}

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("DataUnavailableException", "RequestExpiredException"):
            logger.warning(f"⚠️ Cost Explorer non disponible ({error_code}).")
        else:
            logger.error(f"❌ Erreur Cost Explorer : {e}.")
        return _zero_costs(cost_explorer_available=False)


def handler(event, context):
    logger.info("🚀 ML Cost Analysis - Starting")

    try:
        report_bucket = os.environ.get("REPORT_BUCKET")
        sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
        mock_mode = os.environ.get("MOCK_MODE", "false").lower() == "true"
        cost_threshold = float(os.environ.get("COST_THRESHOLD_USD", "0"))

        if not report_bucket and not mock_mode:
            raise ValueError("REPORT_BUCKET environment variable not set")

        if mock_mode:
            logger.info("📊 Using mock data (MOCK_MODE=true)")
            data = MOCK_DATA
            discovery = None
        else:
            logger.info("📊 Fetching real costs from Cost Explorer...")
            data = get_real_costs()
            logger.info("🔍 Scanning real SageMaker resources...")
            discovery = run_discovery()
            data["discovery"] = discovery

            if data["total_cost"] == 0.0:
                real_costs = build_cost_from_discovery(discovery)
                if real_costs["total_cost"] > 0:
                    logger.info(
                        f"📊 Coûts calculés depuis les ressources détectées : ${real_costs['total_cost']:,.2f}"
                    )
                    data["total_cost"] = real_costs["total_cost"]
                    data["cost_by_resource"] = real_costs["cost_by_resource"]

        discovery = data.get("discovery")
        recs = generate_recommendations(data["cost_by_resource"], discovery)
        total_cost = data["total_cost"]
        total_savings = sum(r["savings"] for r in recs)
        savings_pct = (
            round(total_savings / total_cost * 100, 1) if total_cost > 0 else 0.0
        )
        report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        carbon_kg = (
            (discovery or {}).get("summary", {}).get("total_carbon_kg_month", 0.0)
        )
        stuck_jobs = [
            (j["name"], j["hours_running"])
            for j in (discovery or {}).get("training_jobs", [])
            if j.get("is_stuck")
        ]

        logger.info(f"💰 Total Cost   : ${total_cost:,.2f}")
        logger.info(f"💸 Total Savings: ${total_savings:,.2f}")
        logger.info(f"📈 Savings %    : {savings_pct}%")
        logger.info(f"🌱 Carbon       : {carbon_kg:.1f} kg CO₂/mois")

        below_threshold = cost_threshold > 0 and total_cost < cost_threshold
        if below_threshold:
            logger.info(
                f"ℹ️ Coût ${total_cost:.2f} sous le seuil ${cost_threshold:.2f} — pas de notification"
            )

        json_url = None
        markdown_url = None

        if not mock_mode:
            json_url = save_json_report(
                report_bucket,
                total_cost,
                total_savings,
                savings_pct,
                recs,
                report_date,
                carbon_kg,
            )
            markdown_content = generate_markdown_report(
                total_cost,
                total_savings,
                savings_pct,
                recs,
                report_date,
                carbon_kg=carbon_kg,
                stuck_jobs=stuck_jobs,
            )
            markdown_url = save_markdown_report(
                report_bucket, markdown_content, report_date
            )

            if sns_topic_arn and not below_threshold:
                send_sns_notification(
                    sns_topic_arn,
                    total_savings,
                    savings_pct,
                    len(recs),
                    markdown_url,
                    carbon_kg=carbon_kg,
                    stuck_jobs_count=len(stuck_jobs),
                )
            elif not sns_topic_arn:
                logger.warning("⚠️ SNS_TOPIC_ARN not configured, skipping notification")

            idle_notebooks = len(
                [
                    n
                    for n in (discovery or {}).get("notebooks", [])
                    if n.get("is_idle") and n.get("is_running")
                ]
            )
            idle_endpoints = len(
                [
                    e
                    for e in (discovery or {}).get("endpoints", [])
                    if e.get("is_idle") and e.get("is_running")
                ]
            )
            publish_metrics(total_savings, idle_notebooks, idle_endpoints, carbon_kg)
            write_audit_report(
                report_date,
                total_cost,
                total_savings,
                savings_pct,
                len(recs),
                carbon_kg,
            )
        else:
            logger.info("⏭️ Skipping S3 uploads and SNS in MOCK_MODE")

        idle_notebooks_list = [
            n["name"]
            for n in (discovery or {}).get("notebooks", [])
            if n.get("is_idle") and n.get("is_running")
        ]
        idle_endpoints_list = [
            e["name"]
            for e in (discovery or {}).get("endpoints", [])
            if e.get("is_idle") and e.get("is_running")
        ]

        response_data = {
            "success": True,
            "total_cost": total_cost,
            "potential_savings": round(total_savings, 2),
            "savings_pct": savings_pct,
            "recommendation_count": len(recs),
            "recommendations": recs,
            "carbon_kg_month": carbon_kg,
            "stuck_training_jobs": [j[0] for j in stuck_jobs],
            "idle_resources": {
                "notebooks": idle_notebooks_list,
                "endpoints": idle_endpoints_list,
            },
        }
        if json_url or markdown_url:
            response_data["reports"] = {
                "json_url": json_url,
                "markdown_url": markdown_url,
            }

        return {"statusCode": 200, "body": json.dumps(response_data, indent=2)}

    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"success": False, "error": str(e)}),
        }


if __name__ == "__main__":
    os.environ["REPORT_BUCKET"] = "test-bucket"
    result = handler({}, None)
    print("\n" + "=" * 60)
    print("LOCAL TEST OUTPUT")
    print("=" * 60)
    print(result["body"])
