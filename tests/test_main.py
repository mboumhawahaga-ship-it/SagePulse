import json
import os
import sys
from unittest import mock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
from main import MOCK_DATA, generate_recommendations, handler

# ─────────────────────────────────────────────
# TESTS : generate_recommendations()
# ─────────────────────────────────────────────


class TestGenerateRecommendations:
    def test_4_recommandations_avec_couts_nominaux(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        assert len(recs) == 4, f"Attendu 4 recommandations, obtenu {len(recs)}"

    def test_economie_notebooks_75_pct(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Notebooks")
        expected = round(MOCK_DATA["cost_by_resource"]["notebooks"] * 0.75, 2)
        assert rec["savings"] == expected

    def test_economie_training_70_pct(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Training")
        expected = round(MOCK_DATA["cost_by_resource"]["training"] * 0.70, 2)
        assert rec["savings"] == expected

    def test_economie_endpoints_30_pct(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Endpoints")
        expected = round(MOCK_DATA["cost_by_resource"]["endpoints"] * 0.30, 2)
        assert rec["savings"] == expected

    def test_pas_de_recommandation_sous_les_seuils(self):
        couts_faibles = {
            "notebooks": 5.00,
            "training": 10.00,
            "endpoints": 10.00,
            "storage": 2.00,
            "other": 0,
        }
        recs = generate_recommendations(couts_faibles)
        assert len(recs) == 0

    def test_economies_positives(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        for rec in recs:
            assert rec["savings"] > 0

    def test_economies_inferieures_au_cout(self):
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        for rec in recs:
            assert rec["savings"] < rec["cost"]

    def test_stuck_jobs_monte_priorite_critical(self):
        cost_by_resource = {
            "notebooks": 0.0,
            "training": 297.0,
            "endpoints": 0.0,
            "storage": 0.0,
            "other": 0.0,
        }
        discovery = {
            "notebooks": [],
            "endpoints": [],
            "training_jobs": [
                {"name": "job-1", "is_stuck": True, "hours_running": 30.0}
            ],
        }
        recs = generate_recommendations(cost_by_resource, discovery)
        tr = next(r for r in recs if r["type"] == "Training")
        assert tr["priority"] == "Critical"
        assert tr["idle_count"] == 1


# ─────────────────────────────────────────────
# TESTS : handler()
# ─────────────────────────────────────────────


class TestHandler:
    def setup_method(self):
        os.environ["MOCK_MODE"] = "true"

    def teardown_method(self):
        os.environ.pop("MOCK_MODE", None)
        os.environ.pop("COST_THRESHOLD_USD", None)

    def test_handler_retourne_200(self):
        result = handler({}, None)
        assert result["statusCode"] == 200

    def test_handler_body_contient_champs_requis(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        for champ in [
            "success",
            "total_cost",
            "potential_savings",
            "savings_pct",
            "recommendations",
        ]:
            assert champ in body

    def test_handler_success_est_vrai(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        assert body["success"] is True

    def test_handler_cout_total_correct(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        assert body["total_cost"] == MOCK_DATA["total_cost"]

    def test_handler_savings_pct_entre_0_et_100(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        assert 0 <= body["savings_pct"] <= 100

    def test_handler_pas_de_division_par_zero(self):
        import main

        original = main.MOCK_DATA.copy()
        main.MOCK_DATA = {
            "total_cost": 0,
            "cost_by_resource": {
                "notebooks": 0,
                "training": 0,
                "endpoints": 0,
                "storage": 0,
                "other": 0,
            },
        }
        try:
            result = handler({}, None)
            body = json.loads(result["body"])
            assert body["savings_pct"] == 0.0
        finally:
            main.MOCK_DATA = original

    def test_handler_carbon_kg_present(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        assert "carbon_kg_month" in body

    def test_handler_stuck_training_jobs_present(self):
        result = handler({}, None)
        body = json.loads(result["body"])
        assert "stuck_training_jobs" in body


# ─────────────────────────────────────────────
# TESTS : generate_markdown_report() carbon + stuck jobs
# ─────────────────────────────────────────────


class TestGenerateMarkdownReport:
    def test_section_carbon_presente(self):
        import main

        md = main.generate_markdown_report(
            100, 50, 50.0, [], "2026-04-09", carbon_kg=12.5
        )
        assert "Carbon" in md
        assert "12.5" in md

    def test_section_stuck_jobs_presente(self):
        import main

        md = main.generate_markdown_report(
            100, 50, 50.0, [], "2026-04-09", stuck_jobs=[("job-1", 30.5)]
        )
        assert "Stuck Training Jobs" in md
        assert "job-1" in md

    def test_sans_carbon_ni_stuck_jobs(self):
        import main

        md = main.generate_markdown_report(100, 50, 50.0, [], "2026-04-09")
        assert "Executive Summary" in md


# ─────────────────────────────────────────────
# TESTS : save_markdown_report()
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
# TESTS : get_real_costs() USAGE_TYPE
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

    def test_repartition_par_usage_type(self):
        import main

        ce = mock.MagicMock()
        ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["USE1-Notebook-Hours:ml.t3.medium"],
                            "Metrics": {"UnblendedCost": {"Amount": "200.0"}},
                        },
                        {
                            "Keys": ["USE1-Training-Hours:ml.p3.2xlarge"],
                            "Metrics": {"UnblendedCost": {"Amount": "300.0"}},
                        },
                        {
                            "Keys": ["USE1-Endpoint-Hours:ml.m5.xlarge"],
                            "Metrics": {"UnblendedCost": {"Amount": "150.0"}},
                        },
                    ]
                }
            ]
        }
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["cost_by_resource"]["notebooks"] == 200.0
        assert result["cost_by_resource"]["training"] == 300.0
        assert result["cost_by_resource"]["endpoints"] == 150.0
        assert result["total_cost"] == 650.0

    def test_fallback_si_erreur_client(self):
        import main

        ce = mock.MagicMock()
        ce.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "DataUnavailableException", "Message": ""}},
            "GetCostAndUsage",
        )
        with mock.patch("main.get_ce_client", return_value=ce):
            result = main.get_real_costs()
        assert result["total_cost"] == 0.0
        assert result["cost_explorer_available"] is False
