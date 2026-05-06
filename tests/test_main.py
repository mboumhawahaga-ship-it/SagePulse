import json
import os
import sys

# Ajoute le dossier lambda au path pour pouvoir importer main.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
from main import MOCK_DATA, generate_recommendations, handler

# ─────────────────────────────────────────────
# TESTS : generate_recommendations()
# ─────────────────────────────────────────────


class TestGenerateRecommendations:
    def test_4_recommandations_avec_couts_nominaux(self):
        """Avec les coûts mock, on doit avoir 4 recommandations"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        assert len(recs) == 4, f"Attendu 4 recommandations, obtenu {len(recs)}"

    def test_economie_notebooks_75_pct(self):
        """Les notebooks doivent économiser 75%"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Notebooks")
        expected = round(MOCK_DATA["cost_by_resource"]["notebooks"] * 0.75, 2)
        assert (
            rec["savings"] == expected
        ), f"Attendu ${expected}, obtenu ${rec['savings']}"

    def test_economie_training_70_pct(self):
        """Le training doit économiser 70%"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Training")
        expected = round(MOCK_DATA["cost_by_resource"]["training"] * 0.70, 2)
        assert (
            rec["savings"] == expected
        ), f"Attendu ${expected}, obtenu ${rec['savings']}"

    def test_economie_endpoints_30_pct(self):
        """Les endpoints doivent économiser 30%"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        rec = next(r for r in recs if r["type"] == "Endpoints")
        expected = round(MOCK_DATA["cost_by_resource"]["endpoints"] * 0.30, 2)
        assert (
            rec["savings"] == expected
        ), f"Attendu ${expected}, obtenu ${rec['savings']}"

    def test_pas_de_recommandation_sous_les_seuils(self):
        """Sous les seuils, aucune recommandation ne doit être générée"""
        couts_faibles = {
            "notebooks": 5.00,  # < seuil 20$
            "training": 10.00,  # < seuil 50$
            "endpoints": 10.00,  # < seuil 50$
            "storage": 2.00,  # < seuil 10$
            "other": 0,
        }
        recs = generate_recommendations(couts_faibles)
        assert len(recs) == 0, f"Attendu 0 recommandations, obtenu {len(recs)}"

    def test_economies_positives(self):
        """Les économies doivent toujours être positives"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        for rec in recs:
            assert rec["savings"] > 0, f"{rec['type']} a une économie négative"

    def test_economies_inferieures_au_cout(self):
        """On ne peut pas économiser plus que ce qu'on dépense"""
        recs = generate_recommendations(MOCK_DATA["cost_by_resource"])
        for rec in recs:
            assert rec["savings"] < rec["cost"], (
                f"{rec['type']} : économie (${rec['savings']}) "
                f"supérieure au coût (${rec['cost']})"
            )


# ─────────────────────────────────────────────
# TESTS : handler()
# ─────────────────────────────────────────────


class TestHandler:
    def setup_method(self):
        """Active le mock avant chaque test"""
        os.environ["MOCK_MODE"] = "true"

    def teardown_method(self):
        """Désactive le mock après chaque test"""
        os.environ.pop("MOCK_MODE", None)

    def test_handler_retourne_200(self):
        """Le handler doit retourner un statusCode 200"""
        result = handler({}, None)
        assert result["statusCode"] == 200

    def test_handler_body_contient_champs_requis(self):
        """Le body doit contenir tous les champs attendus"""
        result = handler({}, None)
        body = json.loads(result["body"])
        for champ in [
            "success",
            "total_cost",
            "potential_savings",
            "savings_pct",
            "recommendations",
        ]:
            assert champ in body, f"Champ '{champ}' manquant dans le body"

    def test_handler_success_est_vrai(self):
        """Le champ success doit être True"""
        result = handler({}, None)
        body = json.loads(result["body"])
        assert body["success"] is True

    def test_handler_cout_total_correct(self):
        """Le coût total doit correspondre aux données mock"""
        result = handler({}, None)
        body = json.loads(result["body"])
        assert body["total_cost"] == MOCK_DATA["total_cost"]

    def test_handler_savings_pct_entre_0_et_100(self):
        """Le pourcentage d'économies doit être entre 0 et 100"""
        result = handler({}, None)
        body = json.loads(result["body"])
        assert (
            0 <= body["savings_pct"] <= 100
        ), f"savings_pct hors limites : {body['savings_pct']}"

    def test_handler_pas_de_division_par_zero(self):
        """Le handler ne doit pas crasher si total_cost = 0"""
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
