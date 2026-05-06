# Glossaire — Les concepts du projet expliqués simplement

---

## Notebook
Ton cahier de brouillon interactif pour coder et tester rapidement. Tu écris du code, tu l'exécutes ligne par ligne, tu vois le résultat immédiatement. Problème : il tourne sur une machine AWS payante — si tu oublies de l'éteindre, le compteur tourne.

## Kernel
Le moteur (la machine AWS) qui fait tourner le notebook. C'est lui qui coûte de l'argent. Un notebook sans kernel actif ne coûte rien. Un kernel qui tourne sans que personne ne l'utilise — c'est exactement ce que SagePulse détecte.

---

## Handler
Le responsable de shift au McDonald's. Quand une commande arrive, c'est lui qui reçoit la demande et appelle les bonnes personnes dans le bon ordre. Il ne cuisine pas lui-même — il orchestre.

---

## Lambda
Un cuisinier qui dort jusqu'à ce qu'on l'appelle. Il se réveille, fait son truc, et se rendort. Tu paies uniquement le temps où il était réveillé. Pas de serveur à maintenir.

---

## Event
La commande qui arrive au handler. C'est un paquet d'informations : qui a déclenché la Lambda, avec quelles données, depuis quel service AWS.

---

## Context
La fiche de poste remise au handler au moment du déclenchement. Elle contient des infos pratiques : combien de temps il lui reste avant timeout, l'identifiant de l'exécution, le nom de la Lambda.

---

## Step Functions
Le chef de cuisine. Il connaît toute la recette dans l'ordre : d'abord scan, ensuite approbation humaine, ensuite action, ensuite notification. Si une étape échoue, il sait quoi faire (retry, abandon, alerte).

---

## EventBridge
Le réveil. Il déclenche le workflow tous les lundis à 8h sans que personne n'ait besoin d'appuyer sur un bouton.

---

## SNS (Simple Notification Service)
Le serveur qui apporte le plat à la table. Il prend un message et le livre aux abonnés — email, SMS, autre Lambda. Dans ce projet : il envoie le rapport par mail et gère l'approbation humaine.

---

## S3
Le classeur à archives. Chaque rapport généré (JSON + Markdown) est sauvegardé dedans avec versioning — on peut retrouver n'importe quel rapport passé.

---

## IAM (Identity and Access Management)
Le badge d'accès. Chaque service n'a accès qu'à ce dont il a besoin — la Lambda d'analyse ne peut pas supprimer des ressources, seule la Lambda d'action peut le faire.

---

## waitForTaskToken
La sonnette de la salle d'attente. Le workflow se met en pause et attend qu'un humain appuie sur "valider" avant de continuer. Rien ne s'exécute automatiquement sans accord.

---

## Terraform
Le plan d'architecte. Il décrit toute l'infrastructure en code (Lambdas, rôles IAM, S3, SNS...). Une seule commande déploie tout sur AWS, une autre détruit tout. Reproductible à l'infini.

---

## moto
Le simulateur de vol pour les tests. Il fait croire au code Python qu'il parle à de vrais services AWS — sans coûter un centime et sans toucher à rien en production.

---

## OIDC
Le badge visiteur temporaire pour GitHub Actions. Au lieu de laisser une clé AWS permanente dans GitHub (risque si elle fuit), on donne un accès temporaire qui expire après chaque déploiement.

---

## MCP (Model Context Protocol)
Le traducteur entre toi et AWS. Il permet de poser des questions en langage naturel ("quels notebooks tournent en ce moment ?") et obtenir une vraie réponse depuis AWS — sans écrire de code.

---

## Ressources SageMaker avancées
*Découvertes en explorant l'écosystème SageMaker utilisé en entreprise.*

### Studio Domains
L'environnement de travail partagé de toute l'équipe data. Chaque data scientist a son espace dedans — tu ouvres ton navigateur, tu tombes sur Studio, tu codes, tu entraînes, tu déploies. Le domaine lui-même a des coûts fixes qui tournent même quand personne ne travaille.

### Feature Store
La base de données centralisée des variables ML de l'entreprise. Au lieu que chaque équipe recalcule les mêmes données d'entrée, elles sont calculées une fois et partagées par tous les modèles. C'est du stockage qui coûte en permanence et qui grossit vite.

### Model Registry
Le catalogue officiel des modèles en production. Chaque modèle est versionné, documenté et validé avant de déployer. En entreprise c'est souvent obligatoire pour la conformité — il faut savoir quel modèle tourne, depuis quand, qui l'a approuvé. Lien direct avec l'EU AI Act.
