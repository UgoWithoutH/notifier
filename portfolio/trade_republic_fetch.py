"""Fetch Trade Republic positions and write them as a CSV for portfolio.csv_import.

Secrets are read from the environment (.env, never committed):
  TR_PHONE_NUMBER  phone number in international format (+33...)
  TR_PIN           Trade Republic PIN
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import os
import platform
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import requests
import websockets
from dotenv import load_dotenv

API_URL = "https://api.traderepublic.com"
WS_URL = "wss://api.traderepublic.com"
EXCHANGE = "LSX"
RECV_TIMEOUT = 30
LOGIN_TIMEOUT = 120
APP_VERSION = "2.2631.13"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

CSV_HEADERS = ["Nom", "ISIN", "Quantité", "Cours", "Valorisation", "PRU", "Devise"]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Variable d'environnement manquante : {name} (à définir dans .env)")
    return value


def _login_headers() -> dict[str, str]:
    seed = "|".join([str(uuid.getnode()), platform.node(), platform.machine(), platform.system()])
    offset = datetime.now().astimezone().utcoffset()
    device = {
        "stableDeviceId": hashlib.sha512(seed.encode()).hexdigest(),
        "browser": "Chrome",
        "browserVersion": "146.0.0.0",
        "os": platform.system(),
        "osVersion": platform.release(),
        "timezone": "Europe/Paris",
        "timezoneOffset": -int(offset.total_seconds() // 60) if offset else 0,
        "screen": "1920x1080x24",
        "preferredLanguages": ["fr"],
        "numberOfCores": os.cpu_count() or 1,
    }
    return {
        "X-TR-Device-Info": base64.b64encode(json.dumps(device).encode()).decode(),
        "X-TR-App-Version": APP_VERSION,
        "X-Tr-Platform": "web-pro",
        "Accept-Language": "fr",
    }


def _process(session: requests.Session, process_id: str, headers: dict[str, str]) -> dict:
    response = session.get(
        f"{API_URL}/api/v2/auth/web/login/processes/{process_id}", headers=headers, timeout=30
    )
    if response.status_code >= 400:
        raise SystemExit(f"Suivi de connexion Trade Republic refusé (HTTP {response.status_code}).")
    return response.json()


def login() -> requests.Session:
    """Log in (v2 web flow) and return a session holding the auth cookies."""
    phone_number = _require_env("TR_PHONE_NUMBER")
    pin = _require_env("TR_PIN")
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    headers = _login_headers()

    response = session.post(
        f"{API_URL}/api/v2/auth/web/login",
        json={"phoneNumber": phone_number, "pin": pin},
        headers=headers,
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"Connexion Trade Republic refusée (HTTP {response.status_code}).")
    process_id = response.json().get("processId")
    if not process_id:
        raise SystemExit("Connexion Trade Republic : processId absent, vérifier les identifiants.")

    process = _process(session, process_id, headers)
    if process.get("requiredAction") == "AUTHENTICATOR_VERIFICATION":
        code = input("Code de l'application d'authentification : ").strip()
        verify = session.post(
            f"{API_URL}/api/v2/auth/web/login/processes/{process_id}/authenticator-verification",
            json={"code": code},
            headers=headers,
            timeout=30,
        )
        if verify.status_code >= 400:
            raise SystemExit(f"Code d'authentification refusé (HTTP {verify.status_code}).")
        return session

    print("Confirme la connexion dans l'application Trade Republic...")
    deadline = time.time() + LOGIN_TIMEOUT
    while process.get("status") == "PENDING":
        if time.time() > deadline:
            raise SystemExit("Connexion non confirmée à temps.")
        time.sleep(2)
        process = _process(session, process_id, headers)
    if process.get("status") not in ("CONFIRMED", "COMPLETED"):
        raise SystemExit(f"Connexion refusée (statut {process.get('status')}).")
    return session


class _Socket:
    def __init__(self, websocket) -> None:
        self._ws = websocket
        self._next_id = 0

    async def request(self, payload: dict) -> dict:
        self._next_id += 1
        message_id = self._next_id
        await self._ws.send(f"sub {message_id} {json.dumps(payload)}")
        try:
            while True:
                message = await asyncio.wait_for(self._ws.recv(), RECV_TIMEOUT)
                prefix, _, rest = message.partition(" ")
                if prefix != str(message_id):
                    continue
                state, _, body = rest.partition(" ")
                if state == "E":
                    raise RuntimeError(f"Erreur API Trade Republic pour {payload['type']}: {body}")
                if state == "A":
                    return json.loads(body)
        finally:
            await self._ws.send(f"unsub {message_id}")


def _account_number(session: requests.Session) -> str:
    session.get(f"{API_URL}/api/v1/auth/web/session", timeout=30).raise_for_status()
    response = session.get(f"{API_URL}/api/v2/auth/account", timeout=30)
    response.raise_for_status()
    number = response.json().get("securitiesAccountNumber")
    if not number:
        raise SystemExit("Numéro de compte-titres introuvable dans la réponse Trade Republic.")
    return number


async def _fetch_positions(session: requests.Session) -> list[dict]:
    account_number = _account_number(session)
    cookies = "; ".join(
        f"{cookie.name}={cookie.value}"
        for cookie in session.cookies
        if cookie.domain.endswith("traderepublic.com")
    )
    async with websockets.connect(
        WS_URL, additional_headers={"Cookie": cookies}
    ) as websocket:
        await websocket.send(
            "connect 31 "
            + json.dumps(
                {
                    "locale": "fr",
                    "platformId": "webtrading",
                    "platformVersion": "chrome - 94.0.4606",
                    "clientId": "app.traderepublic.com",
                    "clientVersion": "5582",
                }
            )
        )
        reply = await websocket.recv()
        if reply != "connected":
            raise RuntimeError(f"Connexion WebSocket refusée : {reply}")
        socket = _Socket(websocket)

        portfolio = await socket.request(
            {"type": "compactPortfolioByType", "secAccNo": account_number}
        )
        rows = []
        for category in portfolio.get("categories", []):
            for position in category.get("positions", []):
                isin = position.get("isin") or position["instrumentId"]
                quantity = float(position["netSize"])
                if quantity <= 0:
                    continue
                instrument = await socket.request({"type": "instrument", "id": isin})
                ticker = await socket.request({"type": "ticker", "id": f"{isin}.{EXCHANGE}"})
                price = next(
                    (
                        float(ticker[side]["price"])
                        for side in ("last", "bid", "ask")
                        if ticker.get(side, {}).get("price")
                    ),
                    None,
                )
                if price is None:
                    raise RuntimeError(f"Cours introuvable pour {isin}")
                rows.append(
                    {
                        "Nom": instrument.get("shortName") or instrument.get("name") or isin,
                        "ISIN": isin,
                        "Quantité": quantity,
                        "Cours": price,
                        "Valorisation": round(quantity * price, 2),
                        "PRU": float(position["averageBuyIn"]),
                        "Devise": "EUR",
                    }
                )
        return rows


def write_csv(rows: list[dict], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"trade_republic_{date.today():%Y%m%d}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Récupère les positions Trade Republic et écrit le CSV pour l'import."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "portfolio_imports" / "trade_republic",
    )
    args = parser.parse_args()

    rows = asyncio.run(_fetch_positions(login()))
    if not rows:
        raise SystemExit("Aucune position Trade Republic trouvée.")
    path = write_csv(rows, args.output_dir)
    print(f"{len(rows)} position(s) écrite(s) dans {path}")


if __name__ == "__main__":
    main()
