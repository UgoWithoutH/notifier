"""Live test: for each platform, run the real *_diversification.py with REPORT_DATE set to the
last day of the account's FIRST month and check that the six monthly-yield rows are computed.

Nothing is written to the Google Sheet: every `fill_*` writer in the platform module is replaced
by a recorder, so the values that WOULD have been written are captured and checked instead.

Usage (from the repo root, venv active):
    python _live_test_first_month_yield.py                       # every platform
    python _live_test_first_month_yield.py swaper afranga        # a subset
    python _live_test_first_month_yield.py peerberry=03/2025     # override the first month (MM/AAAA)

The first month is auto-detected from each platform's local cashflow cache when one exists;
platforms without a usable cache (peerberry, loanch, goandgrow, lande) need the `name=MM/AAAA` form.
"""
import calendar
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
DIV = ROOT / "diversification"
MARKER = "LIVETEST_RESULT:"
INTERACTIVE = ("mintos", "lande")  # need a manual login (CAPTCHA) via their *_get_session helper
EXPECTED_KEYS = [
    "Rendements % brut",
    "Intérêts brut %",
    "Bonus brut %",
    "Cash drag brut %",
    "Taxes brut %",
    "Frais brut %",
]
COMPONENT_KEYS = EXPECTED_KEYS[1:]
PLATFORMS = [
    "afranga", "bienpreter", "bricks", "debitum", "goandgrow", "income_marketplace", "iuvo", "lande",
    "lendermarket", "loanch", "mintos", "monefit", "nectaro", "peerberry", "swaper",
]


def _load(name: str):
    path = DIV / f"{name}_xirr_cashflows_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _parse(value: str, formats: tuple) -> date | None:
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def detect_first_month(name: str) -> date | None:
    """Earliest dated entry in the platform's local cache (assumes the cache starts at account creation)."""
    state = _load(name)
    if not state:
        return None
    dates: list[date | None] = []
    if name == "swaper":
        dates = [_parse(e["date"], ("%Y-%m-%d",)) for e in state["cashflows"] if e["transactionType"].upper() == "FUNDING"]
    elif name == "afranga":
        dates = [_parse(e["date"], ("%Y-%m-%d",)) for e in state["cashflows"]]
    elif name == "bienpreter":
        dates = [_parse(r["date"], ("%Y-%m-%d",)) for r in state["rows"]]
    elif name == "bricks":
        dates = [_parse(e["createdAt"][:10], ("%Y-%m-%d",)) for e in state["all_entries"]]
    elif name == "debitum":
        dates = [_parse(t["createdOn"][:10], ("%Y-%m-%d",)) for t in state["transactions"]]
    elif name == "income_marketplace":
        dates = [_parse(t["date"], ("%Y-%m-%d",)) for t in state["transactions"] if "deposit" in t["account_type_name"].lower()]
    elif name == "nectaro":
        dates = [_parse(t["date"][:11], ("%d %b %Y",)) for t in state["transactions"]]
    elif name == "mintos":
        dates = [_parse(e["date"], ("%d.%m.%Y",)) for e in state["all_entries"]]
    elif name in ("iuvo", "lendermarket", "monefit"):
        # Summaries also cover empty months before the account existed: keep only months with real activity.
        dates = [
            _parse(k + "-01", ("%Y-%m-%d",)) for k, s in state["monthly_summaries"].items()
            if any(isinstance(v, (int, float)) and v for f, v in s.items() if f != "days_covered")
        ]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def worker(name: str) -> None:
    import importlib
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, str(ROOT))
    module = importlib.import_module(f"diversification.{name}_diversification")

    captured: dict = {}

    def recorder(*args, **kwargs):
        for value in list(args) + list(kwargs.values()):
            if isinstance(value, dict):
                captured.update({k: v for k, v in value.items() if isinstance(k, str)})

    for attr in dir(module):
        if attr.startswith("fill_") and callable(getattr(module, attr)):
            setattr(module, attr, recorder)

    status = "ok"
    try:
        if name in INTERACTIVE:
            # The *_get_session helper handles login (CAPTCHA/Enter prompts) then calls module.run(session=...).
            importlib.import_module(f"diversification.{name}_get_session").main()
        else:
            module.run()
    except SystemExit as exc:
        status = f"exit({exc.code})"
    except Exception as exc:
        status = f"error: {type(exc).__name__}: {exc}"
    payload = json.dumps({"status": status, "captured": captured}, default=str)
    if os.environ.get("LIVETEST_OUT"):
        Path(os.environ["LIVETEST_OUT"]).write_text(payload, encoding="utf-8")
    print(MARKER + payload, flush=True)


def evaluate(captured: dict) -> tuple[bool, list[str]]:
    problems = []
    for key in EXPECTED_KEYS:
        if captured.get(key) is None:
            problems.append(f"missing {key}")
    if not problems:
        total = sum(captured[k] for k in COMPONENT_KEYS)
        if abs(total - captured["Rendements % brut"]) > 1e-6:
            problems.append(f"components sum {total * 100:.4f}% != Rendements % brut {captured['Rendements % brut'] * 100:.4f}%")
    return not problems, problems


def driver(requests: list[str]) -> int:
    overrides, names = {}, []
    for item in requests:
        name, _, month = item.partition("=")
        names.append(name)
        if month:
            m, y = month.split("/")
            overrides[name] = date(int(y), int(m), 1)
    names = names or PLATFORMS

    summary = []
    for name in names:
        first = overrides.get(name) or detect_first_month(name)
        if first is None:
            summary.append((name, "SKIP", "first month unknown: pass name=MM/AAAA", None))
            continue
        report_date = month_end(first)
        print(f"\n=== {name}: first month {first:%m/%Y}, REPORT_DATE={report_date:%d/%m/%Y} ===", flush=True)
        env = {**os.environ, "REPORT_DATE": f"{report_date:%d/%m/%Y}", "PYTHONIOENCODING": "utf-8"}
        env.pop("REPORT_DATE_MONTHS", None)
        if name in INTERACTIVE:
            # Inherit stdio so the CAPTCHA/Enter prompts reach the user; read the result back from a file.
            out_file = Path(tempfile.gettempdir()) / f"livetest_{name}.json"
            out_file.unlink(missing_ok=True)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__)), "--worker", name],
                cwd=ROOT, env={**env, "LIVETEST_OUT": str(out_file)},
            )
            line = MARKER + out_file.read_text(encoding="utf-8") if out_file.exists() else None
            if line:
                out_file.unlink(missing_ok=True)
            proc.stdout = proc.stderr = ""
        else:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__)), "--worker", name],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(MARKER)), None)
        if line is None:
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
            summary.append((name, "FAIL", "no result (crash)", None))
            print(tail)
            continue
        result = json.loads(line[len(MARKER):])
        ok, problems = evaluate(result["captured"])
        detail = result["status"] if result["status"] != "ok" else ""
        if problems:
            detail = (detail + "; " if detail else "") + ", ".join(problems)
        summary.append((name, "PASS" if ok else "FAIL", detail, result["captured"]))
        if not ok:
            print("\n".join(ln for ln in (proc.stdout + proc.stderr).splitlines() if "ERROR" in ln or "WARNING" in ln or "Traceback" in ln)[-3000:])

    print("\n" + "=" * 100)
    for name, status, detail, captured in summary:
        values = ""
        if captured:
            values = "  " + " | ".join(
                f"{k.replace(' brut', '').replace(' %', '')}={captured[k] * 100:.4f}%" for k in EXPECTED_KEYS if captured.get(k) is not None
            )
        print(f"[{status}] {name:20s}{detail}{values}")
    return 0 if all(s[1] in ("PASS", "SKIP") for s in summary) else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    else:
        sys.exit(driver(sys.argv[1:]))
