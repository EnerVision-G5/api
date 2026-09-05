# Sécurité — API métier EnerVision

La chaîne de sécurité du projet est décrite par ADR-012 : Grype sur l'image
livrée, OWASP ZAP sur l'API en fonctionnement, SonarQube sur le code. Les deux
premiers tournent dans `ci.yml`.

Ils sont **bloquants** : une alerte fait échouer la CI, donc aussi la
livraison de l'image, qui rejoue la CI avant de construire. Une alerte qu'on
choisit de ne pas traiter tout de suite devient une **dérogation** : une entrée
dans `.grype.yaml` (Grype) ou `.zap/rules.tsv` (ZAP), et une section ci-dessous
avec sa raison et sa date de réexamen. Sans les deux, la CI reste rouge.

## Dérogations en cours

### starlette 0.49.3 — deux CVE de sévérité haute

| Champ | Valeur |
|---|---|
| Composant | `starlette 0.49.3` (dépendance transitive de FastAPI) |
| Alertes | `GHSA-82w8-qh3p-5jfq` (High, corrigé en 1.3.1), `GHSA-wqp7-x3pw-xc5r` (High, corrigé en 1.1.0) |
| Signalé par | Grype, job « Grype (CVE de l'image) » |
| Ouverte le | 3 septembre 2026 |
| Expire | **J10** — à réexaminer à cette échéance, sans report tacite |
| Décision | dérogation, la montée est techniquement impossible en l'état |
| Scan | bloquant, dérogation portée par `.grype.yaml` |

**Pourquoi la montée n'est pas appliquée.** `fastapi 0.121.2` déclare
`starlette<0.50.0,>=0.40.0`. Les versions qui corrigent ces deux CVE sont
`1.1.0` et `1.3.1`, hors de cette plage. La montée a été essayée, deux fois et
dans deux environnements indépendants : l'application ne s'importe même plus,
donc la spécification OpenAPI ne peut pas être régénérée.

```
$ pip install starlette==1.3.1
fastapi 0.121.2 requires starlette<0.50.0,>=0.40.0,
but you have starlette 1.3.1 which is incompatible.

$ python scripts/export_openapi.py
TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'
```

Le diff contre le contrat gelé n'est donc pas « non vide » : il est
**impossible à produire**.

**Ce que la lever demanderait.** Monter `starlette` exige de monter `fastapi`.
Or c'est FastAPI qui génère la spécification, et `requirements.txt` l'épingle
précisément pour cette raison : une montée, même en patch, peut modifier le
JSON produit (ordre, titres générés, forme des `anyOf`, version d'OpenAPI
émise) alors qu'aucun DTO n'a bougé. La levée passe donc par une **PR de
contrat** sur `enervision/docs/contracts`, portant à la fois le bump de
version et le contrat régénéré, relue par les trois consommateurs — la
procédure décrite dans `docs/contracts/README.md`.

**Exposition réelle, à peser lors du réexamen.** Les deux alertes portent sur
des composants de `starlette` que cette API n'utilise pas de la même manière
qu'une application exposant des fichiers statiques ou des sessions signées :
l'API sert du JSON derrière Traefik, sans `StaticFiles` ni `SessionMiddleware`
(vérifiable dans `app/main.py`). Cette lecture **atténue** le risque, elle ne
l'annule pas, et elle n'a pas été validée par une analyse d'exploitabilité.

### python-multipart 0.0.20 — trois CVE de sévérité haute

| Champ | Valeur |
|---|---|
| Composant | `python-multipart 0.0.20` (analyse des formulaires, dont `POST /auth/token`) |
| Alertes | `GHSA-wp53-j4wj-2cfg` (corrigé en 0.0.22), `GHSA-pp6c-gr5w-3c5g` (0.0.27), `GHSA-5rvq-cxj2-64vf` (0.0.30) |
| Signalé par | Grype |
| Ouverte le | 5 septembre 2026 |
| Expire | **13 septembre 2026** |
| Décision | dérogation courte : la montée n'a aucune incidence sur le contrat et se fait par une PR de dépendances dédiée |

### python 3.12.14 — trois CVE de l'interpréteur

| Champ | Valeur |
|---|---|
| Composant | interpréteur de l'image de base `python:3.12-slim` |
| Alertes | `CVE-2026-3644`, `CVE-2026-4224`, `CVE-2026-7210` |
| Signalé par | Grype |
| Ouverte le | 5 septembre 2026 |
| Expire | **13 septembre 2026** |
| Décision | dérogation : les correctifs publiés visent 3.13 et 3.14, aucune 3.12.x corrigée à ce jour ; reconstruire l'image dès qu'une base corrigée existe |

## Règles ZAP ignorées

`.zap/rules.tsv` liste les alertes que le scan ne compte pas, avec leur raison
en commentaire : en-têtes posés par le reverse proxy et absents d'un uvicorn
attaqué en direct, directives de cache sans objet pour du JSON, et le script
d'alerte sur code HTTP, qui signalerait chaque 404 rendu aux sondes de ZAP
ainsi que le 502 du relais vers la source de mesures, sans amont sur le
runner. Ce dernier choix a un coût : une erreur 500 non traitée n'apparaît
plus dans le verdict du scan. Elle reste dans `uvicorn.log`, publié avec le
rapport, et le scan du 4 septembre en montrait deux, sur
`GET /api/v1/simulations/spike?site_id=` contenant un octet nul, à corriger
côté API.

## Pourquoi bloquer

Un scan qui peut être rouge sans conséquence cesse d'être lu. Bloquer oblige à
trancher chaque alerte : corriger, ou dater une dérogation qu'on relira. Le
prix est qu'une CVE de l'image de base peut apparaître entre deux exécutions
sans qu'aucune ligne du dépôt ait changé ; la PR touchée l'inscrit alors dans
`.grype.yaml` avec sa date, et c'est cette date qui garantit qu'elle sera
traitée.

## Ce qui n'est pas une dérogation

- **`GET /api/v1/alerts` répond `501`.** Ce n'est pas un défaut de sécurité
  mais une fonctionnalité non livrée (ticket EV-17).
- **`AUTH_ENABLED=false`** rend les lectures anonymes. C'est un mode de
  développement, jamais déployé : le rôle Ansible `applications` ne le
  positionne pas et le défaut du code est `true`.

## Signaler une vulnérabilité

Ouvrir une issue sur le dépôt en la préfixant `SECURITY:`, sans y détailler
d'exploit. Pour une vulnérabilité exploitable en l'état, prévenir d'abord un
membre de l'équipe directement.
