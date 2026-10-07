# Mise à jour mensuelle du dashboard (Trade Republic, Bourse, PEE, métaux précieux)

Toutes les commandes se lancent depuis WSL, à la racine du projet, avec le venv :

```bash
cd /mnt/c/Users/E090154/Documents/personal-notifier
source .venv/bin/activate
```

Ces scripts demandent une validation (téléphone ou code reçu) : on les lance **à la main, en local**, jamais dans GitHub Actions.

## Confidentialité : ce qui ne doit jamais être poussé sur GitHub

| Fichier / dossier | Contenu | Protection |
|---|---|---|
| `.env` | identifiants TR, Esalia, clé Google | ignoré par Git |
| `portfolio_imports/` | exports TR, ledger PEE,  positions initiales | ignoré par Git |
| `portfolio_imports/physical_metals.json` | achats d'or et d'argent physiques | couvert par `portfolio_imports/` |


## Variables du `.env`

```text
TR_PHONE_NUMBER=+33...
TR_PIN=....
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS='{...json sur une seule ligne...}'
ESALIA_LOGIN=...
ESALIA_PASSWORD=...
```

## 1. Trade Republic + Bourse (PEA / CTO)

```bash
python -m portfolio.trade_republic_fetch     # confirmer dans l'app TR (ou saisir le code)
python -m portfolio.sheet_bourse --dry-run   # vérifier les cellules prévues
python -m portfolio.sheet_bourse             # écrire dans « dashboard 2026 »
```

- La première commande écrit le CSV du jour et complète `portfolio_imports/trade_republic/transactions.json` (historique, jamais écrasé).
- `sheet_bourse` n'écrit que les mois ayant des opérations ; le relancer est sans risque.
- Parts détenues mais absentes de l'historique (transferts) : elles sont inscrites une fois dans `portfolio_imports/trade_republic/initial_positions.json` (mois, nombre, prix). Ce fichier est modifiable : corriger le mois ou le prix réels, relancer `sheet_bourse`.
- Si une note « colonne absente de l'onglet » apparaît, le mois n'existe pas dans l'onglet.


## 2. PEE (Société Générale / Esalia, fonds CGI)

À lancer depuis un **réseau personnel** : le proxy d'entreprise a un certificat auto-signé et fait échouer la connexion (la vérification SSL n'est jamais désactivée).

```bash
pip install -r requirements-esalia.txt                 # une seule fois
python -m portfolio.esalia_fetch                       # positions du jour (code reçu par mail ou SMS)
python -m portfolio.esalia_fetch --transactions        # historique des versements et abondements
python -m portfolio.sheet_pee --dry-run
python -m portfolio.sheet_pee
```

- `--transactions` ajoute les nouvelles transactions à `portfolio_imports/esalia/transactions.json` (ledger sans écrasement).
- `sheet_pee` remplit le bloc « CGI » : nombre de parts, valeur unitaire, abondement en négatif (donc le coût du bloc = ce que tu paies toi-même). Les versements d'avant 2026 sont ajoutés en constante dans les lignes « depuis le début ».
- Contrôle : le total de parts de `C886` doit être égal au nombre de parts affiché dans l'espace Esalia.
- Chaque connexion demande un nouveau code unique ; il n'est pas mémorisé.

## 3. Vérifications dans le Sheet

- Lignes « valo totale » et « Rendements % » des blocs mis à jour (Bourse, métaux, PEE CGI).
- Totaux patrimoine (Bourse, Or, Argent, PEE) cohérents avec les blocs.
- Cap Lendermarket à 30 € par investissement : `MAX_INVESTMENT_AMOUNT` dans `monitors/lendermarket_monitor.py`.
- L'onglet `dashboard 2026 (to delete)` est une sauvegarde : le supprimer une fois le dashboard validé.

## Dépannage rapide

| Symptôme | Cause probable | Action |
|---|---|---|
| Erreur de certificat SSL | proxy d'entreprise | relancer depuis un réseau personnel |
| `Aucun PEE trouvé` | session ou compte différent | relancer, vérifier `ESALIA_LOGIN` |
| Colonne de mois ignorée | mois absent de l'onglet | ajouter la colonne ou ignorer |
| Erreur Google 5xx temporaire | indisponibilité passagère | relancer la commande |
