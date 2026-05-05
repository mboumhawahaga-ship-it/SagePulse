# Action Lambda — exécutée uniquement après approbation humaine

import json
import os
from datetime import datetime, timezone

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="ml-cost-optimizer")


def get_sagemaker_client():
    return boto3.client(
        "sagemaker", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    )


def get_dynamodb_table():
    table_name = os.environ.get("AUDIT_TABLE")
    if not table_name:
        return None
    return boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "eu-west-1")
    ).Table(table_name)


def write_audit(resource, action, status, detail=None):
    """Enregistre une action dans la table DynamoDB d'audit."""
    table = get_dynamodb_table()
    if not table:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        table.put_item(
            Item={
                "resource": resource,
                "timestamp": now,
                "action": action,
                "status": status,
                "detail": detail or "",
            }
        )
    except ClientError as e:
        logger.warning(f"⚠️ Audit DynamoDB échoué pour {resource} : {e}")


def stop_notebook(notebook_name):
    """
    Arrête un notebook SageMaker (stop uniquement, pas de suppression).

    Args:
        notebook_name (str): Nom du notebook à arrêter

    Returns:
        dict: Résultat de l'action avec statut et timestamp
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        get_sagemaker_client().stop_notebook_instance(
            NotebookInstanceName=notebook_name
        )
        logger.info(f"✅ [{timestamp}] Notebook arrêté : {notebook_name}")
        return {
            "resource": notebook_name,
            "action": "stop_notebook",
            "status": "success",
            "timestamp": timestamp,
        }
    except ClientError as e:
        logger.error(f"❌ [{timestamp}] Échec arrêt notebook {notebook_name} : {e}")
        return {
            "resource": notebook_name,
            "action": "stop_notebook",
            "status": "error",
            "error": str(e),
            "timestamp": timestamp,
        }


def flag_idle_endpoint(endpoint_name):
    """
    Signale un endpoint idle via SNS au lieu de le supprimer.
    La suppression reste une décision humaine — les poids du modèle
    et la configuration endpoint sont préservés.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    detail = "Endpoint idle depuis 24h — aucune invocation détectée. Action manuelle requise."
    try:
        if sns_topic_arn:
            boto3.client(
                "sns", region_name=os.environ.get("AWS_REGION", "eu-west-1")
            ).publish(
                TopicArn=sns_topic_arn,
                Subject=f"[ML Cost Optimizer] Endpoint idle : {endpoint_name}",
                Message=(
                    f"L'endpoint '{endpoint_name}' n'a reçu aucune invocation depuis 24h.\n"
                    f"Coût estimé : en cours d'accumulation.\n\n"
                    f"Actions possibles :\n"
                    f"  - Supprimer manuellement si le modèle n'est plus nécessaire\n"
                    f"  - Configurer un auto-scaling avec minimum 0 instance\n"
                    f"  - Conserver si des pics de trafic sont attendus\n\n"
                    f"Timestamp : {timestamp}"
                ),
            )
        logger.info(
            f"✅ [{timestamp}] Endpoint idle signalé (non supprimé) : {endpoint_name}"
        )
        write_audit(endpoint_name, "flag_idle_endpoint", "notified", detail)
        return {
            "resource": endpoint_name,
            "action": "flag_idle_endpoint",
            "status": "notified",
            "timestamp": timestamp,
        }
    except ClientError as e:
        logger.error(
            f"❌ [{timestamp}] Échec notification endpoint {endpoint_name} : {e}"
        )
        write_audit(endpoint_name, "flag_idle_endpoint", "error", str(e))
        return {
            "resource": endpoint_name,
            "action": "flag_idle_endpoint",
            "status": "error",
            "error": str(e),
            "timestamp": timestamp,
        }


def handler(event, context):
    """
    Point d'entrée Lambda — exécute les actions uniquement si approuvé.

    Event attendu :
        {
            "approved": true | false,
            "idle_resources": {
                "notebooks": ["notebook-1"],
                "endpoints": ["endpoint-1"]
            }
        }
    """
    logger.info("🚀 Action Lambda - Démarrage")

    try:
        approved = event.get("approved", False)
        idle_resources = event.get("idle_resources", {})
        notebooks = idle_resources.get("notebooks", [])
        endpoints = idle_resources.get("endpoints", [])

        if not approved:
            logger.info("❌ Action refusée par l'humain — aucune ressource touchée")
            return {
                "statusCode": 200,
                "body": json.dumps({"success": True, "approved": False, "actions": []}),
            }

        if not notebooks and not endpoints:
            logger.info("ℹ️ Aucune ressource idle à traiter")
            return {
                "statusCode": 200,
                "body": json.dumps({"success": True, "approved": True, "actions": []}),
            }

        results = []
        for name in notebooks:
            result = stop_notebook(name)
            write_audit(name, "stop_notebook", result["status"])
            results.append(result)
        for name in endpoints:
            results.append(flag_idle_endpoint(name))

        success_count = sum(
            1 for r in results if r["status"] in ("success", "notified")
        )
        logger.info(f"✅ {success_count}/{len(results)} actions réussies")

        return {
            "statusCode": 200,
            "body": json.dumps({"success": True, "approved": True, "actions": results}),
        }

    except Exception as e:
        logger.error(f"❌ Erreur fatale : {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"success": False, "error": str(e)}),
        }
