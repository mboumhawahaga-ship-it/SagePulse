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


def delete_endpoint(endpoint_name):
    """
    Supprime un endpoint SageMaker inactif.

    Args:
        endpoint_name (str): Nom de l'endpoint à supprimer

    Returns:
        dict: Résultat de l'action avec statut et timestamp
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        get_sagemaker_client().delete_endpoint(EndpointName=endpoint_name)
        logger.info(f"✅ [{timestamp}] Endpoint supprimé : {endpoint_name}")
        return {
            "resource": endpoint_name,
            "action": "delete_endpoint",
            "status": "success",
            "timestamp": timestamp,
        }
    except ClientError as e:
        logger.error(
            f"❌ [{timestamp}] Échec suppression endpoint {endpoint_name} : {e}"
        )
        return {
            "resource": endpoint_name,
            "action": "delete_endpoint",
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
            results.append(stop_notebook(name))
        for name in endpoints:
            results.append(delete_endpoint(name))

        success_count = sum(1 for r in results if r["status"] == "success")
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
