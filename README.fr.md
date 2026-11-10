# MuseOS

[English](README.md) · [简体中文](README.zh-CN.md) · Français

> Une idée entre, plusieurs modèles d'IA travaillent en parallèle, un seul résultat coordonné en sort.

MuseOS est un moteur d'orchestration multi-modèles qui tourne sur votre machine, avec **vos propres modèles**.
Chief vérifie l'objectif, découpe l'idée en tâches, les confie à différents modèles en parallèle, lance au besoin
une relecture croisée, puis fusionne une réponse qui liste les accords, les désaccords, les hypothèses, les
risques et les limites. Un **test à l'aveugle** est intégré : un seul modèle répond de son côté à la même
question, les deux réponses sont anonymes et vous votez, pour vérifier sur vos propres questions si le
multi-modèles vous sert vraiment.

La documentation technique complète (API, configuration, tests) est dans le [README.md](README.md) en anglais.
Ce fichier est le guide d'utilisation.

## Utilisation

**1. Installer et démarrer.** Il faut Python 3.12+ et [uv](https://docs.astral.sh/uv/). Aucune base de données à installer.

```bash
git clone <url-du-depot> museos && cd museos
```

```bash
uv sync
```

```bash
uv run museos serve
```

Ouvrez http://127.0.0.1:8000 . Sans configuration, MuseOS tourne en **mode démo** avec des modèles simulés : les réponses sont du texte factice.

**Connecter vos propres modèles :**

```bash
uv run museos setup
```

Lancez-le une fois par modèle. Il demande le type, lit la clé en saisie masquée, la vérifie quand le fournisseur le permet, puis écrit `providers.toml` et `.env`. Deux modèles différents ou plus donnent les meilleurs résultats.

| Vous avez | Choix dans setup | Remarques |
|---|---|---|
| Une clé API OpenAI, Anthropic, Google, DeepSeek ou xAI | ce fournisseur | Facturé à l'usage par le fournisseur |
| Une clé OpenRouter | OpenRouter | Une seule clé pour les modèles de nombreux fournisseurs |
| Qwen, Kimi, GLM, une passerelle d'entreprise, un Ollama local… | Any OpenAI-compatible endpoint | Vous donnez l'URL de base et l'identifiant du modèle |
| Un abonnement Claude Pro/Max | Claude subscription | Lance le CLI officiel Claude Code avec votre propre connexion. Usage personnel uniquement |
| Un autre outil en ligne de commande où vous êtes connecté | Another command-line tool | Vous donnez la commande ; elle lit la question sur l'entrée standard et affiche la réponse |

Vous saisissez vous-même l'identifiant du modèle, car il change souvent : copiez-le depuis la page des modèles du fournisseur. Un abonnement d'application comme ChatGPT Plus n'est pas une clé API.

**À propos des abonnements :** un abonnement personnel est réservé à votre propre usage. MuseOS lance l'outil du fournisseur, ne lit ni ne conserve votre connexion, envoie un seul appel à la fois et plafonne les appels par exécution et par jour. Ne placez pas un abonnement derrière un service utilisé par d'autres personnes : utilisez une clé API dans ce cas. Le respect des conditions du fournisseur relève de votre responsabilité.

**2. Envoyer une idée.** Écrivez-la en haut de la page et cliquez sur « Lancer ». Trois interrupteurs :

| Interrupteur | Effet | Quand l'utiliser |
|---|---|---|
| Me questionner avant de répartir | Chief reformule votre idée en un objectif et pose au plus 3 questions. Le travail commence après votre réponse ou si vous passez | Activé par défaut. Le plus utile quand l'idée est encore floue |
| Relecture croisée | Le résultat de chaque agent est contesté par un autre modèle | Activé par défaut. Désactivez pour gagner du temps et de l'argent |
| Test à l'aveugle | Un seul modèle répond de son côté à la même question. Les deux réponses s'affichent côte à côte, sans signature, et vous votez | Pour chaque idée réelle pendant la période de validation |

**3. Répondre aux questions de Chief.** Une note apparaît sur la page. Répondez en une ligne à ce que vous pouvez. Les champs vides reprennent les hypothèses listées sous les questions.

**4. Lire le résultat.** Commencez par le résumé, les points d'accord, les désaccords et les prochaines étapes. Les désaccords méritent le plus votre attention. La sortie de chaque agent, les hypothèses, les risques et les limites peuvent être dépliés.

**5. Voter au test à l'aveugle.** Une exécution en test à l'aveugle cache sa progression. Quand les deux côtés ont terminé, la page affiche A et B. Choisissez la plus utile ; c'est seulement après le vote que la page indique laquelle était la synthèse multi-modèles. La bande jaune tient le score : après 10 votes, MuseOS réussit avec 6 victoires. Sinon, il faut simplifier le système : plusieurs réponses, une seule fusion.

**Langues.** La page, les réponses des modèles et les notes du système existent en anglais, en français et en chinois simplifié. Utilisez le sélecteur `EN / FR / 中文` en haut : il fixe la langue de la page et celle de la prochaine exécution, quelle que soit la langue de l'idée. Les exécutions terminées gardent leur langue d'origine. Par l'API, envoyez `"language": "en" | "fr" | "zh"` ; en ligne de commande, utilisez `--lang`. Sans indication, les modèles suivent la langue de l'idée.

**Ligne de commande** (sans la page web) :

```bash
uv run museos run "votre idée" --clarify --cross-review --lang fr --budget 0.5 --out artifacts/idee.md
```

**Coût et limites :**
- Vous payez directement vos fournisseurs. Une exécution avec relecture croisée représente en général 8 à 12 appels ; avec de petits modèles, cela coûte quelques centimes. Chaque tâche affiche ses tokens et son coût.
- Le plafond de budget par exécution est de 2 $ par défaut. Il est imposé par le serveur, quelle que soit la valeur envoyée par la page.
- Les fournisseurs par abonnement sont limités par défaut à un appel à la fois, 6 par exécution et 40 par jour ; au-delà, le travail passe à un autre fournisseur.
- Le serveur ne répond qu'aux requêtes de cette machine et n'a pas de connexion. N'exposez pas le port à Internet.

## Licence

MuseOS est publié sous [GNU AGPL v3 ou ultérieure](LICENSE). Pour l'utiliser dans un produit ou un service sans ces obligations, une licence commerciale est disponible : ouvrez une issue intitulée « Commercial licence ».
