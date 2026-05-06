import json
import os
from datetime import datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="ml-cost-optimizer")


def get_sagemaker_client():
    """Connexion au service SageMaker"""
    return boto3.client(
        "sagemaker", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    )


def get_cloudwatch_client():
    """Connexion à CloudWatch pour les métriques"""
    return boto3.client(
        "cloudwatch", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    )


def is_endpoint_idle(endpoint_name, hours=24):
    """
    Vérifie si un endpoint est idle via CloudWatch Invocations.
    Un endpoint est considéré idle s'il n'a reçu aucune requête
    sur les dernières X heures.

    Args:
        endpoint_name (str): Nom de l'endpoint
        hours (int): Fenêtre de temps en heures (défaut 24h)

    Returns:
        dict: {"is_idle": bool, "total_invocations": int, "hours_checked": int}
    """
    from datetime import timedelta
    cw = get_cloudwatch_client()

    try:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)

        response = cw.get_metric_statistics(
            Namespace="AWS/SageMaker",
            MetricName="Invocations",
            Dimensions=[{"Name": "EndpointName", "Value": endpoint_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Sum"],
        )

        datapoints = response.get("Datapoints", [])
        total_invocations = int(sum(d["Sum"] for d in datapoints))
        is_idle = total_invocations == 0

        logger.info(
            f"{'⚠️' if is_idle else '✅'} {endpoint_name} : "
            f"{total_invocations} invocations sur {hours}h → {'IDLE' if is_idle else 'actif'}"
        )
        return {"is_idle": is_idle, "total_invocations": total_invocations, "hours_checked": hours}

    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch indisponible pour {endpoint_name} : {e}")
        return {"is_idle": False, "total_invocations": -1, "hours_checked": hours}


def is_notebook_idle(notebook_name, idle_threshold_pct=5.0, hours=24):
    """
    Vérifie si un notebook est idle via CloudWatch CPUUtilization.
    Un notebook est considéré idle si son CPU moyen est sous le seuil
    sur les dernières X heures.

    Args:
        notebook_name (str): Nom du notebook
        idle_threshold_pct (float): Seuil CPU en % (défaut 5%)
        hours (int): Fenêtre de temps en heures (défaut 24h)

    Returns:
        dict: {"is_idle": bool, "avg_cpu": float, "hours_checked": int}
    """
    from datetime import timedelta
    cw = get_cloudwatch_client()

    try:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)

        response = cw.get_metric_statistics(
            Namespace="AWS/SageMaker",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "NotebookInstanceName", "Value": notebook_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1 point par heure
            Statistics=["Average"],
        )

        datapoints = response.get("Datapoints", [])

        if not datapoints:
            # Pas de métriques = notebook allumé mais jamais utilisé
            logger.info(f"⚠️ {notebook_name} : aucune métrique CPU → considéré idle")
            return {"is_idle": True, "avg_cpu": 0.0, "hours_checked": hours}

        avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
        is_idle = avg_cpu < idle_threshold_pct

        logger.info(
            f"{'⚠️' if is_idle else '✅'} {notebook_name} : "
            f"CPU moyen = {avg_cpu:.1f}% sur {hours}h → {'IDLE' if is_idle else 'actif'}"
        )
        return {"is_idle": is_idle, "avg_cpu": round(avg_cpu, 1), "hours_checked": hours}

    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch indisponible pour {notebook_name} : {e}")
        return {"is_idle": False, "avg_cpu": -1.0, "hours_checked": hours}


def get_account_id():
    """Récupère l'Account ID AWS courant via STS."""
    return boto3.client("sts").get_caller_identity()["Account"]


def get_instance_hourly_price(instance_type, region="eu-west-1"):
    """
    Récupère le vrai prix horaire d'une instance SageMaker via l'AWS Pricing API.
    La Pricing API est uniquement disponible en us-east-1.

    Args:
        instance_type (str): Type d'instance (ex: ml.t3.medium)
        region (str): Région AWS (ex: eu-west-1)

    Returns:
        float: Prix horaire en $/h
    """
    # Mapping région → nom lisible pour la Pricing API
    region_name_map = {
        "eu-west-1": "EU (Ireland)",
        "us-east-1": "US East (N. Virginia)",
        "us-west-2": "US West (Oregon)",
        "eu-central-1": "EU (Frankfurt)",
    }
    location = region_name_map.get(region, "EU (Ireland)")

    # Prix par défaut si l'API échoue
    default_prices = {
        "ml.t3.medium": 0.05,
        "ml.t3.xlarge": 0.20,
        "ml.p3.2xlarge": 3.83,
    }

    try:
        pricing = boto3.client("pricing", region_name="us-east-1")
        response = pricing.get_products(
            ServiceCode="AmazonSageMaker",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "component", "Value": "Notebook"},
            ],
            MaxResults=1,
        )

        price_list = response.get("PriceList", [])
        if not price_list:
            logger.warning(f"⚠️ Prix non trouvé pour {instance_type}, fallback utilisé")
            return default_prices.get(instance_type, 0.10)

        product = json.loads(price_list[0])
        on_demand = product.get("terms", {}).get("OnDemand", {})
        for term in on_demand.values():
            for dimension in term.get("priceDimensions", {}).values():
                price = float(dimension["pricePerUnit"].get("USD", 0))
                if price > 0:
                    logger.info(
                        f"✅ Prix {instance_type} ({location}) : ${price:.4f}/h"
                    )
                    return price

        logger.warning(f"⚠️ Prix USD introuvable pour {instance_type}, fallback utilisé")
        return default_prices.get(instance_type, 0.10)

    except ClientError as e:
        logger.error(f"❌ Erreur Pricing API pour {instance_type} : {e}")
        return default_prices.get(instance_type, 0.10)


def scan_studio_apps():
    """
    Liste toutes les Studio Apps SageMaker actives (successeur des notebooks classiques).

    Returns:
        list: Liste des Studio Apps avec leur statut et domaine
    """
    sm = get_sagemaker_client()
    apps = []

    try:
        paginator = sm.get_paginator("list_apps")
        for page in paginator.paginate():
            for app in page["Apps"]:
                # InService = tourne et coûte de l'argent
                if app["AppType"] in ("JupyterServer", "KernelGateway", "JupyterLab"):
                    apps.append(
                        {
                            "name": app["AppName"],
                            "type": app["AppType"],
                            "domain_id": app.get("DomainId", ""),
                            "user_profile": app.get("UserProfileName", ""),
                            "status": app["Status"],
                            "is_running": app["Status"] == "InService",
                            "last_modified": str(app.get("LastHealthCheckTimestamp", "")),
                        }
                    )

        logger.info(f"✅ {len(apps)} Studio apps trouvées")
        return apps

    except ClientError as e:
        logger.error(f"❌ Erreur scan Studio apps : {e}")
        return []


def scan_notebooks():
    """
    Liste tous les notebooks SageMaker avec leur statut,
    type d'instance et coût estimé.

    Returns:
        list: Liste de notebooks avec leurs infos
    """
    sm = get_sagemaker_client()
    notebooks = []

    try:
        paginator = sm.get_paginator("list_notebook_instances")
        for page in paginator.paginate():
            for nb in page["NotebookInstances"]:
                instance_type = nb.get("InstanceType", "inconnu")
                region = os.environ.get("AWS_REGION", "eu-west-1")
                hourly_price = get_instance_hourly_price(instance_type, region)
                notebooks.append(
                    {
                        "name": nb["NotebookInstanceName"],
                        "status": nb["NotebookInstanceStatus"],
                        "instance_type": instance_type,
                        "last_modified": str(nb.get("LastModifiedTime", "")),
                        "is_running": nb["NotebookInstanceStatus"] == "InService",
                        "carbon_footprint_kg_month": calculate_carbon_footprint(instance_type),
                        "hourly_price": hourly_price,
                        "monthly_cost_estimate": round(hourly_price * 730, 2),
                        **(
                            is_notebook_idle(nb["NotebookInstanceName"])
                            if nb["NotebookInstanceStatus"] == "InService"
                            else {"is_idle": False, "avg_cpu": -1.0, "hours_checked": 24}
                        ),
                    }
                )

        logger.info(f"✅ {len(notebooks)} notebooks trouvés")
        return notebooks

    except ClientError as e:
        logger.error(f"❌ Erreur scan notebooks : {e}")
        return []


def scan_endpoints():
    """
    Liste tous les endpoints SageMaker actifs.

    Returns:
        list: Liste des endpoints avec leur statut
    """
    sm = get_sagemaker_client()
    endpoints = []

    try:
        paginator = sm.get_paginator("list_endpoints")
        for page in paginator.paginate():
            for ep in page["Endpoints"]:
                endpoints.append(
                    {
                        "name": ep["EndpointName"],
                        "status": ep["EndpointStatus"],
                        "last_modified": str(ep.get("LastModifiedTime", "")),
                        "is_running": ep["EndpointStatus"] == "InService",
                        **(
                            is_endpoint_idle(ep["EndpointName"])
                            if ep["EndpointStatus"] == "InService"
                            else {"is_idle": False, "total_invocations": -1, "hours_checked": 24}
                        ),
                    }
                )

        logger.info(f"✅ {len(endpoints)} endpoints trouvés")
        return endpoints

    except ClientError as e:
        logger.error(f"❌ Erreur scan endpoints : {e}")
        return []


def scan_training_jobs():
    """
    Liste les training jobs récents (30 derniers jours).

    Returns:
        list: Liste des training jobs
    """
    sm = get_sagemaker_client()
    jobs = []

    try:
        paginator = sm.get_paginator("list_training_jobs")
        for page in paginator.paginate(StatusEquals="Completed"):
            for job in page["TrainingJobSummaries"]:
                jobs.append(
                    {
                        "name": job["TrainingJobName"],
                        "status": job["TrainingJobStatus"],
                        "creation_time": str(job.get("CreationTime", "")),
                        "end_time": str(job.get("TrainingEndTime", "")),
                    }
                )

        logger.info(f"✅ {len(jobs)} training jobs trouvés")
        return jobs

    except ClientError as e:
        logger.error(f"❌ Erreur scan training jobs : {e}")
        return []


def check_eu_ai_act_compliance(endpoint_name):
    """
    Vérifie la conformité EU AI Act d'un endpoint SageMaker (modèle en production).

    Tags attendus :
      - ai-risk-level       : high | limited | minimal
      - human-oversight     : enabled | disabled
      - model-purpose       : description du cas d'usage (Art. 13)
      - conformity-assessment : done | pending | not-required (Art. 9)

    Pénalités EU AI Act :
      - Jusqu'à 35M€ ou 7% CA mondial pour systèmes haut risque non conformes
      - Jusqu'à 15M€ ou 3% CA pour autres violations

    Returns:
        dict: Statut conformité EU AI Act et alertes par article
    """
    sm = get_sagemaker_client()
    alerts = []

    try:
        region = os.environ.get("AWS_REGION", "eu-west-1")
        arn = f"arn:aws:sagemaker:{region}:{get_account_id()}:endpoint/{endpoint_name}"
        tags_response = sm.list_tags(ResourceArn=arn)
        tags = {t["Key"]: t["Value"] for t in tags_response.get("Tags", [])}

        ai_risk = tags.get("ai-risk-level", "unknown")

        # Art. 9 — classification du risque obligatoire
        if ai_risk == "unknown":
            alerts.append("[Art. 9] Tag 'ai-risk-level' manquant — risque non classifié")

        # Art. 14 — supervision humaine obligatoire pour les systèmes haut risque
        if ai_risk == "high" and tags.get("human-oversight") != "enabled":
            alerts.append("[Art. 14] Modèle haut risque sans human-oversight: enabled — pénalité jusqu'à 35M€")

        # Art. 9 — évaluation de conformité requise pour haut risque
        if ai_risk == "high" and "conformity-assessment" not in tags:
            alerts.append("[Art. 9] Tag 'conformity-assessment' manquant sur modèle haut risque")

        # Art. 13 — transparence et documentation du cas d'usage
        if "model-purpose" not in tags:
            alerts.append("[Art. 13] Tag 'model-purpose' manquant — cas d'usage non documenté")

        if not alerts:
            alerts.append("Conforme EU AI Act")

    except ClientError:
        alerts.append("Impossible de vérifier les tags EU AI Act")
        ai_risk = "unknown"

    return {
        "endpoint": endpoint_name,
        "ai_risk_level": ai_risk,
        "human_oversight": tags.get("human-oversight", "not-set") if "tags" in dir() else "unknown",
        "alerts": alerts,
        "compliant": not alerts or (len(alerts) == 1 and "Conforme" in alerts[0]),
    }


def _build_eu_ai_act_compliance(endpoints):
    """Évalue la conformité EU AI Act sur tous les endpoints."""
    if not endpoints:
        return {"endpoints": [], "global_status": "N/A", "high_risk_count": 0}

    results = [check_eu_ai_act_compliance(e["name"]) for e in endpoints]

    high_risk_non_compliant = [r for r in results if r["ai_risk_level"] == "high" and not r["compliant"]]
    unknown_risk = [r for r in results if r["ai_risk_level"] == "unknown"]

    if high_risk_non_compliant:
        global_status = "Non-Compliant"
    elif unknown_risk:
        global_status = "Incomplete"
    else:
        global_status = "Compliant"

    logger.info(f"✅ EU AI Act scan : {len(results)} endpoints, statut global = {global_status}")
    return {
        "endpoints": results,
        "global_status": global_status,
        "high_risk_count": len([r for r in results if r["ai_risk_level"] == "high"]),
    }


def check_rgpd_compliance(resource_name, resource_type):
    """
    Vérifie la conformité RGPD basique d'une ressource.
    Retourne les alertes et le niveau de risque.

    Args:
        resource_name: Nom de la ressource
        resource_type: Type (notebook, endpoint, training)

    Returns:
        dict: Statut conformité et alertes
    """
    sm = get_sagemaker_client()
    alerts = []
    risk_level = "Low"

    try:
        # Vérifie les tags de la ressource
        arn = f"arn:aws:sagemaker:{os.environ.get('AWS_REGION', 'eu-west-1')}:{get_account_id()}:{resource_type}/{resource_name}"
        tags_response = sm.list_tags(ResourceArn=arn)
        tags = {t["Key"]: t["Value"] for t in tags_response.get("Tags", [])}

        # Vérifie les tags obligatoires RGPD
        if "owner" not in tags:
            alerts.append("⚠️ Tag 'owner' manquant → responsable inconnu")
            risk_level = "Medium"

        if "data-classification" not in tags:
            alerts.append(
                "⚠️ Tag 'data-classification' manquant → données non classifiées"
            )
            risk_level = "Medium"

        if "expiration-date" not in tags:
            alerts.append(
                "⚠️ Tag 'expiration-date' manquant → pas de durée de rétention"
            )
            risk_level = "High"

        if not alerts:
            alerts.append("✅ Tags RGPD conformes")

    except ClientError:
        alerts.append("⚠️ Impossible de vérifier les tags")
        risk_level = "Unknown"

    return {
        "resource": resource_name,
        "type": resource_type,
        "rgpd_risk": risk_level,
        "alerts": alerts,
    }


def _build_rgpd_compliance(notebooks, endpoints):
    """Calcule la conformité RGPD une seule fois et détermine le risque global."""
    nb_results = [
        check_rgpd_compliance(n["name"], "notebook-instance") for n in notebooks
    ]
    ep_results = [check_rgpd_compliance(e["name"], "endpoint") for e in endpoints]
    all_results = nb_results + ep_results

    if any(r["rgpd_risk"] == "High" for r in all_results):
        global_risk = "High"
    elif any(r["rgpd_risk"] == "Medium" for r in all_results):
        global_risk = "Medium"
    else:
        global_risk = "Low"

    return {
        "notebooks": nb_results,
        "endpoints": ep_results,
        "global_risk": global_risk,
    }


def calculate_carbon_footprint(instance_type):
    """
    Retourne une estimation de l'empreinte carbone en kg CO² par mois
    selon le type d'instance SageMaker.

    Args:
        instance_type (str): Type d'instance (ex: ml.t3.medium)

    Returns:
        float: Empreinte carbone estimée en kg/mois
    """
    carbon_map = {
        "ml.t3.medium": 2.5,
        "ml.t3.xlarge": 8.0,
        "ml.p3.2xlarge": 45.0,
        "ml.p3.8xlarge": 180.0,
    }
    return carbon_map.get(instance_type, 5.0)


def run_discovery():
    """
    Point d'entrée principal : scanne toutes les ressources
    SageMaker et retourne un rapport complet.

    Returns:
        dict: Rapport complet avec toutes les ressources
    """
    logger.info("🔍 Démarrage du scan SageMaker...")

    notebooks = scan_notebooks()
    studio_apps = scan_studio_apps()
    endpoints = scan_endpoints()
    training_jobs = scan_training_jobs()

    running_notebooks = [n for n in notebooks if n["is_running"]]
    running_studio_apps = [a for a in studio_apps if a["is_running"]]
    running_endpoints = [e for e in endpoints if e["is_running"]]

    rapport = {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_notebooks": len(notebooks),
            "running_notebooks": len(running_notebooks),
            "total_studio_apps": len(studio_apps),
            "running_studio_apps": len(running_studio_apps),
            "total_endpoints": len(endpoints),
            "running_endpoints": len(running_endpoints),
            "total_training_jobs": len(training_jobs),
        },
        "notebooks": notebooks,
        "studio_apps": studio_apps,
        "endpoints": endpoints,
        "training_jobs": training_jobs,
        "rgpd_compliance": _build_rgpd_compliance(notebooks, endpoints),
        "eu_ai_act_compliance": _build_eu_ai_act_compliance(endpoints),
    }

    logger.info(
        f"📊 Scan terminé : {len(running_notebooks)} notebooks, "
        f"{len(running_studio_apps)} Studio apps, "
        f"{len(running_endpoints)} endpoints actifs"
    )
    return rapport


if __name__ == "__main__":
    # Test local
    os.environ["AWS_REGION"] = "eu-west-1"
    result = run_discovery()
    import json

    print(json.dumps(result, indent=2, default=str))
