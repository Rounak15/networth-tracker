"""Local net worth tracker. Run: python app.py  ->  http://localhost:8000"""
import base64, csv, io, json, os, re, shutil, socket, threading, time, uuid, webbrowser
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
FILE = DATA / "assets.json"
app = FastAPI()
server = None  # set when started with: python app.py
KEY = None  # encryption key; only ever held in memory, never written to disk
MAGIC = b"NWT1"


def meta():
    return DATA / "vault.meta"


def derive(pw, salt, n=2**16, r=8, p=1):
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(pw.encode())


def enc(b, key=None):
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key or KEY).encrypt(nonce, b, None)


def dec(raw, key=None):
    return AESGCM(key or KEY).decrypt(raw[4:16], raw[16:], None)


def read_json(path, default):
    if not path.exists():
        return default
    raw = path.read_bytes()
    return json.loads(dec(raw) if raw[:4] == MAGIC else raw)  # plain text only exists before setup


def write_json(path, obj):
    tmp = path.with_suffix(".tmp")  # atomic write
    tmp.write_bytes(enc(json.dumps(obj).encode()))
    os.replace(tmp, path)


def migrate_plain():
    """Encrypt files written by earlier versions (current data, snapshots and backups)."""
    for p in [FILE, DATA / "snapshots.json", *(DATA / "backups").glob("assets-*.json")]:
        if p.exists() and p.read_bytes()[:4] != MAGIC:
            write_json(p, json.loads(p.read_bytes()))


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and not p.startswith("/api/auth/") and p != "/api/shutdown" and KEY is None:
        return JSONResponse({"detail": "locked"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status():
    return {"exists": meta().exists(), "unlocked": KEY is not None}


@app.post("/api/auth/setup")
def auth_setup(body: dict):
    global KEY
    pw = str(body.get("password", ""))
    if meta().exists():
        raise HTTPException(400, "Already set up.")
    if len(pw) < 6:
        raise HTTPException(400, "Use at least 6 characters.")
    salt = os.urandom(16)
    KEY = derive(pw, salt)
    meta().write_text(json.dumps({"v": 1, "salt": base64.b64encode(salt).decode(), "n": 2**16, "r": 8, "p": 1,
                                  "check": base64.b64encode(enc(b"networth-ok")).decode()}))
    migrate_plain()
    return {"ok": True}


@app.post("/api/auth/unlock")
def auth_unlock(body: dict):
    global KEY
    if not meta().exists():
        raise HTTPException(400, "Not set up yet.")
    m = json.loads(meta().read_text())
    key = derive(str(body.get("password", "")), base64.b64decode(m["salt"]), m["n"], m["r"], m["p"])
    try:
        dec(base64.b64decode(m["check"]), key)
    except InvalidTag:
        time.sleep(1)  # slows down repeated guesses on top of the scrypt cost
        raise HTTPException(403, "Wrong password.")
    KEY = key
    return {"ok": True}


HYBRID_WORDS = ("ARBITRAGE", "HYBRID", "BALANCED", "MULTI ASSET", "ASSET ALLOCATION", "EQUITY SAVINGS", "DYNAMIC ASSET")
DEBT_WORDS = ("LIQUID", "OVERNIGHT", "GILT", "BOND", "MONEY MARKET", "CORPORATE", "TREASURY", "DEBT")


def classify(a):
    """Default asset class: equity / debt / gold / cash / other."""
    t = a.get("type")
    n = str(a.get("name", "")).upper()
    ins = str(a.get("instrument") or "").upper()
    if t == "sgb":
        return "gold"
    if t == "fixed_deposit":
        return "debt"
    if t == "savings":
        return "cash"
    if t == "stock":
        return "equity"
    if "GOLD" in n:
        return "gold"
    if ins.startswith("DEBT") or any(w in n for w in DEBT_WORDS):
        return "debt"
    if ins.startswith("HYBRID") or (not ins.startswith("EQUITY") and any(w in n for w in HYBRID_WORDS)):
        return "other"
    if t in ("etf", "mutual_fund"):
        return "equity"
    return "other"


COMPOUND = {"monthly": 12, "quarterly": 4, "half_yearly": 2, "yearly": 1}  # anything else = simple interest


def fd_value(a, today=None):
    """FD value on a date: principal grown at the stated rate from the start date (capped at maturity)."""
    try:
        start, p, r = date.fromisoformat(a["start"]), float(a["invested"]), float(a.get("rate") or 0) / 100
        end = today or date.today()
        if a.get("maturity"):
            end = min(end, date.fromisoformat(a["maturity"]))
    except (KeyError, ValueError, TypeError):
        return None
    t = max((end - start).days, 0) / 365
    n = COMPOUND.get(a.get("compounding", "quarterly"))
    return round(p * (1 + r * t) if not n else p * (1 + r / n) ** (n * t), 2)


def refresh_fd(a):
    if a.get("type") != "fixed_deposit":
        return
    if a.get("maturity"):
        a["matured"] = a["maturity"] < date.today().isoformat()
    if a.get("value_mode", "auto") != "auto":
        return
    v = fd_value(a)
    if v is None:  # no start date or principal: keep the value as entered
        return
    a["current_value"] = v
    a["matured"] = bool(a.get("maturity")) and a["maturity"] < date.today().isoformat()
    if a.get("maturity") and a.get("maturity_value_src") != "bank":
        a["maturity_value"] = fd_value(a, date.fromisoformat(a["maturity"]))


def snapshot(items):
    """Keep one snapshot per month; the latest one in the month wins."""
    if not items:
        return
    by = {}
    for i in items:
        by[i["asset_class"]] = round(by.get(i["asset_class"], 0) + i["current_value"], 2)
    snap = {"month": date.today().strftime("%Y-%m"), "date": date.today().isoformat(),
            "total": round(sum(by.values()), 2), "by_class": by}
    f = DATA / "snapshots.json"
    hist = read_json(f, [])
    old = next((h for h in hist if h["month"] == snap["month"]), None)
    if old and old["total"] == snap["total"] and old["by_class"] == by:
        return
    hist = sorted([h for h in hist if h["month"] != snap["month"]] + [snap], key=lambda h: h["month"])
    write_json(f, hist)


def load():
    items = read_json(FILE, [])
    for i in items:  # older records get a class on the fly
        i.setdefault("class_manual", False)
        i.setdefault("asset_class", classify(i))
        refresh_fd(i)  # FD values grow every day
    return items


def save(items):
    if FILE.exists():  # dated backup, keep last 30
        bk = DATA / "backups"
        bk.mkdir(exist_ok=True)
        shutil.copy(FILE, bk / f"assets-{datetime.now():%Y%m%d-%H%M%S-%f}.json")
        for old in sorted(bk.glob("assets-*.json"))[:-30]:
            old.unlink()
    write_json(FILE, items)


def norm(a):
    a = dict(a)
    for k in ("quantity", "avg_price", "price", "current_value", "invested", "rate"):
        try:
            a[k] = float(a.get(k) or 0)
        except ValueError:
            raise HTTPException(400, f"{k} must be a number")
    if not str(a.get("name", "")).strip():
        raise HTTPException(400, "name is required")
    a.setdefault("type", "other")
    if a.get("asset_class") in (None, "", "auto"):
        a["asset_class"] = classify(a)
        a["class_manual"] = False
    else:  # chosen by hand, so imports will not overwrite it
        a["class_manual"] = bool(a.get("class_manual", True))
    if a["quantity"] and a["price"]:
        a["current_value"] = round(a["quantity"] * a["price"], 2)
        if a["avg_price"]:
            a["invested"] = round(a["quantity"] * a["avg_price"], 2)
    refresh_fd(a)
    a["updated"] = datetime.now().isoformat(timespec="seconds")
    return a


def key(a):
    return a.get("ref") or a.get("isin") or f'{a["type"]}:{str(a["name"]).strip().lower()}'


# ---------- CRUD ----------
@app.get("/api/assets")
def list_assets():
    items = load()
    snapshot(items)
    return items


@app.get("/api/history")
def history():
    return read_json(DATA / "snapshots.json", [])


@app.post("/api/assets")
def add(a: dict):
    items = load()
    a = norm(a)
    a["id"] = uuid.uuid4().hex[:8]
    a.setdefault("source", "manual")
    items.append(a)
    save(items)
    return a


@app.put("/api/assets/{id}")
def update(id: str, a: dict):
    items = load()
    if a.get("asset_class") not in (None, "", "auto"):
        a["class_manual"] = True  # an explicit choice from the form
    a["maturity_value_src"] = ""  # a hand edit means the maturity amount is calculated again
    for i, it in enumerate(items):
        if it["id"] == id:
            items[i] = {**it, **norm({**it, **a}), "id": id}
            save(items)
            return items[i]
    raise HTTPException(404, "not found")


@app.delete("/api/assets/{id}")
def delete(id: str):
    items = load()
    save([i for i in items if i["id"] != id])
    return {"ok": True}


# ---------- Excel / CSV import ----------
ALIASES = {
    "name": ["symbol", "name", "scheme name", "fund name", "instrument"],
    "isin": ["isin"],
    "quantity": ["quantity available", "quantity", "qty", "units"],
    "avg_price": ["average price", "avg price", "avg. cost", "average cost"],
    "price": ["previous closing price", "closing price", "ltp", "nav", "price"],
    "current_value": ["current value", "value", "amount", "balance"],
    "invested": ["invested", "invested value", "principal"],
    "type": ["type", "asset type"],
    "sector": ["sector"],
    "instrument": ["instrument type"],
}
KNOWN = {n for v in ALIASES.values() for n in v}


def num(v):
    if isinstance(v, str):
        v = v.replace(",", "").replace("%", "").replace("₹", "").strip()
    v = pd.to_numeric(v, errors="coerce")
    return 0.0 if pd.isna(v) else float(v)


def parse_fd_summary(d):
    """ICICI 'FD account summary': merged cells, one FD per row. Returns [] if the sheet is not that format."""
    d = d.dropna(how="all").reset_index(drop=True)
    col = None
    for i in range(len(d)):
        cells = {j: str(v).strip().lower() for j, v in d.iloc[i].items() if not pd.isna(v)}
        if "fixed deposit number" in cells.values() and "maturity date" in cells.values():
            col = {v: j for j, v in cells.items()}
            break
    if col is None:
        return []
    c = lambda p: next((j for h, j in col.items() if h.startswith(p)), None)
    rows = []
    for k in range(i + 1, len(d)):
        r = d.iloc[k]
        g = lambda p: r.iloc[c(p)] if c(p) is not None else None
        no = g("fixed deposit number")
        if pd.isna(no):
            continue
        no = str(int(no)) if isinstance(no, (int, float)) and float(no).is_integer() else str(no).strip()
        md = g("maturity date")
        md = md.date() if hasattr(md, "date") else pd.to_datetime(str(md), errors="coerce")
        md = "" if pd.isna(md) else (md.date() if hasattr(md, "date") else md).isoformat()
        bal, acc = num(g("balance")), num(g("accrued interest"))
        rows.append({"name": f"ICICI FD {no}", "type": "fixed_deposit", "ref": f"ICICI-FD-{no}", "source": "excel",
                     "invested": bal, "current_value": round(bal + acc, 2), "rate": num(g("interest rate")),
                     "maturity": md, "maturity_value": num(g("maturity amount")), "maturity_value_src": "bank", "quantity": 0.0,
                     "avg_price": 0.0, "price": 0.0})
    return rows


def parse_soa_mf(d):
    """Mutual fund 'SOA summary': Scheme Name, Invested Value, Market Value, Gain/Loss, Balance Units, Nav Date."""
    d = d.dropna(how="all").reset_index(drop=True)
    hdr = lambda v: str(v).strip().lower()
    col = None
    for i in range(len(d)):
        cells = {j: hdr(v) for j, v in d.iloc[i].items() if not pd.isna(v)}
        pick = lambda p: next((j for j, h in cells.items() if h.startswith(p)), None)
        if "scheme name" in cells.values() and pick("invested value") is not None and pick("balance units") is not None:
            col = {"name": pick("scheme name"), "inv": pick("invested value"), "mkt": pick("market value"),
                   "units": pick("balance units"), "nav": pick("nav date"), "folio": pick("folio")}
            break
    if col is None:
        return []
    agg = {}  # the same scheme in two folios is one holding for net worth purposes
    for k in range(i + 1, len(d)):
        r = d.iloc[k]
        name = re.sub(r"\s+", " ", "" if pd.isna(r.iloc[col["name"]]) else str(r.iloc[col["name"]]).strip())
        if not name or name.lower().startswith("total"):
            continue
        units, inv = num(r.iloc[col["units"]]), num(r.iloc[col["inv"]])
        mkt = num(r.iloc[col["mkt"]]) if col["mkt"] is not None else 0.0
        if not units and not mkt:
            continue
        t = pd.to_datetime(r.iloc[col["nav"]], errors="coerce", dayfirst=True) if col["nav"] is not None else pd.NaT
        folio = ""
        if col["folio"] is not None and not pd.isna(r.iloc[col["folio"]]):
            f = r.iloc[col["folio"]]
            folio = str(int(f)) if isinstance(f, (int, float)) and float(f).is_integer() else str(f).strip()
        a = agg.setdefault((folio, name.lower()), {"name": name, "folio": folio, "units": 0.0, "inv": 0.0, "mkt": 0.0, "nav": ""})
        a["units"] += units
        a["inv"] += inv
        a["mkt"] += mkt
        a["nav"] = max(a["nav"], "" if pd.isna(t) else t.date().isoformat())
    rows = []
    for a in agg.values():
        u = a["units"]
        slug = re.sub(r"[^a-z0-9]+", "-", (a["folio"] + " " + a["name"]).lower()).strip("-")
        rows.append({"name": "SOA · " + a["name"], "type": "mutual_fund", "isin": a["folio"], "folio": a["folio"],
                     "ref": "SOA-MF-" + slug, "source": "excel",
                     "quantity": round(u, 4), "price": a["mkt"] / u if u else 0.0,
                     "avg_price": a["inv"] / u if u and a["inv"] else 0.0, "invested": a["inv"],
                     "current_value": round(a["mkt"], 2), "nav_date": a["nav"]})
    return rows


def parse_hdfc_fd(d):
    """HDFC Bank 'Fixed Deposit Summary': header row, then one FD per row (principal, rate, start and maturity dates)."""
    d = d.dropna(how="all").reset_index(drop=True)
    norm_h = lambda v: re.sub(r"[\s.]+$", "", str(v).strip().lower())
    col = None
    for i in range(len(d)):
        cells = {j: norm_h(v) for j, v in d.iloc[i].items() if not pd.isna(v)}
        if {"account no", "principal amount", "deposit start date"} <= set(cells.values()):
            col = {h: j for j, h in cells.items()}
            break
    if col is None:
        return []

    def day(v):
        if pd.isna(v):
            return ""
        t = pd.to_datetime(v, format="%d %b %Y", errors="coerce") if isinstance(v, str) else pd.to_datetime(v, errors="coerce")
        return "" if pd.isna(t) else t.date().isoformat()

    rows = []
    for k in range(i + 1, len(d)):
        r = d.iloc[k]
        g = lambda h: r.iloc[col[h]] if h in col else None
        acct = g("account no")
        acct = str(int(acct)) if isinstance(acct, (int, float)) and not pd.isna(acct) and float(acct).is_integer() else str(acct).strip()
        if not re.fullmatch(r"\d{6,}", acct):  # skips the ***** rows, totals and the disclaimers
            continue
        a = {"name": f"HDFC FD {acct}", "type": "fixed_deposit", "ref": f"HDFC-FD-{acct}", "source": "excel",
             "invested": num(g("principal amount")), "rate": num(g("rate of interest")),
             "start": day(g("deposit start date")), "maturity": day(g("maturity date")),
             "maturity_value": num(g("maturity amount")), "maturity_value_src": "bank",
             "compounding": "quarterly", "quantity": 0.0, "avg_price": 0.0, "price": 0.0}
        a["current_value"] = fd_value(a) or a["invested"]  # grown from the start date; recalculated daily
        rows.append(a)
    return rows


def parse_mf_statement(d):
    """Mutual fund 'Statement of Account' (SBI MF): a section per scheme + a Portfolio Summary with cost of investment."""
    d = d.dropna(how="all").reset_index(drop=True)
    txt = lambda v: "" if pd.isna(v) else str(v).strip()

    def after(r, label):  # the next value to the right of a label cell (merged cells can repeat the label)
        cells = [v for v in r if not pd.isna(v)]
        for n, v in enumerate(cells):
            if txt(v).lower() == label:
                for w in cells[n + 1:]:
                    if txt(w).lower() != label:
                        return w
        return None

    secs = []
    for i in range(len(d)):
        r = d.iloc[i]
        folio, isin = after(r, "folio number"), after(r, "isin")
        if folio is None or isin is None:
            continue
        name, nav = "", 0.0
        for k in range(i + 1, min(i + 5, len(d))):
            for v in d.iloc[k]:
                m = re.match(r"NAV as on .*?:\s*([\d,.]+)", txt(v), re.I)
                if m:
                    nav = num(m.group(1))
                elif txt(v) and not name:
                    name = txt(v)
            if name and nav:
                break
        folio = str(int(folio)) if isinstance(folio, (int, float)) and float(folio).is_integer() else txt(folio)
        secs.append({"folio": folio, "isin": txt(isin), "units": num(after(r, "clear unit")), "nav": nav, "name": name})
    if not secs:
        return []
    cost = {}  # scheme code -> (units, cost of investment) from the Portfolio Summary
    for i in range(len(d)):
        if any(txt(v).lower() == "portfolio summary" for v in d.iloc[i]) and i + 1 < len(d):
            col = {}
            for j, v in d.iloc[i + 1].items():
                if not pd.isna(v):
                    col.setdefault(txt(v).lower(), j)  # first cell of a merged range
            if not {"scheme", "unit balance", "cost of investment"} <= set(col):
                continue
            for k in range(i + 2, len(d)):
                r = d.iloc[k]
                sch = txt(r.iloc[col["scheme"]])
                if not sch or sch.lower() == "total":
                    break
                code = sch.split()[0]
                code = code[1:] if code.startswith("L") else code
                cost[code] = (num(r.iloc[col["unit balance"]]), num(r.iloc[col["cost of investment"]]))
            break
    rows = []
    for sc in secs:
        parts = sc["name"].split()
        code = parts[0] if parts and any(ch.isdigit() for ch in parts[0]) else ""
        name = re.sub(r"\s+", " ", " ".join(parts[1:] if code else parts))
        units, c = sc["units"], (cost.get(code) or (0, 0))[1]
        amc = re.sub(r"\W", "", name.split()[0].upper()) if name else "MF"
        rows.append({"name": name, "type": "mutual_fund", "isin": sc["isin"], "folio": sc["folio"],
                     "ref": f"{amc}-MF-{sc['folio']}-{sc['isin']}", "source": "excel", "quantity": units,
                     "price": sc["nav"], "avg_price": c / units if units and c else 0.0, "invested": c,
                     "current_value": round(units * sc["nav"], 2)})
    return rows


def parse(raw: bytes, filename: str):
    if filename.lower().endswith(".csv"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        lines = list(csv.reader(io.StringIO(text)))  # tolerates title lines and ragged rows
        width = max((len(r) for r in lines), default=0)
        df = pd.DataFrame([r + [None] * (width - len(r)) for r in lines], dtype=object)
        sheets = {"csv": df.where(df != "", None)}
    else:
        sheets = pd.read_excel(io.BytesIO(raw), header=None, sheet_name=None)
    rows, covered = [], set()
    for sname, d in sheets.items():
        if "combined" in str(sname).lower():  # repeats the other sheets; would double count
            continue
        fd = parse_fd_summary(d)
        if fd:
            rows += fd
            covered.add("fixed_deposit")
            continue
        so = parse_soa_mf(d)
        if so:
            rows += so
            covered.add("mutual_fund")
            continue
        hd = parse_hdfc_fd(d)
        if hd:
            rows += hd
            covered.add("fixed_deposit")
            continue
        mf = parse_mf_statement(d)
        if mf:
            rows += mf
            covered.add("mutual_fund")
            continue
        sn = str(sname).lower()
        if "mutual" in sn:
            covered.add("mutual_fund")
        elif "equity" in sn:
            covered |= {"stock", "etf", "sgb"}
        d = d.dropna(how="all").reset_index(drop=True)
        # the header row is the first row containing 2+ recognised column names
        hi = next((i for i in range(len(d)) if sum(str(c).strip().lower() in KNOWN for c in d.iloc[i]) >= 2), None)
        if hi is None:
            continue
        hdr = [str(c).strip().lower() for c in d.iloc[hi]]
        qcols = [i for i, h in enumerate(hdr) if h.startswith("quantity") and "long term" not in h]
        col = {f: next((hdr.index(n) for n in names if n in hdr), None) for f, names in ALIASES.items()}
        for i in range(hi + 1, len(d)):
            r = d.iloc[i]
            get = lambda f: r.iloc[col[f]] if col[f] is not None else None
            name = get("name")
            if pd.isna(name) or not str(name).strip():
                continue
            isin = "" if pd.isna(get("isin")) else str(get("isin")).strip()
            t = str(get("type") or "").strip().lower().replace(" ", "_")
            if not t or t == "nan":
                sec = "" if pd.isna(get("sector")) else str(get("sector")).strip().upper()
                if "mutual" in str(sname).lower() or (not sec and isin.startswith("INF") and "equity" not in str(sname).lower()):
                    t = "mutual_fund"
                elif sec == "ETF":
                    t = "etf"
                elif isin.startswith("IN00"):  # sovereign gold bonds
                    t = "sgb"
                elif isin.startswith(("INE", "INF")):
                    t = "stock"
                else:
                    t = "other"
            a = {"name": str(name).strip(), "type": t, "isin": isin, "source": "excel",
                 **{f: num(get(f)) for f in ("quantity", "avg_price", "price", "current_value", "invested")}}
            ins = get("instrument")
            if ins is not None and not pd.isna(ins):
                a["instrument"] = str(ins).strip()
            if qcols and col["quantity"] is not None:  # available + discrepant + pledged
                a["quantity"] = round(sum(num(r.iloc[i]) for i in qcols), 4)
            if a["quantity"] or a["current_value"]:
                rows.append(a)
    covered |= {r["type"] for r in rows}
    return rows, covered


CMP = ("name", "quantity", "avg_price", "price", "current_value", "invested", "rate", "maturity", "maturity_value")


def unchanged(new, old):
    for f in CMP:
        x, y = new.get(f), old.get(f)
        if f in ("maturity", "name"):
            if (x or "") != (y or ""):
                return False
        elif round(float(x or 0), 2) != round(float(y or 0), 2):
            return False
    return True


@app.post("/api/import/preview")
async def preview(file: UploadFile = File(...)):
    try:
        rows, covered = parse(await file.read(), file.filename or "")
    except Exception as e:
        raise HTTPException(400, f"Could not read file: {e}")
    if not rows:
        raise HTTPException(400, "No holdings found. Expected a header row with columns like Symbol, ISIN, Quantity, Average Price.")
    uniq = {}  # the same holding twice in one file: keep the last one
    for r in rows:
        uniq[key(r)] = r
    dupes, rows = len(rows) - len(uniq), list(uniq.values())
    items = load()
    existing = {key(i): i for i in items}
    for r in rows:
        old = existing.get(key(r))
        r["status"] = "new" if old is None else "same" if unchanged(norm(dict(r)), old) else "update"
    noise = {"direct", "plan", "growth", "option", "fund", "regular", "soa"}
    toks = lambda n: set(re.findall(r"[a-z0-9]+", n.lower())) - noise
    held = [i for i in items if i["type"] == "mutual_fund"]
    for r in rows:  # rows without an ISIN can only be matched by name, so warn about funds that look the same
        if r["type"] == "mutual_fund" and (not r.get("isin") or str(r.get("ref", "")).startswith("SOA-")) and r["status"] == "new":
            a = toks(r["name"])
            for i in held:
                b = toks(i["name"])
                if key(i) != key(r) and a and b and len(a & b) / len(a | b) >= 0.8:
                    r["similar"] = i["name"]
                    break
    seen = set(uniq)
    # holdings from earlier imports of this kind of file that are not in it any more
    refp = tuple({"-".join(r["ref"].split("-")[:2]) + "-" for r in rows if r.get("ref")})

    def in_scope(i):
        ref = str(i.get("ref", ""))
        return ref.startswith(refp) if refp else (not ref and i["type"] in covered)

    scope = [i for i in items if i.get("source") == "excel" and in_scope(i)]
    missing = [i for i in scope if key(i) not in seen]
    warn = len(missing) > max(3, 0.3 * len(scope))
    return {"rows": rows, "missing": missing, "warn": warn, "dupes": dupes}


@app.post("/api/import/commit")
def commit(body: dict):
    rows, remove = body.get("rows", []), set(body.get("remove", []))
    items = [i for i in load() if not (i["id"] in remove and i.get("source") == "excel")]  # never removes manual rows
    by_key = {key(i): n for n, i in enumerate(items)}
    added = updated = skipped = 0
    for r in rows:
        if r.pop("status", None) == "same":  # already stored exactly like this
            skipped += 1
            continue
        r = norm(r)
        k = key(r)
        if k in by_key:  # update in place, keep id and manual fields
            n = by_key[k]
            for f in ("isin", "folio"):
                if not r.get(f):
                    r.pop(f, None)  # the sheet has none: keep what is stored
            if items[n].get("class_manual"):
                r["asset_class"], r["class_manual"] = items[n]["asset_class"], True
            items[n] = {**items[n], **r, "id": items[n]["id"]}
            updated += 1
        else:
            r["id"] = uuid.uuid4().hex[:8]
            items.append(r)
            by_key[k] = len(items) - 1
            added += 1
    save(items)
    return {"added": added, "updated": updated, "skipped": skipped, "removed": len(remove)}


@app.post("/api/shutdown")
def shutdown(request: Request):
    # the custom header blocks other websites from stopping the app (browsers preflight it)
    if request.headers.get("x-app") != "networth":
        raise HTTPException(403, "forbidden")
    if server is None:
        raise HTTPException(400, "Start the app with python app.py (or run.bat) to enable this.")
    server.should_exit = True  # graceful stop after this response is sent
    return {"ok": True}


# friendly page URLs (/holdings, /history ...) all serve the same single-page app
for _p in ("dashboard", "holdings", "analytics", "history", "import", "settings"):
    app.add_api_route(f"/{_p}", lambda: FileResponse(BASE / "static" / "index.html"), include_in_schema=False)

app.mount("/", StaticFiles(directory=BASE / "static", html=True))

def open_browser_when_ready(url="http://networth.localhost:8000", port=8000):
    """Wait until the server is accepting connections, then open the browser."""
    def wait_then_open():
        for _ in range(240):  # give up after about two minutes
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            return
        time.sleep(0.3)
        webbrowser.open(url)
    threading.Thread(target=wait_then_open, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000))
    if not os.environ.get("NETWORTH_NO_BROWSER"):
        open_browser_when_ready()
    server.run()
