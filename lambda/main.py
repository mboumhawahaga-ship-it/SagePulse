import json
import os
from datetime import date, datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from discovery import run_discovery

logger = Logger(service="ml-cost-optimizer")


def get_sns_client():
    """Lazy-load SNS client with region from environment"""
    return boto3.client("sns", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_s3_client():
    """Lazy-load S3 client with region from environment"""
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_ce_client():
    """Lazy-load Cost Explorer client with region from environment"""
    return boto3.client("ce", region_name=os.environ.get("AWS_REGION", "eu-west-1"))


def get_cloudwatch_client():
    """Lazy-load CloudWatch client for custom metrics"""
    return boto3.client(
        "cloudwatch", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    )


def publish_metrics(
    total_savings, idle_notebooks_count, idle_endpoints_count, compliance_score
):
    """
    Publie des métriques custom CloudWatch pour monitoring et alertes.

    Métriques publiées dans le namespace MLCostOptimizer :
      - SavingsIdentifiedUSD     : économies potentielles en dollars
      - IdleNotebooksCount       : notebooks allumés sans activité (CPU < 5%)
      - IdleEndpointsCount       : endpoints sans invocations sur 24h
      - ComplianceScore          : % de ressources conformes RGPD (0-100)

    Ces métriques permettent de créer des alarmes CloudWatch :
      ex: alerter si IdleNotebooksCount > 0 ou ComplianceScore < 80
    """
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
                    "MetricName": "ComplianceScore",
                    "Value": float(compliance_score),
                    "Unit": "Percent",
                },
            ],
        )
        logger.info(
            f"✅ CloudWatch metrics published — "
            f"savings=${total_savings:,.2f}, "
            f"idle_notebooks={idle_notebooks_count}, "
            f"idle_endpoints={idle_endpoints_count}, "
            f"compliance={compliance_score}%"
        )
    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch metrics failed (non-blocking): {e}")


# Données mock — utilisées quand MOCK_MODE=true
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
    """
    Calcule les coûts réels par type de ressource depuis les données discovery.
    Utilisé quand Cost Explorer retourne $0 ou n'est pas disponible.
    Les coûts sont calculés depuis les monthly_cost_estimate de chaque ressource
    détectée via la Pricing API.

    Returns:
        dict: {"total_cost": float, "cost_by_resource": {...}}
    """
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
    """
    Génère les recommandations d'économies pour chaque ressource SageMaker.
    Si discovery est fourni, les notebooks idle (CPU < 5%) sont priorisés Critical.
    """
    recs = []

    idle_notebooks = []
    idle_endpoints = []
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
                issue = (
                    f"Auto-stop {len(idle_notebooks)} idle notebook(s) "
                    f"(avg CPU < 5% over 24h)"
                )
            elif name == "Endpoints" and idle_endpoints:
                priority = "Critical"
                issue = (
                    f"Delete {len(idle_endpoints)} idle endpoint(s) "
                    f"(0 invocations over 24h)"
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
                    "idle_count": len(idle_notebooks)
                    if name == "Notebooks"
                    else len(idle_endpoints)
                    if name == "Endpoints"
                    else 0,
                }
            )

    priority_score = {"Critical": 1, "High": 2, "Medium": 3}
    recs.sort(key=lambda x: (priority_score.get(x["priority"], 99), -x["savings"]))
    return recs
    """
    Génère les recommandations d'économies pour chaque ressource SageMaker.
    Si discovery est fourni, les notebooks idle (CPU < 5%) sont priorisés Critical.
    """
    recs = []

    # Notebooks idle détectés via CloudWatch
    idle_notebooks = []
    idle_endpoints = []
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

    rules = [
        ("notebooks", 0.75, "Notebooks", 20, "Low", "High"),
        ("training", 0.70, "Training", 50, "Medium", "Critical"),
        ("endpoints", 0.30, "Endpoints", 50, "Medium", "High"),
        ("storage", 0.75, "Storage", 10, "Low", "Medium"),
    ]

    for key, pct, name, seuil, effort, priority in rules:
        cost = cost_by_resource.get(key, 0)
        if cost > seuil:
            # Si des notebooks idle sont détectés, on monte la priorité à Critical
            if name == "Notebooks" and idle_notebooks:
                priority = "Critical"
                issue = (
                    f"Auto-stop {len(idle_notebooks)} idle notebook(s) "
                    f"(avg CPU < 5% over 24h)"
                )
            elif name == "Endpoints" and idle_endpoints:
                priority = "Critical"
                issue = (
                    f"Delete {len(idle_endpoints)} idle endpoint(s) "
                    f"(0 invocations over 24h)"
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
                    "idle_count": len(idle_notebooks)
                    if name == "Notebooks"
                    else len(idle_endpoints)
                    if name == "Endpoints"
                    else 0,
                }
            )

    priority_score = {"Critical": 1, "High": 2, "Medium": 3}
    recs.sort(key=lambda x: (priority_score.get(x["priority"], 99), -x["savings"]))
    return recs


def get_optimization_issue(resource_type):
    """Retourne la description de l'optimisation pour chaque type de ressource"""
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
    rgpd_data=None,
    eu_ai_act_data=None,
):
    """
    Génère un rapport Markdown avec résumé exécutif et recommandations.

    Args:
        total_cost (float): Coût total mensuel
        total_savings (float): Économies totales identifiées
        savings_pct (float): Pourcentage d'économies
        recs (list): Liste des recommandations triées par priorité
        report_date (str): Date du rapport (YYYY-MM-DD)

    Returns:
        str: Rapport formaté en Markdown
    """
    markdown = f"""# ML Cost Analysis Report

**Generated:** {report_date}

## Executive Summary

| Metric | Value |
|--------|-------|
| **Total Monthly Spend** | ${total_cost:,.2f} |
| **Identified Savings** | ${total_savings:,.2f} |
| **Savings Potential** | **{savings_pct}%** |
| **Recommendations** | {len(recs)} items |

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

    markdown += """
---

## Next Steps (Sorted by ROI)

"""

    for idx, rec in enumerate(recs, 1):
        markdown += f"{idx}. **{rec['type']}** - {rec['issue']}\n"
        markdown += f"   - Potential Savings: ${rec['savings']:,.2f}/month\n"
        markdown += f"   - Effort: {rec['effort']} | Priority: {rec['priority']}\n\n"

    # Section RGPD
    if rgpd_data:
        risk = rgpd_data.get("global_risk", "Unknown")
        risk_emoji = {"Low": "✅", "Medium": "⚠️", "High": "🔴", "Unknown": "❓"}.get(
            risk, "❓"
        )
        markdown += "\n---\n\n## GDPR Compliance\n\n"
        markdown += f"**Global Risk Level: {risk_emoji} {risk}**\n\n"

        all_resources = rgpd_data.get("notebooks", []) + rgpd_data.get("endpoints", [])
        non_compliant = [r for r in all_resources if r["rgpd_risk"] != "Low"]

        if non_compliant:
            markdown += "| Resource | Type | Risk | Alerts |\n"
            markdown += "|----------|------|------|--------|\n"
            for r in non_compliant:
                alerts = " / ".join(r["alerts"])
                markdown += (
                    f"| {r['resource']} | {r['type']} | {r['rgpd_risk']} | {alerts} |\n"
                )
        else:
            markdown += "All scanned resources have compliant GDPR tags.\n"

    # Section EU AI Act
    if eu_ai_act_data and eu_ai_act_data.get("endpoints"):
        status = eu_ai_act_data.get("global_status", "N/A")
        status_emoji = {
            "Compliant": "✅",
            "Incomplete": "⚠️",
            "Non-Compliant": "🔴",
            "N/A": "—",
        }.get(status, "❓")
        high_risk = eu_ai_act_data.get("high_risk_count", 0)

        markdown += "\n---\n\n## EU AI Act Compliance\n\n"
        markdown += f"**Global Status: {status_emoji} {status}**"
        if high_risk > 0:
            markdown += f" | **High-Risk Models: {high_risk}**"
        markdown += "\n\n"
        markdown += "> Penalties up to \u20ac35M or 7% of global turnover for non-compliant high-risk AI systems.\n\n"

        non_compliant = [r for r in eu_ai_act_data["endpoints"] if not r["compliant"]]
        if non_compliant:
            markdown += "| Endpoint | Risk Level | Human Oversight | Alerts |\n"
            markdown += "|----------|------------|-----------------|--------|\n"
            for r in non_compliant:
                alerts = " / ".join(r["alerts"])
                markdown += f"| {r['endpoint']} | {r['ai_risk_level']} | {r['human_oversight']} | {alerts} |\n"
        else:
            markdown += "All endpoints are EU AI Act compliant.\n"

    return markdown


def save_json_report(
    bucket_name, total_cost, total_savings, savings_pct, recs, report_date
):
    """
    Sauvegarde un rapport JSON structuré en S3 avec schéma strict.

    JSON Schema:
    {
        "metadata": {
            "report_date": str (YYYY-MM-DD),
            "generated_at": str (ISO 8601),
            "version": str (semantic version)
        },
        "summary": {
            "total_monthly_spend": float,
            "identified_savings": float,
            "savings_percentage": float,
            "recommendation_count": int
        },
        "optimizations": [
            {
                "category": str,
                "issue": str,
                "monthly_savings": float,
                "effort": str,
                "priority": str
            }
        ]
    }

    Args:
        bucket_name (str): Nom du bucket S3
        total_cost (float): Coût total mensuel
        total_savings (float): Économies totales
        savings_pct (float): Pourcentage d'économies
        recs (list): Recommandations
        report_date (str): Date du rapport

    Returns:
        str: URL du fichier JSON en S3
    """
    try:
        now = datetime.now(timezone.utc)

        # Structured JSON with strict schema
        report_data = {
            "metadata": {
                "report_date": report_date,
                "generated_at": now.isoformat(),
                "version": "1.0.0",
            },
            "summary": {
                "total_monthly_spend": float(total_cost),
                "identified_savings": float(total_savings),
                "savings_percentage": float(savings_pct),
                "recommendation_count": len(recs),
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

        # Upload to S3
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
    """
    Sauvegarde le rapport Markdown en S3.

    Args:
        bucket_name (str): Nom du bucket S3
        markdown_content (str): Contenu du rapport en Markdown
        report_date (str): Date du rapport

    Returns:
        str: URL du fichier Markdown en S3
    """
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
    rgpd_risk=None,
    eu_ai_act_status=None,
):
    """
    Envoie une notification SNS avec un résumé des économies identifiées.
    Gère les erreurs gracieusement sans faire échouer la Lambda.

    Args:
        sns_topic_arn (str): ARN du topic SNS
        total_savings (float): Économies totales identifiées
        savings_pct (float): Pourcentage d'économies
        recommendation_count (int): Nombre de recommandations
        markdown_s3_url (str): URL du rapport Markdown en S3
    """
    try:
        message = (
            f"ML Cost Analysis — ${total_savings:,.2f} identified in savings "
            f"({savings_pct}%) across {recommendation_count} recommendations.\n\n"
        )
        if rgpd_risk:
            risk_emoji = {"Low": "✅", "Medium": "⚠️", "High": "🔴"}.get(rgpd_risk, "❓")
            message += f"GDPR Risk: {risk_emoji} {rgpd_risk}\n"
        if eu_ai_act_status:
            status_emoji = {
                "Compliant": "✅",
                "Incomplete": "⚠️",
                "Non-Compliant": "🔴",
            }.get(eu_ai_act_status, "❓")
            message += f"EU AI Act: {status_emoji} {eu_ai_act_status}\n"
        if rgpd_risk or eu_ai_act_status:
            message += "\n"
        message += f"Full report: {markdown_s3_url}"

        response = get_sns_client().publish(
            TopicArn=sns_topic_arn,
            Subject="ML Cost Analysis Report - Weekly Summary",
            Message=message,
        )

        logger.info(f"✅ SNS notification sent: {response['MessageId']}")
        return response
    except ClientError as e:
        # Log error but don't fail Lambda execution
        logger.warning(f"⚠️  Warning: SNS notification failed (non-blocking): {e}")
        return None


def get_real_costs():
    """
    Récupère les coûts SageMaker réels via Cost Explorer.
    Retourne la même structure que MOCK_DATA.

    Note: Cost Explorer peut nécessiter jusqu'à 24h après activation
    avant de retourner des données. En cas d'absence de données ou
    d'erreur, retourne MOCK_DATA comme fallback avec un avertissement.

    Returns:
        dict: {"total_cost": float, "cost_by_resource": {...}}
    """
    try:
        today = date.today()
        # Cost Explorer requiert start != end — on prend le mois courant
        # Si on est le 1er du mois, on recule d'un mois pour éviter start == end
        start_date = today.replace(day=1)
        if start_date == today:
            prev_month = today.replace(day=1) - __import__("datetime").timedelta(days=1)
            start_date = prev_month.replace(day=1)

        start = start_date.strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        response = get_ce_client().get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon SageMaker"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )

        results = response.get("ResultsByTime", [])
        if not results:
            logger.info(
                "ℹ️ Cost Explorer : aucun résultat — pas encore de dépenses SageMaker ce mois-ci."
            )
            return {
                "total_cost": 0.0,
                "cost_by_resource": {
                    "notebooks": 0.0,
                    "training": 0.0,
                    "endpoints": 0.0,
                    "storage": 0.0,
                    "other": 0.0,
                },
                "cost_explorer_available": True,
            }

        total_cost = sum(
            float(group["Metrics"]["UnblendedCost"]["Amount"])
            for result in results
            for group in result.get("Groups", [])
        )

        if total_cost == 0:
            logger.info(
                "ℹ️ Cost Explorer : coût SageMaker = $0 ce mois-ci "
                "(pas encore de dépenses ou délai 24h)."
            )
            return {
                "total_cost": 0.0,
                "cost_by_resource": {
                    "notebooks": 0.0,
                    "training": 0.0,
                    "endpoints": 0.0,
                    "storage": 0.0,
                    "other": 0.0,
                },
                "cost_explorer_available": True,
            }

        # Répartition estimée par type de ressource (proportions SageMaker typiques)
        cost_by_resource = {
            "notebooks": round(total_cost * 0.25, 2),
            "training": round(total_cost * 0.35, 2),
            "endpoints": round(total_cost * 0.20, 2),
            "storage": round(total_cost * 0.05, 2),
            "other": round(total_cost * 0.15, 2),
        }

        logger.info(f"✅ Cost Explorer : coût SageMaker du mois = ${total_cost:,.2f}")
        return {
            "total_cost": round(total_cost, 2),
            "cost_by_resource": cost_by_resource,
        }

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("DataUnavailableException", "RequestExpiredException"):
            logger.warning(
                f"⚠️ Cost Explorer non disponible ({error_code}) — "
                "données pas encore prêtes (délai 24h)."
            )
        else:
            logger.error(f"❌ Erreur Cost Explorer : {e}.")
        return {
            "total_cost": 0.0,
            "cost_by_resource": {
                "notebooks": 0.0,
                "training": 0.0,
                "endpoints": 0.0,
                "storage": 0.0,
                "other": 0.0,
            },
            "cost_explorer_available": False,
        }


def handler(event, context):
    """
    Main Lambda handler for ML cost analysis.

    Flow:
    1. Fetch cost data (mock or AWS APIs)
    2. Generate recommendations sorted by ROI
    3. Save JSON structured report to S3
    4. Save Markdown report to S3
    5. Send SNS notification (non-blocking)
    6. Return structured response
    """
    logger.info("🚀 ML Cost Analysis - Starting")

    try:
        # Get environment variables
        report_bucket = os.environ.get("REPORT_BUCKET")
        sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
        mock_mode = os.environ.get("MOCK_MODE", "false").lower() == "true"

        if not report_bucket and not mock_mode:
            raise ValueError("REPORT_BUCKET environment variable not set")

        # Fetch cost data
        if mock_mode:
            logger.info("📊 Using mock data (MOCK_MODE=true)")
            data = MOCK_DATA
            rgpd_data = None
            eu_ai_act_data = None
        else:
            logger.info("📊 Fetching real costs from Cost Explorer...")
            data = get_real_costs()
            logger.info("🔍 Scanning real SageMaker resources...")
            discovery = run_discovery()
            data["discovery"] = discovery
            rgpd_data = discovery.get("rgpd_compliance")
            eu_ai_act_data = discovery.get("eu_ai_act_compliance")

            # Si Cost Explorer retourne $0, on calcule depuis les ressources réelles
            if data["total_cost"] == 0.0:
                real_costs = build_cost_from_discovery(discovery)
                if real_costs["total_cost"] > 0:
                    logger.info(
                        f"📊 Coûts calculés depuis les ressources détectées : "
                        f"${real_costs['total_cost']:,.2f}"
                    )
                    data["total_cost"] = real_costs["total_cost"]
                    data["cost_by_resource"] = real_costs["cost_by_resource"]
                else:
                    logger.info(
                        "ℹ️ Aucune ressource SageMaker active détectée — coût réel = $0"
                    )

        # Generate recommendations (sorted by ROI/Priority)
        discovery = data.get("discovery")
        recs = generate_recommendations(data["cost_by_resource"], discovery)
        total_cost = data["total_cost"]
        total_savings = sum(r["savings"] for r in recs)
        savings_pct = (
            round(total_savings / total_cost * 100, 1) if total_cost > 0 else 0.0
        )

        # Generate report date
        report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        logger.info(f"💰 Total Cost   : ${total_cost:,.2f}")
        logger.info(f"💸 Total Savings: ${total_savings:,.2f}")
        logger.info(f"📈 Savings %    : {savings_pct}%")
        logger.info(f"📋 Recommendations: {len(recs)}")

        # Initialize report URLs (may not be set in mock mode)
        json_url = None
        markdown_url = None

        # 1. Save structured JSON report to S3 (skip in mock mode)
        if not mock_mode:
            json_url = save_json_report(
                report_bucket, total_cost, total_savings, savings_pct, recs, report_date
            )

            # 2. Generate and save Markdown report
            markdown_content = generate_markdown_report(
                total_cost,
                total_savings,
                savings_pct,
                recs,
                report_date,
                rgpd_data,
                eu_ai_act_data,
            )
            markdown_url = save_markdown_report(
                report_bucket, markdown_content, report_date
            )

            # 3. Send SNS notification (non-blocking - errors caught)
            if sns_topic_arn:
                send_sns_notification(
                    sns_topic_arn,
                    total_savings,
                    savings_pct,
                    len(recs),
                    markdown_url,
                    rgpd_data.get("global_risk") if rgpd_data else None,
                    eu_ai_act_data.get("global_status") if eu_ai_act_data else None,
                )
            else:
                logger.warning(
                    "⚠️  Warning: SNS_TOPIC_ARN not configured, skipping notification"
                )

            # 4. Publish custom CloudWatch metrics
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
            # Compliance score : % de ressources sans alerte RGPD High
            all_rgpd = []
            if rgpd_data:
                all_rgpd = rgpd_data.get("notebooks", []) + rgpd_data.get(
                    "endpoints", []
                )
            compliant_count = sum(1 for r in all_rgpd if r.get("rgpd_risk") == "Low")
            compliance_score = (
                round(compliant_count / len(all_rgpd) * 100) if all_rgpd else 100.0
            )

            publish_metrics(
                total_savings, idle_notebooks, idle_endpoints, compliance_score
            )
        else:
            logger.info("⏭️  Skipping S3 uploads and SNS in MOCK_MODE")

        # Return success response
        # Ressources idle à traiter par action.py
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
    # Local test mode - only with MOCK_MODE
    os.environ["REPORT_BUCKET"] = "test-bucket"  # Won't actually upload in mock mode

    result = handler({}, None)
    print("\n" + "=" * 60)
    print("LOCAL TEST OUTPUT")
    print("=" * 60)
    print(result["body"])
