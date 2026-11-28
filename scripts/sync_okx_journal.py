"""Synchronise OKX (bonus Earn, intérêts de staking, achats de crypto) dans l'onglet Google Sheet
"Journal crypto".

Opérations importées (clé API OKX en lecture seule) :
- Bonus : crédits "Received" (type 48) du compte de financement, p. ex. le bonus dépôt de 8 % versé
  en USDC toutes les deux semaines -> ligne "Bonus" ;
- Intérêts : gains du Liquid Staking (BETH, comptés comme de l'ETH), agrégés par mois civil terminé
  -> ligne "Intérêt" datée du dernier jour du mois ; intérêts Simple Earn d'une position soldée
  (rachats - souscriptions) -> ligne "Intérêt" datée du rachat ;
- Achats : ordres spot d'achat regroupés par ordre ; paire en EUR -> "Achat €", paire en USDC -> "Swap".
  Les ventes ne sont pas importées (un avertissement est journalisé).

Chaque ligne est insérée à la bonne place chronologique, comme l'écran "Journaux" de la web app
(JournalForm.js) : seules les colonnes de saisie A:J sont décalées, les formules des colonnes de
droite sont pré-remplies sur toute la hauteur de la feuille. La valeur en € est la quantité
multipliée par la clôture quotidienne OKX de la paire <actif>-EUR.

Idempotent : chaque ligne créée porte un marqueur "[OKX:...]" dans le commentaire ; les lignes
saisies à la main sont reconnues par (date, type, actif reçu, quantité reçue).

Lancement : python -m scripts.sync_okx_journal
Variables : OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE (+ OKX_BASE_URL), GOOGLE_SHEET_ID,
GOOGLE_CREDENTIALS ; LOOKBACK_DAYS (défaut 120) ; DRY_RUN=1 pour ne rien écrire.
"""
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from shared.google_sheet import _call_with_retry, get_worksheet_by_name
from shared.okx import OkxClient, OkxError

log = logging.getLogger(__name__)

SHEET_NAME = "Journal crypto"
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or 120)
TIMEZONE = ZoneInfo("Europe/Paris")

INPUT_COLS = 10  # colonnes de saisie A:J (J = "Capital de référence (€)")
COL_DATE, COL_TYPE, COL_CEDE, COL_QTE_OUT, COL_RECU, COL_QTE_IN, COL_MONTANT, COL_FRAIS, COL_COMMENT, COL_CAPITAL = range(INPUT_COLS)
EXCEL_EPOCH = date(1899, 12, 30)

SUPPORTED_ASSETS = {"BTC", "ETH", "USDC"}  # actifs suivis par le bloc "Portefeuille actuel"
BUY_QUOTES = {"EUR": "Achat €", "USDC": "Swap"}
STAKING_ASSETS = {"BETH": "ETH"}  # le BETH est valorisé comme de l'ETH (1 BETH = 1 ETH staké)
BONUS_BILL_TYPE = "48"
STAKING_BILL_TYPE = "139"
EARN_SUBSCRIPTION_BILL_TYPE = "75"
EARN_REDEMPTION_BILL_TYPE = "76"
TAG_RE = re.compile(r"\[OKX:([^\]]+)\]")


@dataclass
class Operation:
    day: date
    ts: int  # ms, départage les opérations du même jour
    type: str
    cede: str
    qte_out: float | None
    recu: str
    qte_in: float
    montant: float
    frais: float | None
    comment: str
    tag: str
    capital_ref: float | None = None

    @property
    def serial(self) -> int:
        return (self.day - EXCEL_EPOCH).days

    def key(self) -> tuple:
        return (self.serial, self.type, self.recu, round(self.qte_in, 6))

    def row(self) -> list:
        def cell(value):
            return "" if value is None else value

        return [self.serial, self.type, self.cede, cell(self.qte_out), self.recu, self.qte_in, self.montant,
                cell(self.frais), f"{self.comment} [OKX:{self.tag}]", cell(self.capital_ref)]


def to_day(ts_ms) -> date:
    return datetime.fromtimestamp(int(ts_ms) / 1000, TIMEZONE).date()


def number(value, digits: int = 10) -> float:
    return float(round(Decimal(value), digits))


class PriceBook:
    """Clôtures quotidiennes <actif>-EUR d'OKX, chargées une fois par actif."""

    def __init__(self, client: OkxClient, since: date):
        self.client = client
        self.since_ms = int(datetime.combine(since - timedelta(days=7), datetime.min.time(), timezone.utc).timestamp() * 1000)
        self._closes: dict = {}

    def eur(self, ccy: str, day: date) -> Decimal:
        if ccy == "EUR":
            return Decimal(1)
        if ccy not in self._closes:
            self._closes[ccy] = self.client.daily_closes_eur(ccy, self.since_ms)
        for back in range(6):  # repli sur le dernier cours connu si la bougie du jour manque
            close = self._closes[ccy].get((day - timedelta(days=back)).isoformat())
            if close is not None:
                return Decimal(str(close))
        raise OkxError(f"Cours {ccy}-EUR introuvable pour le {day.isoformat()}")


def collect_bonus(client: OkxClient, prices: PriceBook, since_ms: int, journal: list) -> list:
    bills = client.get_all("/api/v5/asset/bills-history", {"type": BONUS_BILL_TYPE, "begin": str(since_ms)})
    operations = []

    for bill in bills:
        ccy, amount = bill["ccy"], Decimal(bill["balChg"])
        if amount <= 0:
            continue
        if ccy not in SUPPORTED_ASSETS:
            log.warning("Crédit OKX ignoré (actif %s non suivi dans le journal) : %s %s", ccy, bill["balChg"], bill["notes"])
            continue

        day = to_day(bill["ts"])
        operations.append(Operation(
            day=day, ts=int(bill["ts"]), type="Bonus", cede="", qte_out=None, recu=ccy, qte_in=number(amount),
            montant=number(amount * prices.eur(ccy, day), 2), frais=None,
            comment=f"Bonus OKX — valeur = quantité × cours {ccy}/EUR OKX du jour", tag=f"bill:{bill['billId']}",
            capital_ref=last_capital_reference(journal, ccy),
        ))

    return operations


def last_capital_reference(journal: list, ccy: str):
    """Capital de référence du dernier bonus du même actif (colonne facultative, saisie à la main)."""
    for row in reversed(journal):
        if row[COL_TYPE] == "Bonus" and row[COL_RECU] == ccy and isinstance(row[COL_CAPITAL], (int, float)):
            return row[COL_CAPITAL]
    return None


def staking_credits(client: OkxClient, since_ms: int) -> list:
    """Gains de staking quotidiens (ccy, jour, quantité).

    L'historique du compte de financement (type 139) est limité à ~100 jours ; pour les jours plus
    anciens, on lit les transferts entrants du même actif sur le compte de trading, qui remontent plus loin.
    """
    funding = [b for b in client.get_all("/api/v5/asset/bills-history", {"type": STAKING_BILL_TYPE, "begin": str(since_ms)})
               if b["ccy"] in STAKING_ASSETS]
    credits = [(b["ccy"], to_day(b["ts"]), Decimal(b["balChg"])) for b in funding]
    covered = {day for _, day, _ in credits}

    for bill in client.get_all("/api/v5/account/bills-archive", {"begin": str(since_ms)}):
        day = to_day(bill["ts"])
        if bill["ccy"] in STAKING_ASSETS and bill["type"] == "1" and bill["subType"] == "11" and day not in covered:
            credits.append((bill["ccy"], day, Decimal(bill["balChg"])))

    return credits


def collect_staking(client: OkxClient, prices: PriceBook, since_ms: int, since: date, today: date) -> list:
    months: dict = defaultdict(lambda: {"qty": Decimal(0), "eur": Decimal(0)})

    for ccy, day, amount in staking_credits(client, since_ms):
        asset = STAKING_ASSETS[ccy]
        if amount <= 0:
            continue
        month = months[(day.year, day.month, asset)]
        month["qty"] += amount
        month["eur"] += amount * prices.eur(asset, day)

    operations = []
    current_month = today.replace(day=1)

    for (year, month_number, asset), total in sorted(months.items()):
        first_day = date(year, month_number, 1)
        # Mois en cours ou entamé avant le début de la fenêtre : total incomplet, on attend / on ignore
        if first_day >= current_month or first_day < since:
            continue
        last_day = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        label = f"{year}-{month_number:02d}"
        operations.append(Operation(
            day=last_day, ts=int(datetime.combine(last_day, datetime.max.time(), TIMEZONE).timestamp() * 1000),
            type="Intérêt", cede="", qte_out=None, recu=asset, qte_in=number(total["qty"]),
            montant=number(total["eur"], 2), frais=None,
            comment=f"Intérêt Liquid Staking OKX (BETH) {label} — date = fin du mois, valeur = Σ quantité × cours {asset}/EUR du jour",
            tag=f"stk:{label}:{asset}",
        ))

    return operations


def collect_earn_interest(client: OkxClient, prices: PriceBook, since_ms: int) -> list:
    """Intérêts Simple Earn d'une position soldée : rachats - souscriptions du cycle (OKX n'isole pas les intérêts).

    Un cycle commence à une souscription (type 75) et se termine par un rachat (type 76) ; une position
    encore ouverte n'est pas traitée car ses intérêts ne sont pas définitifs.
    """
    events = client.get_all("/api/v5/asset/bills-history", {"type": EARN_SUBSCRIPTION_BILL_TYPE}) \
        + client.get_all("/api/v5/asset/bills-history", {"type": EARN_REDEMPTION_BILL_TYPE})
    operations = []

    for ccy in sorted({e["ccy"] for e in events} & SUPPORTED_ASSETS):
        still_open = sum(Decimal(s["amt"]) for s in client.get("/api/v5/finance/savings/balance", {"ccy": ccy})) > 0
        cycles: list = []
        cycle = {"sub": Decimal(0), "red": Decimal(0), "ts": 0, "bill": ""}

        for event in sorted((e for e in events if e["ccy"] == ccy), key=lambda e: int(e["ts"])):
            amount = Decimal(event["balChg"])
            if event["type"] == EARN_SUBSCRIPTION_BILL_TYPE:
                if cycle["red"] > 0:
                    cycles.append(cycle)
                    cycle = {"sub": Decimal(0), "red": Decimal(0), "ts": 0, "bill": ""}
                cycle["sub"] += -amount
            else:
                cycle.update(red=cycle["red"] + amount, ts=int(event["ts"]), bill=event["billId"])
        if cycle["red"] > 0 and not still_open:
            cycles.append(cycle)

        for cycle in cycles:
            interest = cycle["red"] - cycle["sub"]
            if interest <= 0 or cycle["ts"] < since_ms:
                continue
            day = to_day(cycle["ts"])
            operations.append(Operation(
                day=day, ts=cycle["ts"], type="Intérêt", cede="", qte_out=None, recu=ccy, qte_in=number(interest),
                montant=number(interest * prices.eur(ccy, day), 2), frais=None,
                comment=f"Intérêt Simple Earn OKX {ccy} — rachat moins souscriptions, date = rachat, valeur = quantité × cours {ccy}/EUR du jour",
                tag=f"earn:{ccy}:{cycle['bill']}",
            ))

    return operations


def collect_buys(client: OkxClient, prices: PriceBook, since_ms: int) -> list:
    fills = client.get_all("/api/v5/trade/fills-history", {"instType": "SPOT", "begin": str(since_ms)})
    orders: dict = defaultdict(list)
    for fill in fills:
        orders[fill["ordId"]].append(fill)

    operations = []

    for order_id, order_fills in orders.items():
        inst_id = order_fills[0]["instId"]
        base, quote = inst_id.split("-")

        if order_fills[0]["side"] != "buy":
            log.warning("Vente OKX non importée (%s, ordre %s) : à saisir à la main si besoin.", inst_id, order_id)
            continue
        if base not in SUPPORTED_ASSETS or quote not in BUY_QUOTES:
            log.warning("Achat OKX ignoré (paire %s non gérée par le journal, ordre %s).", inst_id, order_id)
            continue

        paid = sum(Decimal(f["fillPx"]) * Decimal(f["fillSz"]) for f in order_fills)  # montant en `quote`
        received = sum(Decimal(f["fillSz"]) for f in order_fills)
        fee_base = sum(Decimal(f["fee"] or 0) for f in order_fills if f["feeCcy"] == base)
        fee_quote = sum(Decimal(f["fee"] or 0) for f in order_fills if f["feeCcy"] == quote)
        average_price = paid / received

        # Les frais prélevés dans l'actif acheté sont déjà retirés de la quantité reçue ; ceux prélevés
        # dans la devise de la paire s'ajoutent à ce qui est payé. Un frais positif est une remise.
        received += fee_base
        paid -= fee_quote
        fees = max(Decimal(0), -fee_base) * average_price + max(Decimal(0), -fee_quote)

        day, ts = to_day(max(int(f["ts"]) for f in order_fills)), max(int(f["ts"]) for f in order_fills)
        rate = prices.eur(quote, day)
        in_euro = quote == "EUR"
        note = "Frais déjà déduits de la quantité" if fee_base < 0 else f"{base}-{quote}"

        operations.append(Operation(
            day=day, ts=ts, type=BUY_QUOTES[quote], cede=quote, qte_out=number(paid), recu=base, qte_in=number(received),
            montant=number(paid * rate, 2), frais=number(fees * rate, 4) or None,
            comment=note if in_euro else f"{quote} → {base} (pas de fait générateur) ; {note}",
            tag=f"ord:{order_id}",
        ))

    return operations


def read_journal(worksheet) -> list:
    """Lignes de saisie A:J, de la ligne 2 à la dernière ligne dont la date (colonne A) est renseignée."""
    rows = _call_with_retry(worksheet.get, f"A2:J{worksheet.row_count}", value_render_option="FORMULA")
    rows = [list(row) + [""] * (INPUT_COLS - len(row)) for row in rows]
    last = max((i for i, row in enumerate(rows) if str(row[COL_DATE]).strip() != ""), default=-1)
    return rows[:last + 1]


def drop_known(operations: list, journal: list) -> list:
    tags = {m.group(1) for row in journal for m in [TAG_RE.search(str(row[COL_COMMENT]))] if m}
    manual = Counter()
    for row in journal:
        if isinstance(row[COL_DATE], (int, float)) and isinstance(row[COL_QTE_IN], (int, float)):
            manual[(int(row[COL_DATE]), row[COL_TYPE], row[COL_RECU], round(row[COL_QTE_IN], 6))] += 1

    new = []
    for operation in operations:
        if operation.tag in tags:
            continue
        if manual[operation.key()] > 0:
            manual[operation.key()] -= 1  # une ligne saisie à la main ne couvre qu'une seule opération
            continue
        new.append(operation)
    return new


def merge(journal: list, operations: list) -> tuple:
    """Insère chaque opération avant la première ligne de date strictement postérieure (comme JournalForm.js).

    Retourne (nouvelles lignes du journal, indice de la première ligne modifiée).
    """
    rows = [list(row) for row in journal]
    first_changed = len(rows)

    for operation in sorted(operations, key=lambda op: (op.serial, op.ts)):
        index = next((i for i, row in enumerate(rows)
                      if isinstance(row[COL_DATE], (int, float)) and row[COL_DATE] > operation.serial), len(rows))
        rows.insert(index, operation.row())
        first_changed = min(first_changed, index)

    return rows, first_changed


def ensure_capacity(worksheet, last_row: int):
    """Ajoute des lignes à la feuille pleine en recopiant la dernière (formules de calcul incluses)."""
    missing = last_row - worksheet.row_count
    if missing <= 0:
        return

    source_row = worksheet.row_count
    sheet_id = worksheet.id
    columns = worksheet.col_count
    _call_with_retry(worksheet.add_rows, missing)
    _call_with_retry(worksheet.spreadsheet.batch_update, {"requests": [{"copyPaste": {
        "source": {"sheetId": sheet_id, "startRowIndex": source_row - 1, "endRowIndex": source_row,
                   "startColumnIndex": 0, "endColumnIndex": columns},
        "destination": {"sheetId": sheet_id, "startRowIndex": source_row, "endRowIndex": source_row + missing,
                        "startColumnIndex": 0, "endColumnIndex": columns},
        "pasteType": "PASTE_NORMAL",
    }}]})


def write_rows(worksheet, rows: list, first_changed: int):
    ensure_capacity(worksheet, 1 + len(rows))
    changed = rows[first_changed:]
    start = 2 + first_changed
    _call_with_retry(worksheet.batch_update,
                     [{"range": f"A{start}:J{start + len(changed) - 1}", "values": changed}],
                     value_input_option="USER_ENTERED")


def main() -> int:
    now = datetime.now(TIMEZONE)
    since = now.date() - timedelta(days=LOOKBACK_DAYS)
    since_ms = int(datetime.combine(since, datetime.min.time(), TIMEZONE).timestamp() * 1000)

    client = OkxClient()
    prices = PriceBook(client, since)
    worksheet = get_worksheet_by_name(SHEET_NAME)
    journal = read_journal(worksheet)
    log.info("Journal crypto : %s ligne(s) ; synchronisation OKX depuis le %s.", len(journal), since.isoformat())

    operations = (collect_bonus(client, prices, since_ms, journal)
                  + collect_staking(client, prices, since_ms, since, now.date())
                  + collect_earn_interest(client, prices, since_ms)
                  + collect_buys(client, prices, since_ms))
    new = drop_known(operations, journal)
    log.info("%s opération(s) OKX lue(s), %s nouvelle(s).", len(operations), len(new))

    if not new:
        return 0

    rows, first_changed = merge(journal, new)
    for operation in sorted(new, key=lambda op: (op.serial, op.ts)):
        log.info("  + %s %s %s %s -> %s %s (%s €)", operation.day.isoformat(), operation.type, operation.cede,
                 operation.qte_out if operation.qte_out is not None else "", operation.recu, operation.qte_in,
                 operation.montant)

    if DRY_RUN:
        log.info("DRY_RUN : aucune écriture.")
        return 0

    write_rows(worksheet, rows, first_changed)
    log.info("%s ligne(s) écrite(s) dans '%s' à partir de la ligne %s.", len(new), SHEET_NAME, 2 + first_changed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        sys.exit(main())
    except Exception:
        log.exception("Synchronisation OKX -> Journal crypto en échec")
        sys.exit(1)
