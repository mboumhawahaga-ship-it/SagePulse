"""
Test suite for discovery.py and action.py
Uses unittest.mock to patch boto3 clients.
"""

import os
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ["MOCK_MODE"] = "true"
os.environ["AWS_REGION"] = "eu-west-1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../lambda"))

from action import delete_endpoint, handler as action_handler, stop_notebook
from discovery import (
    _build_eu_ai_act_compliance,
    _build_rgpd_compliance,
    calculate_carbon_footprint,
    check_eu_ai_act_compliance,
    check_rgpd_compliance,
    run_discovery,
    scan_endpoints,
    scan_notebooks,
    scan_studio_apps,
    scan_training_jobs,
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

def make_sm_mock(notebooks=None, endpoints=None, apps=None, jobs=None, tags=None):
    """Crée un mock SageMaker client avec des données configurables."""
    sm = mock.MagicMock()

    sm.get_paginator.side_effect = lambda name: _make_paginator(name, {
        "list_notebook_instances": {"NotebookInstances": notebooks or []},
        "list_endpoints": {"Endpoints": endpoints or []},
        "list_apps": {"Apps": apps or []},
        "list_training_jobs": {"TrainingJobSummaries": jobs or []},
    })

    sm.list_tags.return_value = {"Tags": tags or []}
    return sm


def _make_paginator(name, data_map):
    paginator = mock.MagicMock()
    key = data_map.get(name, {})
    paginator.paginate.return_value = [key]
    return paginator


# ─────────────────────────────────────────────
# TESTS : scan_notebooks()
# ─────────────────────────────────────────────

class TestScanNotebooks:
    def test_retourne_liste_vide_si_aucun_notebook(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_instance_hourly_price", return_value=0.05):
            result = scan_notebooks()
        assert result == []

    def test_detecte_notebook_in_service(self):
        sm = make_sm_mock(notebooks=[{
            "NotebookInstanceName": "test-nb",
            "NotebookInstanceStatus": "InService",
            "InstanceType": "ml.t3.medium",
            "LastModifiedTime": "2026-01-01",
        }])
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_instance_hourly_price", return_value=0.05):
            result = scan_notebooks()
        assert len(result) == 1
        assert result[0]["name"] == "test-nb"
        assert result[0]["is_running"] is True
        assert result[0]["monthly_cost_estimate"] == round(0.05 * 730, 2)

    def test_notebook_stopped_is_running_false(self):
        sm = make_sm_mock(notebooks=[{
            "NotebookInstanceName": "stopped-nb",
            "NotebookInstanceStatus": "Stopped",
            "InstanceType": "ml.t3.medium",
            "LastModifiedTime": "2026-01-01",
        }])
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_instance_hourly_price", return_value=0.05):
            result = scan_notebooks()
        assert result[0]["is_running"] is False

    def test_erreur_client_retourne_liste_vide(self):
        sm = mock.MagicMock()
        sm.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "ListNotebookInstances"
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
        sm = make_sm_mock(endpoints=[{
            "EndpointName": "test-ep",
            "EndpointStatus": "InService",
            "LastModifiedTime": "2026-01-01",
        }])
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
# TESTS : scan_studio_apps()
# ─────────────────────────────────────────────

class TestScanStudioApps:
    def test_retourne_liste_vide_si_aucune_app(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert result == []

    def test_detecte_jupyter_lab_in_service(self):
        sm = make_sm_mock(apps=[{
            "AppName": "default",
            "AppType": "JupyterLab",
            "Status": "InService",
            "DomainId": "d-123",
            "UserProfileName": "user1",
        }])
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert len(result) == 1
        assert result[0]["is_running"] is True

    def test_ignore_types_non_pertinents(self):
        sm = make_sm_mock(apps=[{
            "AppName": "canvas",
            "AppType": "Canvas",
            "Status": "InService",
            "DomainId": "d-123",
        }])
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_studio_apps()
        assert result == []


# ─────────────────────────────────────────────
# TESTS : scan_training_jobs()
# ─────────────────────────────────────────────

class TestScanTrainingJobs:
    def test_retourne_liste_vide_si_aucun_job(self):
        sm = make_sm_mock()
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_training_jobs()
        assert result == []

    def test_detecte_training_job_completed(self):
        sm = make_sm_mock(jobs=[{
            "TrainingJobName": "job-1",
            "TrainingJobStatus": "Completed",
            "CreationTime": "2026-01-01",
            "TrainingEndTime": "2026-01-02",
        }])
        with mock.patch("discovery.get_sagemaker_client", return_value=sm):
            result = scan_training_jobs()
        assert len(result) == 1
        assert result[0]["name"] == "job-1"


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
# TESTS : check_rgpd_compliance()
# ─────────────────────────────────────────────

class TestRGPDCompliance:
    def test_tags_complets_retourne_low(self):
        sm = mock.MagicMock()
        sm.list_tags.return_value = {"Tags": [
            {"Key": "owner", "Value": "team-ml"},
            {"Key": "data-classification", "Value": "internal"},
            {"Key": "expiration-date", "Value": "2027-01-01"},
        ]}
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_rgpd_compliance("test-nb", "notebook-instance")
        assert result["rgpd_risk"] == "Low"

    def test_tag_expiration_manquant_retourne_high(self):
        sm = mock.MagicMock()
        sm.list_tags.return_value = {"Tags": [
            {"Key": "owner", "Value": "team-ml"},
            {"Key": "data-classification", "Value": "internal"},
        ]}
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_rgpd_compliance("test-nb", "notebook-instance")
        assert result["rgpd_risk"] == "High"

    def test_erreur_client_retourne_unknown(self):
        sm = mock.MagicMock()
        sm.list_tags.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "ListTags"
        )
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_rgpd_compliance("test-nb", "notebook-instance")
        assert result["rgpd_risk"] == "Unknown"


# ─────────────────────────────────────────────
# TESTS : check_eu_ai_act_compliance()
# ─────────────────────────────────────────────

class TestEUAIActCompliance:
    def test_endpoint_conforme(self):
        sm = mock.MagicMock()
        sm.list_tags.return_value = {"Tags": [
            {"Key": "ai-risk-level", "Value": "high"},
            {"Key": "human-oversight", "Value": "enabled"},
            {"Key": "model-purpose", "Value": "fraud-detection"},
            {"Key": "conformity-assessment", "Value": "done"},
        ]}
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_eu_ai_act_compliance("test-ep")
        assert result["compliant"] is True
        assert result["ai_risk_level"] == "high"

    def test_high_risk_sans_human_oversight_non_conforme(self):
        sm = mock.MagicMock()
        sm.list_tags.return_value = {"Tags": [
            {"Key": "ai-risk-level", "Value": "high"},
            {"Key": "model-purpose", "Value": "credit-scoring"},
        ]}
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_eu_ai_act_compliance("test-ep")
        assert result["compliant"] is False
        assert any("Art. 14" in a for a in result["alerts"])

    def test_tag_ai_risk_manquant(self):
        sm = mock.MagicMock()
        sm.list_tags.return_value = {"Tags": []}
        with mock.patch("discovery.get_sagemaker_client", return_value=sm), \
             mock.patch("discovery.get_account_id", return_value="123456789"):
            result = check_eu_ai_act_compliance("test-ep")
        assert any("Art. 9" in a for a in result["alerts"])

    def test_global_status_non_compliant(self):
        endpoints = [{"name": "ep-1"}]
        with mock.patch("discovery.check_eu_ai_act_compliance", return_value={
            "endpoint": "ep-1", "ai_risk_level": "high",
            "human_oversight": "disabled", "alerts": ["[Art. 14] ..."], "compliant": False
        }):
            result = _build_eu_ai_act_compliance(endpoints)
        assert result["global_status"] == "Non-Compliant"

    def test_global_status_na_si_pas_dendpoints(self):
        result = _build_eu_ai_act_compliance([])
        assert result["global_status"] == "N/A"


# ─────────────────────────────────────────────
# TESTS : run_discovery()
# ─────────────────────────────────────────────

class TestRunDiscovery:
    def test_structure_rapport_complet(self):
        with mock.patch("discovery.scan_notebooks", return_value=[]), \
             mock.patch("discovery.scan_studio_apps", return_value=[]), \
             mock.patch("discovery.scan_endpoints", return_value=[]), \
             mock.patch("discovery.scan_training_jobs", return_value=[]), \
             mock.patch("discovery._build_rgpd_compliance", return_value={"global_risk": "Low"}), \
             mock.patch("discovery._build_eu_ai_act_compliance", return_value={"global_status": "N/A"}):
            result = run_discovery()

        assert "scan_date" in result
        assert "summary" in result
        assert "notebooks" in result
        assert "endpoints" in result
        assert "rgpd_compliance" in result
        assert "eu_ai_act_compliance" in result


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
            "StopNotebookInstance"
        )
        with mock.patch("action.get_sagemaker_client", return_value=sm):
            result = stop_notebook("test-nb")
        assert result["status"] == "error"
        assert "error" in result


# ─────────────────────────────────────────────
# TESTS : action.py — delete_endpoint()
# ─────────────────────────────────────────────

class TestDeleteEndpoint:
    def test_delete_succes(self):
        sm = mock.MagicMock()
        sm.delete_endpoint.return_value = {}
        with mock.patch("action.get_sagemaker_client", return_value=sm):
            result = delete_endpoint("test-ep")
        assert result["status"] == "success"
        assert result["action"] == "delete_endpoint"

    def test_delete_erreur_retourne_error(self):
        sm = mock.MagicMock()
        sm.delete_endpoint.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFound", "Message": "Not found"}},
            "DeleteEndpoint"
        )
        with mock.patch("action.get_sagemaker_client", return_value=sm):
            result = delete_endpoint("test-ep")
        assert result["status"] == "error"


# ─────────────────────────────────────────────
# TESTS : action.py — handler()
# ─────────────────────────────────────────────

class TestActionHandler:
    def test_stop_notebook_via_handler(self):
        with mock.patch("action.stop_notebook", return_value={"status": "success", "action": "stop_notebook", "resource": "nb-1", "timestamp": "2026-01-01"}):
            result = action_handler({"action_type": "stop_notebook", "resource_name": "nb-1"}, None)
        assert result["statusCode"] == 200

    def test_delete_endpoint_via_handler(self):
        with mock.patch("action.delete_endpoint", return_value={"status": "success", "action": "delete_endpoint", "resource": "ep-1", "timestamp": "2026-01-01"}):
            result = action_handler({"action_type": "delete_endpoint", "resource_name": "ep-1"}, None)
        assert result["statusCode"] == 200

    def test_action_type_inconnu_retourne_500(self):
        result = action_handler({"action_type": "unknown_action", "resource_name": "ep-1"}, None)
        assert result["statusCode"] == 500

    def test_champs_manquants_retourne_500(self):
        result = action_handler({}, None)
        assert result["statusCode"] == 500


# ─────────────────────────────────────────────
# TESTS : get_instance_hourly_price()
# ─────────────────────────────────────────────

class TestGetInstanceHourlyPrice:
    def test_prix_trouve_via_pricing_api(self):
        import json
        from discovery import get_instance_hourly_price

        mock_product = json.dumps({
            "terms": {
                "OnDemand": {
                    "term1": {
                        "priceDimensions": {
                            "dim1": {"pricePerUnit": {"USD": "0.0464"}}
                        }
                    }
                }
            }
        })

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
        assert price == 0.05  # fallback default_prices

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
# TESTS : _build_rgpd_compliance()
# ─────────────────────────────────────────────

class TestBuildRGPDCompliance:
    def test_global_risk_high_si_un_high(self):
        with mock.patch("discovery.check_rgpd_compliance", side_effect=[
            {"resource": "nb-1", "type": "notebook-instance", "rgpd_risk": "High", "alerts": []},
            {"resource": "ep-1", "type": "endpoint", "rgpd_risk": "Low", "alerts": []},
        ]):
            result = _build_rgpd_compliance(
                [{"name": "nb-1"}], [{"name": "ep-1"}]
            )
        assert result["global_risk"] == "High"

    def test_global_risk_low_si_tout_conforme(self):
        with mock.patch("discovery.check_rgpd_compliance", return_value={
            "resource": "nb-1", "type": "notebook-instance", "rgpd_risk": "Low", "alerts": []
        }):
            result = _build_rgpd_compliance([{"name": "nb-1"}], [])
        assert result["global_risk"] == "Low"


# ─────────────────────────────────────────────
# TESTS : main.py — get_real_costs()
# ─────────────────────────────────────────────

class TestGetRealCosts:
    def test_fallback_si_resultats_vides(self):
        import main
        ce = mock.MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["total_cost"] == 0.0
        assert result["cost_by_resource"]["notebooks"] == 0.0

    def test_fallback_si_cout_zero(self):
        import main
        ce = mock.MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": [
            {"Groups": [{"Metrics": {"UnblendedCost": {"Amount": "0"}}}]}
        ]}
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["total_cost"] == 0.0

    def test_retourne_vrais_couts(self):
        import main
        ce = mock.MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": [
            {"Groups": [{"Metrics": {"UnblendedCost": {"Amount": "1000.0"}}}]}
        ]}
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["total_cost"] == 1000.0
        assert "notebooks" in result["cost_by_resource"]

    def test_fallback_si_erreur_client(self):
        import main
        ce = mock.MagicMock()
        ce.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "DataUnavailableException", "Message": ""}},
            "GetCostAndUsage"
        )
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["total_cost"] == 0.0
        assert result["cost_explorer_available"] is False


# ─────────────────────────────────────────────
# TESTS : main.py — save_markdown_report()
# ─────────────────────────────────────────────

class TestSaveMarkdownReport:
    def test_sauvegarde_et_retourne_url(self):
        import main
        s3 = mock.MagicMock()
        s3.put_object.return_value = {}
        with mock.patch("main.get_s3_client", return_value=s3):
            url = main.save_markdown_report("test-bucket", "# Report", "2026-04-09")
        assert url == "s3://test-bucket/reports/report_2026-04-09.md"
        s3.put_object.assert_called_once()

    def test_leve_exception_si_s3_echoue(self):
        import main
        s3 = mock.MagicMock()
        s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "PutObject"
        )
        with mock.patch("main.get_s3_client", return_value=s3):
            with pytest.raises(ClientError):
                main.save_markdown_report("test-bucket", "# Report", "2026-04-09")


# ─────────────────────────────────────────────
# TESTS : main.py — generate_markdown_report() avec RGPD + EU AI Act
# ─────────────────────────────────────────────

class TestGenerateMarkdownReportCompliance:
    def test_section_rgpd_presente(self):
        import main
        recs = []
        rgpd_data = {
            "global_risk": "High",
            "notebooks": [{"resource": "nb-1", "type": "notebook-instance", "rgpd_risk": "High", "alerts": ["Tag manquant"]}],
            "endpoints": [],
        }
        md = main.generate_markdown_report(100, 50, 50.0, recs, "2026-04-09", rgpd_data=rgpd_data)
        assert "GDPR Compliance" in md
        assert "High" in md

    def test_section_eu_ai_act_presente(self):
        import main
        recs = []
        eu_data = {
            "global_status": "Non-Compliant",
            "high_risk_count": 1,
            "endpoints": [{"endpoint": "ep-1", "ai_risk_level": "high", "human_oversight": "disabled", "alerts": ["[Art. 14] ..."], "compliant": False}],
        }
        md = main.generate_markdown_report(100, 50, 50.0, recs, "2026-04-09", eu_ai_act_data=eu_data)
        assert "EU AI Act" in md
        assert "35M" in md
        assert "Non-Compliant" in md


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
            "notebooks": 212.0, "training": 0.0,
            "endpoints": 0.0, "storage": 0.0, "other": 0.0,
        }
        discovery = {
            "notebooks": [
                {"name": "nb-1", "is_running": True, "is_idle": True, "avg_cpu": 1.2},
                {"name": "nb-2", "is_running": True, "is_idle": True, "avg_cpu": 0.5},
            ]
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        nb_rec = next(r for r in recs if r["type"] == "Notebooks")
        assert nb_rec["priority"] == "Critical"
        assert nb_rec["idle_count"] == 2
        assert "idle" in nb_rec["issue"].lower()

    def test_priorite_high_si_aucun_notebook_idle(self):
        import main
        cost_by_resource = {"notebooks": 212.0, "training": 0.0, "endpoints": 0.0, "storage": 0.0}
        discovery = {
            "notebooks": [
                {"name": "nb-1", "is_running": True, "is_idle": False, "avg_cpu": 45.0},
            ]
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        nb_rec = next(r for r in recs if r["type"] == "Notebooks")
        assert nb_rec["priority"] == "High"

    def test_sans_discovery_comportement_normal(self):
        import main
        cost_by_resource = {"notebooks": 212.0, "training": 0.0, "endpoints": 0.0, "storage": 0.0}
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
            "notebooks": 0.0, "training": 0.0,
            "endpoints": 170.0, "storage": 0.0, "other": 0.0,
        }
        discovery = {
            "notebooks": [],
            "endpoints": [
                {"name": "ep-1", "is_running": True, "is_idle": True, "total_invocations": 0},
            ]
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        ep_rec = next(r for r in recs if r["type"] == "Endpoints")
        assert ep_rec["priority"] == "Critical"
        assert ep_rec["idle_count"] == 1
        assert "idle" in ep_rec["issue"].lower()

    def test_priorite_high_si_endpoint_actif(self):
        import main
        cost_by_resource = {"notebooks": 0.0, "training": 0.0, "endpoints": 170.0, "storage": 0.0}
        discovery = {
            "notebooks": [],
            "endpoints": [
                {"name": "ep-1", "is_running": True, "is_idle": False, "total_invocations": 500},
            ]
        }
        recs = main.generate_recommendations(cost_by_resource, discovery)
        ep_rec = next(r for r in recs if r["type"] == "Endpoints")
        assert ep_rec["priority"] == "High"
