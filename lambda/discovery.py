import json
import os
from datetime import datetime, timedelta, timezone

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
        return {
            "is_idle": is_idle,
            "total_invocations": total_invocations,
            "hours_checked": hours,
        }

    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch indisponible pour {endpoint_name} : {e}")
        return {"is_idle": False, "total_invocations": -1, "hours_checked": hours}


def is_notebook_idle(notebook_name, idle_threshold_pct=5.0, hours=24):
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
        return {
            "is_idle": is_idle,
            "avg_cpu": round(avg_cpu, 1),
            "hours_checked": hours,
        }

    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch indisponible pour {notebook_name} : {e}")
        return {"is_idle": False, "avg_cpu": -1.0, "hours_checked": hours}


def is_studio_app_idle(domain_id, user_profile, app_name, hours=24):
    """
    Vérifie si une Studio App KernelGateway est idle via CloudWatch.
    Métrique : KernelGateway/CPUUtilization sur les dernières X heures.
    """
    cw = get_cloudwatch_client()
    try:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours)
        response = cw.get_metric_statistics(
            Namespace="AWS/SageMaker",
            MetricName="CPUUtilization",
            Dimensions=[
                {"Name": "DomainId", "Value": domain_id},
                {"Name": "UserProfileName", "Value": user_profile},
                {"Name": "AppName", "Value": app_name},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Average"],
        )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            return {"is_idle": True, "avg_cpu": 0.0, "hours_checked": hours}
        avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
        is_idle = avg_cpu < 5.0
        logger.info(
            f"{'⚠️' if is_idle else '✅'} Studio {app_name} : CPU={avg_cpu:.1f}% → {'IDLE' if is_idle else 'actif'}"
        )
        return {
            "is_idle": is_idle,
            "avg_cpu": round(avg_cpu, 1),
            "hours_checked": hours,
        }
    except ClientError as e:
        logger.warning(f"⚠️ CloudWatch indisponible pour Studio app {app_name} : {e}")
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
    sm = get_sagemaker_client()
    apps = []
    try:
        paginator = sm.get_paginator("list_apps")
        for page in paginator.paginate():
            for app in page["Apps"]:
                if app["AppType"] in ("JupyterServer", "KernelGateway", "JupyterLab"):
                    is_running = app["Status"] == "InService"
                    domain_id = app.get("DomainId", "")
                    user_profile = app.get("UserProfileName", "")
                    idle_info = (
                        is_studio_app_idle(domain_id, user_profile, app["AppName"])
                        if is_running and app["AppType"] == "KernelGateway"
                        else {"is_idle": False, "avg_cpu": -1.0, "hours_checked": 24}
                    )
                    apps.append(
                        {
                            "name": app["AppName"],
                            "type": app["AppType"],
                            "domain_id": domain_id,
                            "user_profile": user_profile,
                            "status": app["Status"],
                            "is_running": is_running,
                            "last_modified": str(
                                app.get("LastHealthCheckTimestamp", "")
                            ),
                            **idle_info,
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
                        "carbon_footprint_kg_month": calculate_carbon_footprint(
                            instance_type
                        ),
                        "hourly_price": hourly_price,
                        "monthly_cost_estimate": round(hourly_price * 730, 2),
                        **(
                            is_notebook_idle(nb["NotebookInstanceName"])
                            if nb["NotebookInstanceStatus"] == "InService"
                            else {
                                "is_idle": False,
                                "avg_cpu": -1.0,
                                "hours_checked": 24,
                            }
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
                            else {
                                "is_idle": False,
                                "total_invocations": -1,
                                "hours_checked": 24,
                            }
                        ),
                    }
                )

        logger.info(f"✅ {len(endpoints)} endpoints trouvés")
        return endpoints

    except ClientError as e:
        logger.error(f"❌ Erreur scan endpoints : {e}")
        return []


def scan_training_jobs(stuck_threshold_hours=24):
    """
    Liste les training jobs complétés ET détecte les jobs bloqués en InProgress
    depuis plus de stuck_threshold_hours heures.
    """
    sm = get_sagemaker_client()
    jobs = []
    now = datetime.now(timezone.utc)

    try:
        paginator = sm.get_paginator("list_training_jobs")

        for page in paginator.paginate(StatusEquals="Completed"):
            for job in page.get("TrainingJobSummaries", []):
                jobs.append(
                    {
                        "name": job["TrainingJobName"],
                        "status": job["TrainingJobStatus"],
                        "creation_time": str(job.get("CreationTime", "")),
                        "end_time": str(job.get("TrainingEndTime", "")),
                        "is_stuck": False,
                        "hours_running": None,
                    }
                )

        for page in paginator.paginate(StatusEquals="InProgress"):
            for job in page.get("TrainingJobSummaries", []):
                creation = job.get("CreationTime")
                hours_running = None
                is_stuck = False
                if creation:
                    hours_running = round((now - creation).total_seconds() / 3600, 1)
                    is_stuck = hours_running > stuck_threshold_hours
                if is_stuck:
                    logger.warning(
                        f"⚠️ Training job bloqué : {job['TrainingJobName']} "
                        f"en InProgress depuis {hours_running}h"
                    )
                jobs.append(
                    {
                        "name": job["TrainingJobName"],
                        "status": job["TrainingJobStatus"],
                        "creation_time": str(creation or ""),
                        "end_time": None,
                        "is_stuck": is_stuck,
                        "hours_running": hours_running,
                    }
                )

        stuck_count = sum(1 for j in jobs if j["is_stuck"])
        logger.info(f"✅ {len(jobs)} training jobs trouvés ({stuck_count} bloqués)")
        return jobs

    except ClientError as e:
        logger.error(f"❌ Erreur scan training jobs : {e}")
        return []


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
    logger.info("🔍 Démarrage du scan SageMaker...")

    notebooks = scan_notebooks()
    studio_apps = scan_studio_apps()
    endpoints = scan_endpoints()
    training_jobs = scan_training_jobs()

    running_notebooks = [n for n in notebooks if n["is_running"]]
    running_studio_apps = [a for a in studio_apps if a["is_running"]]
    running_endpoints = [e for e in endpoints if e["is_running"]]
    stuck_jobs = [j for j in training_jobs if j.get("is_stuck")]
    idle_studio_apps = [a for a in studio_apps if a.get("is_idle") and a["is_running"]]

    total_carbon_kg = sum(
        n.get("carbon_footprint_kg_month", 0) for n in notebooks if n["is_running"]
    )

    rapport = {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_notebooks": len(notebooks),
            "running_notebooks": len(running_notebooks),
            "total_studio_apps": len(studio_apps),
            "running_studio_apps": len(running_studio_apps),
            "idle_studio_apps": len(idle_studio_apps),
            "total_endpoints": len(endpoints),
            "running_endpoints": len(running_endpoints),
            "total_training_jobs": len(training_jobs),
            "stuck_training_jobs": len(stuck_jobs),
            "total_carbon_kg_month": round(total_carbon_kg, 2),
        },
        "notebooks": notebooks,
        "studio_apps": studio_apps,
        "endpoints": endpoints,
        "training_jobs": training_jobs,
    }

    logger.info(
        f"📊 Scan terminé : {len(running_notebooks)} notebooks, "
        f"{len(running_studio_apps)} Studio apps ({len(idle_studio_apps)} idle), "
        f"{len(running_endpoints)} endpoints, "
        f"{len(stuck_jobs)} training jobs bloqués, "
        f"🌱 {total_carbon_kg:.1f} kg CO₂/mois"
    )
    return rapport


if __name__ == "__main__":
    # Test local
    os.environ["AWS_REGION"] = "eu-west-1"
    result = run_discovery()
    import json

    print(json.dumps(result, indent=2, default=str))
