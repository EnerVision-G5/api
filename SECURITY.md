# Sécurité — API métier EnerVision

La chaîne de sécurité du projet est décrite par ADR-012 : Grype sur l'image
livrée, OWASP ZAP sur l'API en fonctionnement, SonarQube sur le code. Les deux
premiers tournent dans `ci.yml` et sont **bloquants** : une alerte fait échouer
la CI.

Ce document est le seul endroit où une alerte peut être écartée sciemment.
Écarter une alerte en posant `continue-on-error` sur un job la rendrait
invisible sans que personne ne l'ait décidé, et sans date de réexamen.

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
