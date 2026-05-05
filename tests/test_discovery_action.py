"""
Test suite for discovery.py and action.py
Uses unittest.mock to patch boto3 clients.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

from botocore.exceptions import ClientError

os.environ["MOCK_MODE"] = "true"
os.environ["AWS_REGION"] = "eu-west-1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../lambda"))

from action import flag_idle_endpoint, stop_notebook
from action import handler as action_handler
from discovery import (
    calculate_carbon_footprint,
    run_discovery,
    scan_endpoints,
    scan_notebooks,
    scan_studio_apps,
    scan_training_jobs,
)

# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────


def make_sm_mock(notebooks=None, endpoints=None, apps=None, tags=None):
    """Crée un mock SageMaker client avec des données configurables."""
    sm = mock.MagicMock()

    def paginator_side_effect(name):
        p = mock.MagicMock()
        data = {
            "list_notebook_instances": {"NotebookInstances": notebooks or []},
            "list_endpoints": {"Endpoints": endpoints or []},
            "list_apps": {"Apps": apps or []},
        }
        if name in data:
            p.paginate.return_value = [data[name]]
        else:
            # list_training_jobs : paginate retourne liste vide par défaut
            p.paginate.return_value = [{}]
        return p

    sm.get_paginator.side_effect = paginator_side_effect
    sm.list_tags.return_value = {"Tags": tags or []}
    return sm


# ─────────────────────────────────────────────
# TESTS : scan_notebooks()
# ─────────────────────────────────────────────


class TestScanNotebooks:
    def test_retourne_liste_vide_si_aucun_notebook(self):
        sm = make_sm_mock()
        with (
            mock.patch("discovery.get_sagemaker_client", return_value=sm),
            mock.patch("discovery.get_instance_hourly_price", return_value=0.05),
        ):
            result = scan_notebooks()
        assert result == []

    def test_detecte_notebook_in_service(self):
        sm = make_sm_mock(
            notebooks=[
                {
                    "NotebookInstanceName": "test-nb",
                    "NotebookInstanceStatus": "InService",
                    "InstanceType": "ml.t3.medium",
                    "LastModifiedTime": "2026-01-01",
                }
            ]
        )
        with (
            mock.patch("discovery.get_sagemaker_client", return_value=sm),
            mock.patch("discovery.get_instance_hourly_price", return_value=0.05),
        ):
            result = scan_notebooks()
        assert len(result) == 1
        assert result[0]["name"] == "test-nb"
        assert result[0]["is_running"] is True
        assert result[0]["monthly_cost_estimate"] == round(0.05 * 730, 2)

    def test_notebook_stopped_is_running_false(self):
        sm = make_sm_mock(
            notebooks=[
                {
                    "NotebookInstanceName": "stopped-nb",
                    "NotebookInstanceStatus": "Stopped",
                    "InstanceType": "ml.t3.medium",
                    "LastModifiedTime": "2026-01-01",
                }
            ]
        )
        with (
            mock.patch("discovery.get_sagemaker_client", return_value=sm),
            mock.patch("discovery.get_instance_hourly_price", return_value=0.05),
        ):
            result = scan_notebooks()
        assert result[0]["is_running"] is False

    def test_erreur_client_retourne_liste_vide(self):
        sm = mock.MagicMock()
        sm.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "ListNotebookInstances",
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_notebooks()
        assert result == []


# ─────────────────────────────────────────────
# TESTS : scan_endpoints()
# ─────────────────────────────────────────────


class TestScanEndpoints:
    def test_retourne_liste_vide_si_aucun_endpoint(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_endpoints()
        assert result == []

    def test_detecte_endpoint_in_service(self):
        sm = make_sm_mock(
            endpoints=[
                {
                    "EndpointName": "test-ep",
                    "EndpointStatus": "InService",
                    "LastModifiedTime": "2026-01-01",
                }
            ]
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_endpoints()
        assert len(result) == 1
        assert result[0]["name"] == "test-ep"
        assert result[0]["is_running"] is True

    def test_erreur_client_retourne_liste_vide(self):
        sm = mock.MagicMock()
        sm.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "ListEndpoints"
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_endpoints()
        assert result == []


# ─────────────────────────────────────────────
# TESTS : scan_studio_apps() avec idle detection
# ─────────────────────────────────────────────


class TestScanStudioApps:
    def test_retourne_liste_vide_si_aucune_app(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert result == []

    def test_detecte_jupyter_lab_in_service(self):
        sm = make_sm_mock(
            apps=[
                {
                    "AppName": "default",
                    "AppType": "JupyterLab",
                    "Status": "InService",
                    "DomainId": "d-123",
                    "UserProfileName": "user1",
                }
            ]
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert len(result) == 1
        assert result[0]["is_running"] is True

    def test_kernel_gateway_idle_detection(self):
        sm = make_sm_mock(
            apps=[
                {
                    "AppName": "kernel-app",
                    "AppType": "KernelGateway",
                    "Status": "InService",
                    "DomainId": "d-123",
                    "UserProfileName": "user1",
                }
            ]
        )
        with (
            mock.patch("discovery.get_sagemaker_client", return_value=sm),
            mock.patch(
                "discovery.is_studio_app_idle",
                return_value={"is_idle": True, "avg_cpu": 0.5, "hours_checked": 24},
            ),
        ):
            result = scan_studio_apps()
        assert result[0]["is_idle"] is True
        assert result[0]["avg_cpu"] == 0.5

    def test_ignore_types_non_pertinents(self):
        sm = make_sm_mock(
            apps=[
                {
                    "AppName": "canvas",
                    "AppType": "Canvas",
                    "Status": "InService",
                    "DomainId": "d-123",
                }
            ]
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert result == []


# ─────────────────────────────────────────────
# TESTS : scan_training_jobs() avec stuck detection
# ─────────────────────────────────────────────


class TestScanTrainingJobs:
    def test_retourne_liste_vide_si_aucun_job(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_training_jobs()
        assert result == []

    def test_detecte_training_job_completed(self):
        creation = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sm = mock.MagicMock()

        def paginator_side_effect(name):
            p = mock.MagicMock()

            def paginate_side_effect(**kwargs):
                status = kwargs.get("StatusEquals", "")
                if status == "Completed":
                    return [
                        {
                            "TrainingJobSummaries": [
                                {
                                    "TrainingJobName": "job-1",
                                    "TrainingJobStatus": "Completed",
                                    "CreationTime": creation,
                                    "TrainingEndTime": datetime(
                                        2026, 1, 2, tzinfo=timezone.utc
                                    ),
                                }
                            ]
                        }
                    ]
                return [{}]

            p.paginate.side_effect = paginate_side_effect
            return p

        sm.get_paginator.side_effect = paginator_side_effect
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_training_jobs()
        assert len(result) == 1
        assert result[0]["name"] == "job-1"
        assert result[0]["is_stuck"] is False

    def test_detecte_job_bloque_in_progress(self):
        old_time = datetime.now(timezone.utc) - timedelta(hours=30)
        sm = mock.MagicMock()

        def paginator_side_effect(name):
            p = mock.MagicMock()

            def paginate_side_effect(**kwargs):
                status = kwargs.get("StatusEquals", "")
                if status == "Completed":
                    return [{}]
                if status == "InProgress":
                    return [
                        {
                            "TrainingJobSummaries": [
                                {
                                    "TrainingJobName": "stuck-job",
                                    "TrainingJobStatus": "InProgress",
                                    "CreationTime": old_time,
                                }
                            ]
                        }
                    ]
                return [{}]

            p.paginate.side_effect = paginate_side_effect
            return p

        sm.get_paginator.side_effect = paginator_side_effect
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_training_jobs(stuck_threshold_hours=24)

        stuck = [j for j in result if j["is_stuck"]]
        assert len(stuck) == 1
        assert stuck[0]["name"] == "stuck-job"
        assert stuck[0]["hours_running"] > 24


# ─────────────────────────────────────────────
# TESTS : calculate_carbon_footprint()
# ─────────────────────────────────────────────


class TestCarbonFootprint:
    def test_instance_connue(self):
        assert calculate_carbon_footprint("ml.t3.medium") == 2.5
        assert calculate_carbon_footprint("ml.p3.2xlarge") == 45.0

    def test_instance_inconnue_retourne_defaut(self):
        assert calculate_carbon_footprint("ml.unknown.xlarge") == 5.0


# ─────────────────────────────────────────────
# TESTS : run_discovery()
# ─────────────────────────────────────────────


class TestRunDiscovery:
    def test_structure_rapport_complet(self):
        with (
            mock.patch("discovery.scan_notebooks", return_value=[]),
            mock.patch("discovery.scan_studio_apps", return_value=[]),
            mock.patch("discovery.scan_endpoints", return_value=[]),
            mock.patch("discovery.scan_training_jobs", return_value=[]),
        ):
            result = run_discovery()

        assert "scan_date" in result
        assert "summary" in result
        assert "notebooks" in result
        assert "endpoints" in result
        assert "total_carbon_kg_month" in result["summary"]
        assert "stuck_training_jobs" in result["summary"]
        assert "idle_studio_apps" in result["summary"]


# ─────────────────────────────────────────────
# TESTS : action.py — stop_notebook()
# ─────────────────────────────────────────────


class TestStopNotebook:
    def test_stop_succes(self):
        sm = mock.MagicMock()
        sm.stop_notebook_instance.return_value = {}
        with mock.patch("action.get_sagemaker_client", return_value=sm):
            result = stop_notebook("test-nb")
        assert result["status"] == "success"
        assert result["action"] == "stop_notebook"
        assert result["resource"] == "test-nb"

    def test_stop_erreur_retourne_error(self):
        sm = mock.MagicMock()
        sm.stop_notebook_instance.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Already stopped"}},
            "StopNotebookInstance",
        )
        with mock.patch("action.get_sagemaker_client", return_value=sm):
            result = stop_notebook("test-nb")
        assert result["status"] == "error"
        assert "error" in result


# ─────────────────────────────────────────────
# TESTS : action.py — flag_idle_endpoint()
# ─────────────────────────────────────────────


class TestFlagIdleEndpoint:
    def test_notification_envoyee(self):
        sns = mock.MagicMock()
        sns.publish.return_value = {"MessageId": "msg-1"}
        with (
            mock.patch("action.boto3.client", return_value=sns),
            mock.patch("action.get_dynamodb_table", return_value=None),
            mock.patch.dict(
                os.environ, {"SNS_TOPIC_ARN": "arn:aws:sns:eu-west-1:123:topic"}
            ),
        ):
            result = flag_idle_endpoint("test-ep")
        assert result["status"] == "notified"
        assert result["action"] == "flag_idle_endpoint"
        assert result["resource"] == "test-ep"

    def test_erreur_sns_retourne_error(self):
        sns = mock.MagicMock()
        sns.publish.side_effect = ClientError(
            {"Error": {"Code": "AuthorizationError", "Message": ""}}, "Publish"
        )
        with (
            mock.patch("action.boto3.client", return_value=sns),
            mock.patch("action.get_dynamodb_table", return_value=None),
            mock.patch.dict(
                os.environ, {"SNS_TOPIC_ARN": "arn:aws:sns:eu-west-1:123:topic"}
            ),
        ):
            result = flag_idle_endpoint("test-ep")
        assert result["status"] == "error"


# ─────────────────────────────────────────────
# TESTS : action.py — handler()
# ─────────────────────────────────────────────


class TestActionHandler:
    def test_stop_notebook_via_handler(self):
        with (
            mock.patch(
                "action.stop_notebook",
                return_value={
                    "status": "success",
                    "action": "stop_notebook",
                    "resource": "nb-1",
                    "timestamp": "2026-01-01",
                },
            ),
            mock.patch("action.write_audit"),
        ):
            result = action_handler(
                {
                    "approved": True,
                    "idle_resources": {"notebooks": ["nb-1"], "endpoints": []},
                },
                None,
            )
        assert result["statusCode"] == 200

    def test_flag_endpoint_via_handler(self):
        with mock.patch(
            "action.flag_idle_endpoint",
            return_value={
                "status": "notified",
                "action": "flag_idle_endpoint",
                "resource": "ep-1",
                "timestamp": "2026-01-01",
            },
        ):
            result = action_handler(
                {
                    "approved": True,
                    "idle_resources": {"notebooks": [], "endpoints": ["ep-1"]},
                },
                None,
            )
        assert result["statusCode"] == 200

    def test_non_approuve_retourne_200_sans_action(self):
        result = action_handler(
            {
                "approved": False,
                "idle_resources": {"notebooks": ["nb-1"], "endpoints": []},
            },
            None,
        )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["approved"] is False
        assert body["actions"] == []

    def test_aucune_ressource_retourne_200(self):
        result = action_handler(
            {"approved": True, "idle_resources": {"notebooks": [], "endpoints": []}},
            None,
        )
        assert result["statusCode"] == 200


# ─────────────────────────────────────────────
# TESTS : get_instance_hourly_price()
# ─────────────────────────────────────────────


class TestGetInstanceHourlyPrice:
    def test_prix_trouve_via_pricing_api(self):
        from discovery import get_instance_hourly_price

        mock_product = json.dumps(
            {
                "terms": {
                    "OnDemand": {
                        "term1": {
                            "priceDimensions": {
                                "dim1": {"pricePerUnit": {"USD": "0.0464"}}
                            }
                        }
                    }
                }
            }
        )
        pricing = mock.MagicMock()
        pricing.get_products.return_value = {"PriceList": [mock_product]}
        with mock.patch("boto3.client", return_value=pricing):
            price = get_instance_hourly_price("ml.t3.medium", "eu-west-1")
        assert price == 0.0464

    def test_fallback_si_prix_vide(self):
        from discovery import get_instance_hourly_price

        pricing = mock.MagicMock()
        pricing.get_products.return_value = {"PriceList": []}
        with mock.patch("boto3.client", return_value=pricing):
            price = get_instance_hourly_price("ml.t3.medium", "eu-west-1")
        assert price == 0.05

    def test_fallback_si_erreur_api(self):
        from discovery import get_instance_hourly_price

        pricing = mock.MagicMock()
        pricing.get_products.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "GetProducts"
        )
        with mock.patch("boto3.client", return_value=pricing):
            price = get_instance_hourly_price("ml.t3.medium", "eu-west-1")
        assert price == 0.05


# ─────────────────────────────────────────────
# TESTS : is_notebook_idle()
# ─────────────────────────────────────────────


class TestIsNotebookIdle:
    def test_idle_si_cpu_sous_seuil(self):
        from discovery import is_notebook_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 2.0}, {"Average": 1.5}, {"Average": 3.0}]
        }
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_notebook_idle("test-nb")
        assert result["is_idle"] is True
        assert result["avg_cpu"] == 2.2

    def test_actif_si_cpu_au_dessus_seuil(self):
        from discovery import is_notebook_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 45.0}, {"Average": 60.0}]
        }
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_notebook_idle("test-nb")
        assert result["is_idle"] is False
        assert result["avg_cpu"] == 52.5

    def test_idle_si_aucune_metrique(self):
        from discovery import is_notebook_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_notebook_idle("test-nb")
        assert result["is_idle"] is True
        assert result["avg_cpu"] == 0.0

    def test_fallback_si_erreur_cloudwatch(self):
        from discovery import is_notebook_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "GetMetricStatistics"
        )
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_notebook_idle("test-nb")
        assert result["is_idle"] is False
        assert result["avg_cpu"] == -1.0


# ─────────────────────────────────────────────
# TESTS : generate_recommendations() avec idle detection
# ─────────────────────────────────────────────


class TestGenerateRecommendationsIdle:
    def test_priorite_critical_si_notebooks_idle(self):
        import main

        cost_by_resource = {
            "notebooks": 212.0,
            "training": 0.0,
            "endpoints": 0.0,
            "storage": 0.0,
            "other": 0.0,
        }
        discovery = {
            "notebooks": [
                {"name": "nb-1", "is_running": True, "is_idle": True, "avg_cpu": 1.2},
                {"name": "nb-2", "is_running": True, "is_idle": True, "avg_cpu": 0.5},
            ],
            "endpoints": [],
            "training_jobs": [],
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        nb_rec = next(r for r in recs if r["type"] == "Notebooks")
        assert nb_rec["priority"] == "Critical"
        assert nb_rec["idle_count"] == 2
        assert "idle" in nb_rec["issue"].lower()

    def test_priorite_high_si_aucun_notebook_idle(self):
        import main

        cost_by_resource = {
            "notebooks": 212.0,
            "training": 0.0,
            "endpoints": 0.0,
            "storage": 0.0,
        }
        discovery = {
            "notebooks": [
                {"name": "nb-1", "is_running": True, "is_idle": False, "avg_cpu": 45.0}
            ],
            "endpoints": [],
            "training_jobs": [],
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        nb_rec = next(r for r in recs if r["type"] == "Notebooks")
        assert nb_rec["priority"] == "High"

    def test_sans_discovery_comportement_normal(self):
        import main

        cost_by_resource = {
            "notebooks": 212.0,
            "training": 0.0,
            "endpoints": 0.0,
            "storage": 0.0,
        }
        recs = main.generate_recommendations(cost_by_resource, None)
        nb_rec = next(r for r in recs if r["type"] == "Notebooks")
        assert nb_rec["priority"] == "High"


# ─────────────────────────────────────────────
# TESTS : is_endpoint_idle()
# ─────────────────────────────────────────────


class TestIsEndpointIdle:
    def test_idle_si_zero_invocations(self):
        from discovery import is_endpoint_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_endpoint_idle("test-ep")
        assert result["is_idle"] is True
        assert result["total_invocations"] == 0

    def test_actif_si_invocations_presentes(self):
        from discovery import is_endpoint_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.return_value = {
            "Datapoints": [{"Sum": 150.0}, {"Sum": 320.0}]
        }
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_endpoint_idle("test-ep")
        assert result["is_idle"] is False
        assert result["total_invocations"] == 470

    def test_fallback_si_erreur_cloudwatch(self):
        from discovery import is_endpoint_idle

        cw = mock.MagicMock()
        cw.get_metric_statistics.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "GetMetricStatistics"
        )
        with mock.patch("discovery.get_cloudwatch_client", return_value=cw):
            result = is_endpoint_idle("test-ep")
        assert result["is_idle"] is False
        assert result["total_invocations"] == -1


# ─────────────────────────────────────────────
# TESTS : generate_recommendations() endpoints idle
# ─────────────────────────────────────────────


class TestGenerateRecommendationsEndpointIdle:
    def test_priorite_critical_si_endpoints_idle(self):
        import main

        cost_by_resource = {
            "notebooks": 0.0,
            "training": 0.0,
            "endpoints": 170.0,
            "storage": 0.0,
            "other": 0.0,
        }
        discovery = {
            "notebooks": [],
            "endpoints": [
                {
                    "name": "ep-1",
                    "is_running": True,
                    "is_idle": True,
                    "total_invocations": 0,
                }
            ],
            "training_jobs": [],
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        ep_rec = next(r for r in recs if r["type"] == "Endpoints")
        assert ep_rec["priority"] == "Critical"
        assert ep_rec["idle_count"] == 1
        assert "idle" in ep_rec["issue"].lower()

    def test_priorite_high_si_endpoint_actif(self):
        import main

        cost_by_resource = {
            "notebooks": 0.0,
            "training": 0.0,
            "endpoints": 170.0,
            "storage": 0.0,
        }
        discovery = {
            "notebooks": [],
            "endpoints": [
                {
                    "name": "ep-1",
                    "is_running": True,
                    "is_idle": False,
                    "total_invocations": 500,
                }
            ],
            "training_jobs": [],
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        ep_rec = next(r for r in recs if r["type"] == "Endpoints")
        assert ep_rec["priority"] == "High"
