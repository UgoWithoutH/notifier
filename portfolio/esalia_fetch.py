"""Minimal Esalia (Société Générale épargne salariale) reader: PEE funds, parts, values and unrealized gains.

Logs in like the website (IAM callbacks, one-time code), lets the site's back end open its API session,
then reads /api/epargnant/v1/plans-epargne. Nothing from woob is used.
.env: ESALIA_LOGIN (identifiant) and ESALIA_PASSWORD (code secret à 6 chiffres).
A one-time code (mail or SMS) is asked at each run; the device is NOT registered as trusted.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import random
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from dotenv import dotenv_values

BASE = "https://salaries.esalia.com"
AUTH = "https://iam.esalia.com"
SLUG = "sg"
REALM = "sg_ws"
SERVICE = "authn_sg_ws"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:91.0) Gecko/20100101 Firefox/91.0"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "portfolio_imports" / "esalia"
PEE_MARKERS = ("PEE", "PEI", "PEG")
EMBEDDED = "dispositif_epargne,support_investissement,positions,profils_risque,entreprise"
CSV_HEADERS = ["Compte", "Fonds", "Parts", "Valeur part", "Valorisation", "Plus-value latente"]

FINGERPRINT = (
    '{"screen":{"screenWidth":1920,"screenHeight":1080,"screenColourDepth":24},'
    '"timezone":{"timezone":-120},"plugins":{"installedPlugins":""},"fonts":'
    '{"installedFonts":"cursive;monospace;serif;sans-serif;fantasy;default;Arial;"},'
    f'"userAgent":"{USER_AGENT}","appName":"Netscape","appCodeName":"Mozilla",'
    '"appVersion":"5.0 (X11)","platform":"Linux x86_64","oscpu":"Linux x86_64",'
    '"product":"Gecko","productSub":"20100101","language":"en-US"}'
)


def _secret(name: str) -> str:
    value = (dotenv_values().get(name) or "").strip()
    if not value:
        raise SystemExit(f"Variable manquante : {name} (à définir dans .env)")
    return value


def _encrypt_password(public_key_b64: str, password: str, salted: bool = True) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    raw = base64.b64decode(public_key_b64)
    try:
        key = serialization.load_pem_public_key(raw)
    except ValueError:
        key = serialization.load_der_public_key(raw)
    # Same scheme as the website's JS: optional 24 random digits + password, RSA PKCS#1 v1.5.
    salt = "".join(random.choices("0123456789", k=24)) if salted else ""
    return base64.b64encode(key.encrypt((salt + password).encode(), padding.PKCS1v15())).decode()


class Esalia:
    def __init__(self, state_dir: Path = OUTPUT_DIR) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.state_dir = state_dir

    def _device_profile(self) -> str:
        """Browser-like profile for the ForgeRock device step; the identifier is kept between runs."""
        path = self.state_dir / "device_id.txt"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(random.choices("0123456789abcdef", k=64)), encoding="utf-8")
        profile = {
            "identifier": path.read_text(encoding="utf-8").strip(),
            "metadata": {
                "hardware": {
                    "cpuClass": None,
                    "deviceMemory": 8,
                    "hardwareConcurrency": 8,
                    "maxTouchPoints": 0,
                    "oscpu": "Linux x86_64",
                    "display": {"width": 1920, "height": 1080, "pixelDepth": 24, "angle": 0},
                },
                "browser": {
                    "userAgent": USER_AGENT,
                    "appName": "Netscape",
                    "appCodeName": "Mozilla",
                    "appVersion": "5.0 (X11)",
                    "appMinorVersion": None,
                    "buildID": "20181001000000",
                    "product": "Gecko",
                    "productSub": "20100101",
                    "vendor": "",
                    "vendorSub": "",
                    "language": "fr-FR",
                    "plugins": "",
                },
                "platform": {
                    "platform": "Linux x86_64",
                    "deviceName": "Linux",
                    "fonts": ["Arial", "Courier New", "Times New Roman"],
                    "timezone": -120,
                },
            },
        }
        return json.dumps(profile)

    # --- login (ForgeRock "callbacks" flow, matched to what Esalia returns today)
    def _show(self, data: dict) -> None:
        prompts = [
            (
                cb.get("type"),
                [str(o.get("value"))[:40] for o in cb.get("output", []) if o.get("name") == "prompt"]
                + [f"{o.get('name')}={o.get('value')}" for o in cb.get("output", []) if isinstance(o.get("value"), bool)]
                + [f"choices={o.get('value')}" for o in cb.get("output", []) if o.get("name") == "choices"],
            )
            for cb in data.get("callbacks", [])
            if cb.get("type") not in ("TextOutputCallback", "MetadataCallback", "HiddenValueCallback")
        ]
        print(f"Etape Esalia : {data.get('stage')} {prompts}")

    def _answer(self, data: dict) -> None:
        """Fill a follow-up step (device profile, send-code button, one-time code, trusted device)."""
        if data.get("stage") == "DeviceIdMatch2":
            data["callbacks"][0]["input"][0]["value"] = FINGERPRINT
            return
        for callback in data.get("callbacks", []):
            kind = callback.get("type")
            outputs = {o.get("name"): o.get("value") for o in callback.get("output", [])}
            inputs = callback.get("input", [])
            prompt = str(outputs.get("prompt", ""))
            if not inputs:
                continue
            if kind == "DeviceProfileCallback":
                inputs[0]["value"] = self._device_profile()
            elif kind in ("NameCallback", "PasswordCallback", "TextInputCallback") and re.search(
                "code|otp|onetime|temporaire|re\u00e7u", prompt, re.IGNORECASE
            ):
                inputs[0]["value"] = input("Code unique Esalia (re\u00e7u par mail ou SMS) : ").strip()
            elif kind == "ConfirmationCallback":
                inputs[0]["value"] = 0
            elif kind == "ChoiceCallback":
                choices = [str(c).lower() for c in outputs.get("choices", [])]
                default = outputs.get("defaultChoice", 0)
                if prompt == "iam_action":
                    # Pick the validating action, never "resend" or "cancel".
                    positive = [
                        i
                        for i, c in enumerate(choices)
                        if re.search("valid|submit|confirm|continu|suivant|ok|envoyer|send", c)
                        and not re.search("resend|renvoy|cancel|annul|back|retour", c)
                    ]
                    inputs[0]["value"] = positive[0] if positive else default
                else:
                    inputs[0]["value"] = default

    def login(self) -> None:
        username, password = _secret("ESALIA_LOGIN"), _secret("ESALIA_PASSWORD")
        response = self.session.get(f"{BASE}/portal/salarie-{SLUG}/connect", timeout=30)
        redirect_uri = parse_qs(urlparse(response.url).query).get("goto", [""])[0]
        auth_url = f"{AUTH}/connect/json/realms/root/realms/{REALM}/authenticate"
        params = {"locale": "fr", "goto": redirect_uri, "authIndexType": "service", "authIndexValue": SERVICE}
        data = self.session.post(auth_url, params=params, data="", timeout=30).json()
        self._show(data)

        public_key, salted, password_input = "", True, None
        for callback in data["callbacks"]:
            kind = callback.get("type")
            outputs = {o.get("name"): o.get("value") for o in callback.get("output", [])}
            inputs = callback.get("input", [])
            if kind == "NameCallback":
                inputs[0]["value"] = username
            elif kind == "HiddenValueCallback" and outputs.get("id") == "publicKey":
                public_key = outputs["value"]
                inputs[0]["value"] = public_key
            elif kind == "PasswordCallback":
                password_input = inputs[0]
            elif kind == "MetadataCallback" and isinstance(outputs.get("data"), dict):
                salted = not outputs["data"].get("ENCRYPT_WITHOUT_SALT", False)
            elif kind == "ChoiceCallback":
                inputs[0]["value"] = outputs.get("defaultChoice", 0)
        if not public_key or password_input is None:
            raise SystemExit("Etape de connexion Esalia inattendue : cl\u00e9 publique ou champ mot de passe absent.")
        password_input["value"] = _encrypt_password(public_key, password, salted)

        for _ in range(8):
            response = self.session.post(auth_url, json=data, timeout=30)
            if response.status_code == 401:
                raise SystemExit("Identifiants ou code Esalia refus\u00e9s.")
            response.raise_for_status()
            data = response.json()
            if data.get("tokenId"):
                break
            self._show(data)
            self._answer(data)
        else:
            raise SystemExit("Connexion Esalia non aboutie apr\u00e8s 8 \u00e9tapes.")

        self.session.cookies["idtksam"] = data["tokenId"]
        # The site's back end runs the OAuth code + PKCE exchange itself and keeps the token in its session.
        reply = self.session.get(f"{BASE}/sso/login?redirect_uri=/web/login/process", timeout=60)
        if "OBFFSESSIONID" not in self.session.cookies:
            raise SystemExit(f"Session Esalia non \u00e9tablie (HTTP {reply.status_code}).")
        print("Connexion Esalia r\u00e9ussie.")

    def api(self, path: str, **kwargs) -> requests.Response:
        headers = {"Accept": "application/json"}
        xsrf = self.session.cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        return self.session.get(f"{BASE}/api/epargnant/v1/{path.lstrip('/')}", headers=headers, timeout=60, **kwargs)

    @staticmethod
    def _shape(value, depth: int = 0):
        """Structure of a JSON value (field names and types only, no values)."""
        if isinstance(value, dict):
            return {k: Esalia._shape(v, depth + 1) for k, v in value.items()} if depth < 9 else "{...}"
        if isinstance(value, list):
            return [Esalia._shape(value[0], depth + 1), f"({len(value)} \u00e9l.)"] if value else []
        return type(value).__name__

    def bundles(self) -> list[str]:
        page = self.session.get(f"{BASE}/web/", timeout=60)
        return [
            self.session.get(urljoin(page.url, source), timeout=60).text
            for source in re.findall(r'src="([^"]+\.js)"', page.text)
        ]

    def resources(self) -> list[str]:
        """API resource names declared in the site's public JavaScript."""
        page = self.session.get(f"{BASE}/web/", timeout=60)
        names: set[str] = set()
        for source in re.findall(r'src="([^"]+\.js)"', page.text):
            body = self.session.get(urljoin(page.url, source), timeout=60).text
            names.update(re.findall(r'super\(\w+,"([a-z][a-z0-9\-/]+)",\w+\)', body))
        return sorted(names)

    def plans(self) -> list[dict]:
        reply = self.api("plans-epargne", params={"_embedded": EMBEDDED})
        reply.raise_for_status()
        return reply.json()

    def transactions(self, company_id: str, page_size: int = 50) -> list[dict]:
        """All transactions, newest first. The paging parameter names are not documented, so they are probed."""
        names = None
        for offset_name, limit_name in ("offset", "limit"), ("_offset", "_limit"), ("debut", "limite"):
            probe = self.api("transactions", params={"id_entreprise": company_id, limit_name: 3})
            if probe.ok and len(probe.json()) == 3:
                names = (offset_name, limit_name)
                break
        if names is None:
            reply = self.api("transactions", params={"id_entreprise": company_id})
            reply.raise_for_status()
            return reply.json()
        items: list[dict] = []
        for offset in range(0, 5000, page_size):
            reply = self.api(
                "transactions",
                params={"id_entreprise": company_id, names[0]: offset, names[1]: page_size, "sort": "DATE_CREATION"},
            )
            reply.raise_for_status()
            batch = reply.json()
            items += batch
            if len(batch) < page_size:
                break
        return items

    def transaction_detail(self, transaction_id: str) -> dict:
        reply = self.api(f"transactions/{transaction_id}", params={"_embedded": TRANSACTION_EMBEDDED})
        reply.raise_for_status()
        return reply.json()


TRANSACTION_EMBEDDED = "instructions,mouvements,retenues,allocations,reglements,support_investissement,dispositif_epargne,detail"


def sync_transactions(client: Esalia, plan: dict, output_dir: Path) -> None:
    """Appends new transactions (with details) to an append-only JSON ledger and prints a summary."""
    company_id = ((plan.get("_links") or {}).get("entreprise", {}).get("href", "")).rstrip("/").split("/")[-1]
    ledger_path = output_dir / "transactions.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    listed = client.transactions(company_id)
    new = [item for item in listed if item["id"] not in ledger]
    for item in new:
        ledger[item["id"]] = {**item, "_detail": client.transaction_detail(item["id"])}
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(listed)} transactions list\u00e9es, {len(new)} nouvelles \u2192 {ledger_path}")
    if new:
        print("Structure d'un d\u00e9tail :", json.dumps(Esalia._shape(ledger[new[0]['id']]['_detail']), ensure_ascii=False)[:2500])
    for item in sorted(ledger.values(), key=lambda t: t.get("date_comptabilisation") or t.get("date_creation") or ""):
        net = (item.get("montant_net") or {}).get("montant")
        employer = (item.get("abondement_net") or {}).get("montant")
        print(
            f"   {str(item.get('date_comptabilisation') or item.get('date_creation'))[:10]} "
            f"{item.get('type')} {item.get('statut')} {item.get('cadre')} net={net} abondement={employer}"
        )


def _links(node, path: str = ".") -> list[str]:
    """HATEOAS links found in a JSON tree (names and masked paths only)."""
    found: list[str] = []
    if isinstance(node, dict):
        for name, link in (node.get("_links") or {}).items():
            if isinstance(link, dict):
                href = re.sub(r"[0-9A-Za-z_\-]{12,}", "<id>", str(link.get("href")))
                found.append(f"{path} :: {name} -> {link.get('method')} {href}")
        for key, value in (node.get("_embedded") or {}).items():
            found += _links(value, f"{path}/{key}")
    elif isinstance(node, list) and node:
        found += _links(node[0], f"{path}[0]")
    return found


def _find(node, keys: tuple[str, ...]) -> str | None:
    """First non-empty string stored under one of `keys`, searching nested dicts and lists."""
    if isinstance(node, dict):
        for key in keys:
            if isinstance(node.get(key), str) and node[key].strip():
                return node[key].strip()
        for value in node.values():
            found = _find(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find(item, keys)
            if found:
                return found
    return None


def _scheme(plan: dict) -> tuple[str, str]:
    scheme = (plan.get("_embedded") or {}).get("dispositif_epargne") or {}
    return str(scheme.get("titre") or ""), str(scheme.get("type") or "")


def fund_rows(plans: list[dict], amount_divisor: float, parts_divisor: float) -> list[dict]:
    rows = []
    for plan in plans:
        title, kind = _scheme(plan)
        if not any(marker in f"{title} {kind}".upper() for marker in PEE_MARKERS):
            continue
        for position in (plan.get("_embedded") or {}).get("positions") or []:
            name = _find(
                position.get("_embedded") or {},
                ("libelle_long", "libelle", "nom_long", "nom", "titre", "designation", "label"),
            ) or str(position.get("id_support_investissement", "?"))
            raw_parts = position.get("nb_parts")
            raw_value = (position.get("montant_total_brut") or {}).get("montant")
            raw_gain = (position.get("plus_value") or {}).get("montant")
            parts = None if raw_parts is None else raw_parts / parts_divisor
            value = None if raw_value is None else raw_value / amount_divisor
            gain = None if raw_gain is None else raw_gain / amount_divisor
            rows.append(
                {
                    "Compte": title or kind,
                    "Fonds": name,
                    "Parts": parts,
                    "Valeur part": value / parts if value is not None and parts else None,
                    "Valorisation": value,
                    "Plus-value latente": gain,
                    "_brut": (raw_parts, raw_value, raw_gain),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Lit le PEE Esalia : fonds, parts, valorisations, plus-values.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--amount-divisor", type=float, default=1.0, help="Diviseur des montants (100 si en centimes).")
    parser.add_argument("--parts-divisor", type=float, default=1.0, help="Diviseur du nombre de parts.")
    parser.add_argument("--discover", action="store_true", help="Affiche la structure (noms de champs) d'une position.")
    parser.add_argument("--transactions", action="store_true", help="Synchronise l'historique des transactions (versements, abondements).")
    args = parser.parse_args()

    client = Esalia(args.output_dir)
    try:
        client.login()
        plans = client.plans()
    except requests.exceptions.SSLError:
        raise SystemExit(
            "Erreur de certificat SSL (proxy d'entreprise ?). Lance ce script depuis un réseau personnel."
        ) from None

    if args.discover:
        bodies = client.bundles()
        for pattern in ('super(this.http,"transactions"', '"transactions"', '"abondements"', '"versements-programmes"'):
            for body in bodies:
                match = re.search(re.escape(pattern), body)
                if match:
                    print(f"[{pattern}] ..." + " ".join(body[max(0, match.start() - 80) : match.end() + 900].split()))
                    break
        pee = next((p for p in plans if any(m in " ".join(_scheme(p)).upper() for m in PEE_MARKERS)), None)
        if pee is None:
            return
        company_id = ((pee.get("_links") or {}).get("entreprise", {}).get("href", "")).rstrip("/").split("/")[-1]
        print("Sondage de 'transactions' (lecture seule) :")
        for params in (None, {"id_entreprise": company_id}):
            reply = client.api("transactions", params=params)
            try:
                shape = json.dumps(client._shape(reply.json()), ensure_ascii=False)[:1500]
            except ValueError:
                shape = ""
            print(f"   transactions {list(params) if params else ''} -> HTTP {reply.status_code} {shape}")
        return

    if args.transactions:
        pee = next((p for p in plans if any(m in " ".join(_scheme(p)).upper() for m in PEE_MARKERS)), None)
        if pee is None:
            raise SystemExit("Aucun PEE trouv\u00e9.")
        sync_transactions(client, pee, args.output_dir)
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = args.output_dir / f"plans_{date.today():%Y%m%d}.json"
    snapshot.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")

    rows = fund_rows(plans, args.amount_divisor, args.parts_divisor)
    if not rows:
        raise SystemExit(f"Aucun PEE trouv\u00e9. Dispositifs vus : {[_scheme(p)[0] for p in plans]}")
    path = args.output_dir / f"esalia_{date.today():%Y%m%d}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} fonds \u00e9crits dans {path} ; instantan\u00e9 brut : {snapshot.name}")
    print("Valeurs brutes (parts, montant, plus-value) \u00e0 comparer avec l'application :")
    for row in rows:
        print(f"   {row['Fonds'][:40]:40} {row['_brut']}")


if __name__ == "__main__":
    main()
