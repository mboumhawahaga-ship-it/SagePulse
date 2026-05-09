import json
import os
from datetime import date, datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
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


def get_dynamodb_table():
    """Lazy-load DynamoDB table pour l'historique des ressources idle."""
    table_name = os.environ.get("IDLE_TABLE")
    if not table_name:
        return None
    return boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    ).Table(table_name)


def save_to_dynamodb(resource_id, resource_type, status, cost, alert_sent):
    """
    Enregistre une ressource idle dans DynamoDB.

    Schema :
      ResourceId  (PK) : identifiant unique de la ressource
      Timestamp   (SK) : ISO 8601 UTC
      Type             : notebook | endpoint | studio_app
      Status           : idle | running
      Cost             : coût mensuel estimé en $
      AlertSent        : bool — alerte déjà envoyée
      ExpiresAt        : TTL Unix timestamp (90 jours)
    """
    table = get_dynamodb_table()
    if not table:
        logger.warning("⚠️ IDLE_TABLE non configuré — save_to_dynamodb ignoré")
        return
    try:
        now = datetime.now(timezone.utc)
        ttl = int(now.timestamp()) + 90 * 24 * 3600  # 90 jours
        table.put_item(Item={
            "ResourceId": resource_id,
            "Timestamp": now.isoformat(),
            "Type": resource_type,
            "Status": status,
            "Cost": str(round(cost, 2)),
            "AlertSent": alert_sent,
            "ExpiresAt": ttl,
        })
        logger.info(f"✅ DynamoDB — {resource_id} ({resource_type}) enregistré")
    except ClientError as e:
        logger.warning(f"⚠️ DynamoDB save échoué pour {resource_id} (non-bloquant): {e}")


def alert_already_sent(resource_id, within_hours=4):
    """
    Vérifie si une alerte a déjà été envoyée pour cette ressource
    dans les dernières `within_hours` heures.
    Utilise Query sur ResourceId pour éviter un Scan.

    Returns:
        bool: True si une alerte récente existe, False sinon
    """
    table = get_dynamodb_table()
    if not table:
        return False
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
        response = table.query(
            KeyConditionExpression=(
                Key("ResourceId").eq(resource_id)
                & Key("Timestamp").gte(cutoff)
            ),
            FilterExpression=Attr("AlertSent").eq(True),
            Limit=1,
        )
        already_sent = len(response.get("Items", [])) > 0
        if already_sent:
            logger.info(f"ℹ️ Alerte déjà envoyée pour {resource_id} dans les {within_hours}h — skip")
        return already_sent
    except ClientError as e:
        logger.warning(f"⚠️ DynamoDB query échoué pour {resource_id} (non-bloquant): {e}")
        return False


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
            f"Full report: {markdown_s3_url}"
        )

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
    Récupère les coûts SageMaker réels via Cost Explorer groupés par USAGE_TYPE.
    Permet d'obtenir les coûts réels par type de ressource (notebook, endpoint, training).
    """
    try:
        today = date.today()
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
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Metrics=["UnblendedCost"],
        )

        results = response.get("ResultsByTime", [])
        if not results:
            logger.info("ℹ️ Cost Explorer : aucun résultat ce mois-ci.")
            return {
                "total_cost": 0.0,
                "cost_by_resource": {
                    "notebooks": 0.0, "training": 0.0, "endpoints": 0.0,
                    "storage": 0.0, "other": 0.0,
                },
                "cost_explorer_available": True,
            }

        cost_by_resource = {
            "notebooks": 0.0, "training": 0.0, "endpoints": 0.0,
            "storage": 0.0, "other": 0.0,
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
            return {
                "total_cost": 0.0,
                "cost_by_resource": {
                    "notebooks": 0.0, "training": 0.0, "endpoints": 0.0,
                    "storage": 0.0, "other": 0.0,
                },
                "cost_explorer_available": True,
            }

        logger.info(f"✅ Cost Explorer : coût SageMaker = ${total_cost:,.2f} (répartition réelle par USAGE_TYPE)")
        return {"total_cost": total_cost, "cost_by_resource": cost_by_resource}

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("DataUnavailableException", "RequestExpiredException"):
            logger.warning(f"⚠️ Cost Explorer non disponible ({error_code}).")
        else:
            logger.error(f"❌ Erreur Cost Explorer : {e}.")
        return {
            "total_cost": 0.0,
            "cost_by_resource": {
                "notebooks": 0.0, "training": 0.0, "endpoints": 0.0,
                "storage": 0.0, "other": 0.0,
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
        else:
            logger.info("📊 Fetching real costs from Cost Explorer...")
            data = get_real_costs()
            logger.info("🔍 Scanning real SageMaker resources...")
            discovery = run_discovery()
            data["discovery"] = discovery

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
            )
            markdown_url = save_markdown_report(
                report_bucket, markdown_content, report_date
            )

            # 3. Log idle resources in DynamoDB + deduplicate SNS alerts
            idle_notebooks_raw = [
                n for n in (discovery or {}).get("notebooks", [])
                if n.get("is_idle") and n.get("is_running")
            ]
            idle_endpoints_raw = [
                e for e in (discovery or {}).get("endpoints", [])
                if e.get("is_idle") and e.get("is_running")
            ]

            new_alerts = []
            for nb in idle_notebooks_raw:
                already = alert_already_sent(nb["name"])
                save_to_dynamodb(
                    resource_id=nb["name"],
                    resource_type="notebook",
                    status="idle",
                    cost=nb.get("monthly_cost_estimate", 0.0),
                    alert_sent=not already,
                )
                if not already:
                    new_alerts.append(nb["name"])

            for ep in idle_endpoints_raw:
                already = alert_already_sent(ep["name"])
                save_to_dynamodb(
                    resource_id=ep["name"],
                    resource_type="endpoint",
                    status="idle",
                    cost=0.0,
                    alert_sent=not already,
                )
                if not already:
                    new_alerts.append(ep["name"])

            # 4. Send SNS only if new idle resources (deduplication)
            if sns_topic_arn and new_alerts:
                send_sns_notification(
                    sns_topic_arn,
                    total_savings,
                    savings_pct,
                    len(recs),
                    markdown_url,
                )
            elif not sns_topic_arn:
                logger.warning("⚠️ SNS_TOPIC_ARN not configured, skipping notification")
            else:
                logger.info("ℹ️ Toutes les ressources idle ont déjà été alertées — pas de nouvelle notification")

            # 5. Publish custom CloudWatch metrics
            idle_notebooks = len(idle_notebooks_raw)
            idle_endpoints = len(idle_endpoints_raw)
            publish_metrics(total_savings, idle_notebooks, idle_endpoints, 100.0)
        else:
            logger.info("⏭️ Skipping S3 uploads and SNS in MOCK_MODE")

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
