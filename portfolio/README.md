# Import des portefeuilles ETF

Ce module lit le relevé CSV le plus récent de Boursobank et de Trade Republic,
normalise les positions puis remplace le contenu de l'onglet Google Sheets
`Positions ETF`.

## Dossiers locaux

Créer cette arborescence à la racine du projet :

```text
portfolio_imports/
  boursobank/
  trade_republic/
```

Déposer chaque nouveau relevé dans le dossier du courtier correspondant. Le
dossier `portfolio_imports/` est ignoré par Git : les données patrimoniales ne
seront pas publiées sur GitHub.

Le script sélectionne le fichier `.csv` dont la date de modification est la
plus récente pour chaque courtier. Il accepte les séparateurs `;`, `,` et
tabulation, ainsi que les encodages UTF-8 et Windows-1252.

## Récupération automatique Trade Republic

Les identifiants se placent dans le fichier `.env` (ignoré par Git), jamais dans
le code ni dans un fichier suivi :

```text
TR_PHONE_NUMBER=+33600000000
TR_PIN=1234
```

```powershell
.\run_portfolio_import.ps1 -FetchTradeRepublic -DryRun
```

Depuis WSL / Linux :

```bash
python3 -m portfolio.trade_republic_fetch && python3 -m portfolio.csv_import --dry-run
```

La commande attend la confirmation de la connexion dans l'application Trade
Republic (ou demande le code de l'application d'authentification), écrit un CSV dans
`portfolio_imports/trade_republic/` puis lance l'import habituel. Le dossier
`trade_republic_scraper/` reste un outil séparé (export des transactions) ; son
`config.ini` est ignoré par Git.

## Colonnes reconnues

Les trois informations minimales sont :

- nom du produit : `Nom`, `Libellé`, `Instrument`, `Produit` ou `Name` ;
- quantité : `Quantité`, `Qté`, `Nombre`, `Shares` ou `Quantity` ;
- valorisation : `Valorisation`, `Valeur actuelle`, `Market value` ou `Value`.

Le script reconnaît aussi `ISIN`, `Cours`, `PRU`, `Montant investi`,
`Plus-value latente`, son pourcentage et la devise. Lorsqu'un PRU est présent,
les valeurs manquantes sont calculées automatiquement :

```text
montant investi = quantité × PRU
plus-value latente = valorisation - montant investi
plus-value % = plus-value latente / montant investi × 100
```

Si les intitulés réels diffèrent, la commande affiche les en-têtes détectés.
Il suffit alors d'ajouter leurs alias dans `FIELD_ALIASES`.

## Exécution

Contrôler les données sans toucher à Google Sheets :

```powershell
.\run_portfolio_import.ps1 -DryRun
```

Synchroniser l'onglet `Positions ETF` :

```powershell
.\run_portfolio_import.ps1
```

Les variables `GOOGLE_SHEET_ID` et `GOOGLE_CREDENTIALS` sont les mêmes que
pour les rapports existants. L'onglet est créé automatiquement s'il n'existe
pas. Il contient une ligne par ETF, puis les totaux par courtier et le total
global.

## Automatisation Windows

Une tâche du Planificateur de tâches Windows peut exécuter quotidiennement :

```text
powershell.exe -ExecutionPolicy Bypass -File "C:\chemin\personal-notifier\run_portfolio_import.ps1"
```

Cette tâche automatise la lecture et la mise à jour de la feuille. Le
téléchargement depuis Boursobank ou Trade Republic reste manuel tant qu'aucune
API officielle ne fournit les relevés. Il ne faut pas automatiser la saisie des
identifiants bancaires dans un navigateur depuis ce dépôt public.