# Journal de bord

---

## Session — Avril 2026

### Ce qui fonctionne

- Scanner SageMaker : notebooks, Studio apps, endpoints, training jobs
- Rapports JSON + Markdown sauvegardés dans S3
- Notification email reçue dans Gmail
- 19/19 tests passent
- CI/CD GitHub Actions propre
- Infrastructure Terraform avec state S3 remote

---

### Erreurs rencontrées et solutions

**1. `POWERTOOLS_LOG_LEVEL=WARNING ` — espace parasite Windows**

```
ValueError: Unknown level: 'WARNING '
```

Sur Windows, `set VAR=valeur` ajoute parfois un espace invisible. Toujours utiliser `set "VAR=valeur"` avec les guillemets. Lambda Powertools est strict sur le format.

---

**2. Emojis dans les logs — encoding Windows cp1252**

```
UnicodeEncodeError: 'charmap' codec can't encode character
```

Le terminal Windows utilise cp1252 qui ne supporte pas les emojis. En prod Lambda c'est UTF-8 — aucun problème. En local : `set "PYTHONIOENCODING=utf-8"`.

---

**3. `detect-secrets` en boucle infinie**

Le hook recalculait les numéros de ligne à chaque commit, modifiait le baseline, ce qui déclenchait un nouveau cycle. Solution : `git commit --no-verify` uniquement pour le fichier baseline, jamais pour le code.

---

**4. ARN RGPD avec triple `:::`**

```python
# ❌
f"arn:aws:sagemaker:{region}:::{resource_type}/{resource_name}"
# ✅
f"arn:aws:sagemaker:{region}:{account_id}:{resource_type}/{resource_name}"
```

L'Account ID était absent. Tous les appels `list_tags()` échouaient silencieusement — la vérification RGPD ne fonctionnait jamais sans que personne ne s'en rende compte.

---

**5. `aws-lambda-powertools==3.9.1` — version inexistante**

Version épinglée qui n'existe pas sur PyPI. La liste saute de 3.9.0 à 3.10.0 directement. Corrigé en `3.27.0`.

---

**6. Test SNS qui échouait à cause de S3**

```
AssertionError: Lambda should return 200 even with SNS error
assert 500 == 200
```

Le test mockait SNS mais pas S3. Le handler faisait un vrai appel S3 → `AccessDenied` → crash avant d'atteindre le code SNS. Le test testait la mauvaise chose.

---

**7. Notebook SageMaker qui échouait au démarrage**

Le rôle Lambda n'a pas `sagemaker.amazonaws.com` comme principal — il ne peut pas être utilisé par SageMaker. Il faut un rôle dédié avec le bon `AssumeRolePolicy`.

---

**8. Gmail qui désabonnait automatiquement SNS**

Gmail détecte le mot "unsubscribe" dans les mails AWS et clique automatiquement dessus. Solution : marquer `no-reply@sns.amazonaws.com` comme contact, et chercher le mail de confirmation dans les spams.

---

### Leçon générale

La majorité des erreurs venaient de la différence entre l'environnement local Windows et Lambda — encoding, variables d'environnement, permissions IAM. En prod sur Lambda, aucun de ces problèmes n'existe. C'est pour ça que les tests avec moto sont importants : ils simulent Lambda sans dépendre de l'environnement local.

---

## À faire

- [x] **RGPD visible** — implémenté puis retiré (trop complexe pour le scope actuel)
- [x] **EU AI Act** — implémenté puis retiré (trop complexe pour le scope actuel)
- [x] **Notebooks idle** — CloudWatch CPU < 5% sur 24h → recommandation Critical
- [x] **Endpoints idle** — CloudWatch Invocations = 0 sur 24h → recommandation Critical
- [x] **Tests discovery + action** — 63 tests, coverage 87% (action 98%, discovery 86%, main 85%)
- [x] **README humain** — ajout pourquoi ce projet, problèmes résolus, choix d'architecture
- [ ] **Cost Explorer réel** — remplacer les pourcentages fixes par de vraies données (attendre 24h d'activation)
- [ ] **CO2 dans les rapports** — les données sont collectées mais jamais affichées

---

## V2 — Feedback utilisateurs MLOps

Deux MLOps contactés ont validé le besoin et demandé :
- **Notifications en temps réel** — alertes dès qu'une ressource dépasse un seuil de coût
- **Agir après le seuil** — pouvoir couper directement depuis la notification

Pourquoi l'hebdomadaire ne suffit pas : SageMaker facture à l'heure. Un notebook GPU (ml.p3.2xlarge = 3,06$/h) idle une nuit = ~75$ perdus avant le prochain scan.

### Ce qui change dans la v2

| Actuel (v1) | Cible (v2) |
|---|---|
| Scan 1x par semaine | Scan toutes les 4h |
| 1 Lambda fait scan + calcul + rapport | 3 Lambdas séparées |
| Rapport uniquement par email | Alerte immédiate SNS si seuil dépassé |
| Pas de DynamoDB | DynamoDB audit trail |

### État du code v1 (base de départ)
- `discovery.py` — 405 lignes : scan + idle detection + RGPD + EU AI Act
- `main.py` — 759 lignes : calcul coûts + rapport + S3 + SNS
- `action.py` — 155 lignes : stop notebook / flag endpoint
- 70 tests, coverage 88% — ne pas casser

### Plan v2
- [ ] Séparer en 3 Lambdas : `cost_scanner` / `cost_calculator` / `cost_action`
- [ ] Passer EventBridge de hebdomadaire à toutes les 4h
- [ ] Ajouter alerte SNS immédiate si seuil dépassé (configurable)
- [ ] Ajouter DynamoDB pour l'historique des scans et actions

---

## Décisions — ce qu'on a refusé et pourquoi

| Proposition | Décision | Raison |
|---|---|---|
| Cache Pricing API DynamoDB | ❌ Refusé | 3 appels par scan — over-engineering sans valeur réelle |
| DLQ SQS + retry | ❌ Refusé | Retry exponentiel déjà dans Step Functions — doubler c'est de la complexité pour rien |
| TypedDict / dataclass | ❌ Refusé | Code lisible, projet solo — aucun gain concret maintenant |
| Découper main.py | ❌ Refusé | 550 lignes raisonnables — découper casse 75 tests pour zéro gain |
| RGPD + EU AI Act | ❌ Retiré | Trop complexe pour le scope actuel — à réintégrer en v3 si besoin |
