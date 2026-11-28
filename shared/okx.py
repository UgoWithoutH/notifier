"""Client minimal (lecture seule) de l'API REST OKX v5.

Les comptes européens dépendent du domaine eea.okx.com : la même clé API est refusée
("API key doesn't exist") sur www.okx.com. Le domaine peut être surchargé avec OKX_BASE_URL.
"""
import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://eea.okx.com"
RATE_LIMIT_CODE = "50011"
PAGE_SIZE = 100
MIN_INTERVAL_SECONDS = 0.6  # les endpoints "bills" sont limités à quelques requêtes par seconde
MAX_ATTEMPTS = 6
REQUEST_TIMEOUT_SECONDS = 30


class OkxError(RuntimeError):
    pass


class OkxClient:
    def __init__(self, api_key=None, secret_key=None, passphrase=None, base_url=None):
        self.api_key = api_key or os.environ["OKX_API_KEY"]
        self.secret_key = secret_key or os.environ["OKX_SECRET_KEY"]
        self.passphrase = passphrase or os.environ["OKX_PASSPHRASE"]
        self.base_url = (base_url or os.environ.get("OKX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._last_call = 0.0

    def _headers(self, request_path: str) -> dict:
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        digest = hmac.new(self.secret_key.encode(), f"{timestamp}GET{request_path}".encode(), hashlib.sha256).digest()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None) -> list:
        """GET signé ; retourne le champ "data" et réessaie (backoff) quand OKX limite le débit."""
        request_path = path + (f"?{urlencode(params)}" if params else "")
        wait = 2.0

        for attempt in range(1, MAX_ATTEMPTS + 1):
            pause = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if pause > 0:
                time.sleep(pause)

            response = requests.get(self.base_url + request_path, headers=self._headers(request_path),
                                    timeout=REQUEST_TIMEOUT_SECONDS)
            self._last_call = time.monotonic()

            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = str(payload.get("code", ""))

            if code == "0":
                return payload.get("data", [])

            retryable = code == RATE_LIMIT_CODE or response.status_code in (429, 502, 503, 504)
            if retryable and attempt < MAX_ATTEMPTS:
                log.warning("OKX %s : %s (tentative %s/%s), nouvelle tentative dans %ss",
                            path, payload.get("msg") or response.status_code, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                wait *= 2
                continue

            raise OkxError(f"OKX {path} : HTTP {response.status_code}, code={code or '?'}, msg={payload.get('msg', '')}")

        raise OkxError(f"OKX {path} : échec après {MAX_ATTEMPTS} tentatives")

    def get_all(self, path: str, params: dict | None = None, cursor: str = "billId") -> list:
        """Parcourt toutes les pages (du plus récent au plus ancien) via le paramètre `after`."""
        params = {**(params or {}), "limit": str(PAGE_SIZE)}
        rows: list = []

        while True:
            page = self.get(path, params)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            params["after"] = page[-1][cursor]

    def daily_closes_eur(self, ccy: str, since_ms: int) -> dict:
        """Cours de clôture quotidiens (UTC) de `ccy`-EUR depuis `since_ms`, indexés par "AAAA-MM-JJ"."""
        params = {"instId": f"{ccy}-EUR", "bar": "1D", "limit": str(PAGE_SIZE)}
        closes: dict = {}

        while True:
            page = self.get("/api/v5/market/history-candles", params)
            for candle in page:
                day = datetime.fromtimestamp(int(candle[0]) / 1000, timezone.utc).strftime("%Y-%m-%d")
                closes[day] = float(candle[4])
            if len(page) < PAGE_SIZE or int(page[-1][0]) <= since_ms:
                return closes
            params["after"] = page[-1][0]
