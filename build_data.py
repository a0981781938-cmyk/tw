#!/usr/bin/env python3
"""台股強勢雷達：資料與策略腳本（只用標準函式庫）。

用法
  python build_data.py            抓證交所上市日行情，產生 data.json
  python build_data.py --demo     用模擬資料產生 data.json，用來測試版面與策略邏輯

輸出
  data.json                網頁讀取的結果
  data/history.json        上市普通股日 K 快取（只保留最近 HISTORY_DAYS 個交易日）
  data/signal_log.json     訊號紀錄（每次執行追加新訊號，並更新舊訊號的後續表現）
  data/exclude.txt         （選填）自行補充要排除的代號，一行一個
"""
import argparse, datetime as dt, json, math, os, random, re, sys, time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

# ---------- 參數 ----------
HISTORY_DAYS = 60          # 需要的交易日數（20MA、斐波那契區間、平台都用得到）
MIN_BARS = 40              # 至少要有幾根日 K 才分析
MIN_PRICE = 10.0           # 股價低於此值不看（全額交割股多半在此以下）
MIN_AVG_VALUE = 1.0e8      # 20 日均成交金額下限（新台幣）
MIN_AVG_LOTS = 1500        # 20 日均量下限（張）
MAX_RISK = 0.07            # 持有 2–15 天，單筆止損距離上限
HOLD_MIN, HOLD_MAX = 2, 15
MIN_RR = 1.5               # 風險報酬比達此值才標示為「可規劃」
LOG_KEEP_DAYS = 120
NO_FILL_DAYS = 5

UA = {"User-Agent": "Mozilla/5.0 (tw-radar)"}


# ---------- 小工具 ----------
def avg(a):
    return sum(a) / len(a) if a else 0.0


def tick(p):
    return 0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100 else 0.5 if p < 500 else 1 if p < 1000 else 5


def r_near(p):
    t = tick(p)
    return round(round(p / t) * t, 2)


def r_up(p):
    t = tick(p)
    return round(math.ceil(p / t - 1e-9) * t, 2)


def r_down(p):
    t = tick(p)
    return round(math.floor(p / t + 1e-9) * t, 2)


def sma_at(a, n, i):
    if i < 0:
        i += len(a)
    if i - n + 1 < 0:
        return None
    return sum(a[i - n + 1:i + 1]) / n


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def get_json(url, tries=3, wait=5):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (k + 1))
    raise last


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj, compact=True):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":") if compact else None, indent=None if compact else 1)


# ---------- 抓資料 ----------
def is_common_stock(code):
    """上市普通股：4 位數字，排除 ETF(00 開頭)、存託憑證(91 開頭)。"""
    return bool(re.fullmatch(r"\d{4}", code)) and not code.startswith("00") and not code.startswith("91")


def fetch_day(d):
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=%s&type=ALLBUT0999&response=json" % d.strftime("%Y%m%d")
    j = get_json(url)
    if j.get("stat") != "OK":
        return None, {}
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ix = {k: f.index(k) for k in f}
            out, names = {}, {}
            for r in t.get("data") or []:
                code = str(r[ix["證券代號"]]).strip()
                if not is_common_stock(code):
                    continue
                o, h, l, c = (num(r[ix[k]]) for k in ("開盤價", "最高價", "最低價", "收盤價"))
                v, a = num(r[ix["成交股數"]]), num(r[ix["成交金額"]])
                if None in (o, h, l, c, v, a) or c <= 0 or v <= 0:
                    continue
                out[code] = [o, h, l, c, v, a]
                names[code] = str(r[ix["證券名稱"]]).strip()
            return out, names
    return None, {}


def collect_history(cache, today):
    days, hol, names = cache.setdefault("days", {}), cache.setdefault("holidays", []), cache.setdefault("names", {})
    d, tries = today, 0
    have = lambda: len([k for k in days])  # noqa: E731
    while have() < HISTORY_DAYS and tries < 130:
        tries += 1
        key = d.strftime("%Y-%m-%d")
        if d.weekday() < 5 and key not in days and key not in hol:
            data, nm = fetch_day(d)
            if data:
                days[key] = data
                names.update(nm)
                print("fetched", key, len(data), file=sys.stderr)
            elif d < today:
                hol.append(key)
            time.sleep(3.2)  # 證交所對連續請求有限流
        d -= dt.timedelta(days=1)
    for k in sorted(days)[:-HISTORY_DAYS]:
        del days[k]
    return cache


def extract_codes(j):
    codes = set()
    rows = j if isinstance(j, list) else (j.get("data") or [])
    for r in rows:
        if isinstance(r, dict):
            for k in ("Code", "證券代號", "公司代號", "SecuritiesCode"):
                if k in r and re.fullmatch(r"\d{4}", str(r[k]).strip()):
                    codes.add(str(r[k]).strip())
        elif isinstance(r, list) and r:
            for cell in r[:3]:
                if re.fullmatch(r"\d{4}", str(cell).strip()):
                    codes.add(str(cell).strip())
                    break
    return codes


# 處置股、注意股取自證交所 OpenAPI。全額交割／變更交易的端點請依證交所現況確認後填入 EXTRA_URLS。
EXCLUDE_SOURCES = {
    "punish": "https://openapi.twse.com.tw/v1/announcement/punish",
    "attention": "https://openapi.twse.com.tw/v1/announcement/notice",
}
EXTRA_URLS = {
    # "full_delivery": "https://...",   # 全額交割股名單
}


def fetch_excluded():
    ex, status = {"punish": set(), "attention": set(), "full_delivery": set(), "manual": set()}, {}
    for key, url in {**EXCLUDE_SOURCES, **EXTRA_URLS}.items():
        try:
            ex[key] = extract_codes(get_json(url))
            status[key] = "ok:%d" % len(ex[key])
        except Exception as e:  # noqa: BLE001
            status[key] = "fail"
            print("exclude source failed", key, e, file=sys.stderr)
    if "full_delivery" not in EXTRA_URLS:
        status["full_delivery"] = "not_configured"
    p = os.path.join(DATA, "exclude.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            ex["manual"] = {x.strip() for x in f if re.fullmatch(r"\d{4}", x.strip())}
        status["manual"] = "ok:%d" % len(ex["manual"])
    return ex, status


# ---------- 指標 ----------
def features(S):
    c, h, l, o, v, a = S["c"], S["h"], S["l"], S["o"], S["v"], S["a"]
    n = len(c)
    i = n - 1
    f = {"close": c[-1], "prev": c[-2], "pct": (c[-1] / c[-2] - 1) * 100}
    f["ma5"], f["ma20"] = sma_at(c, 5, i), sma_at(c, 20, i)
    f["ma5p"], f["ma20_3"] = sma_at(c, 5, i - 1), sma_at(c, 20, i - 3)
    f["slope"] = f["ma20"] / f["ma20_3"] - 1
    f["vr5"] = v[-1] / (avg(v[-6:-1]) or 1)
    f["vr20"] = v[-1] / (avg(v[-21:-1]) or 1)
    f["vchg"] = (v[-1] / v[-2] - 1) if v[-2] else 0.0
    f["avgval20"], f["avglots20"] = avg(a[-20:]), avg(v[-20:]) / 1000
    rng = h[-1] - l[-1]
    f["pos"] = (c[-1] - l[-1]) / rng if rng > 0 else (1.0 if c[-1] >= c[-2] else 0.5)
    f["ret5"], f["ret20"] = c[-1] / c[-6] - 1, c[-1] / c[-21] - 1
    trs = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(n - 14, n)]
    f["atr"] = max(avg(trs), tick(c[-1]))
    s = 0.0
    s += 15 if c[-1] > f["ma5"] else 0
    s += 15 if f["ma5"] > f["ma20"] else 0
    s += 15 if f["slope"] > 0 else 0
    s += 20 * min(max((c[-1] / f["ma20"] - 1) / 0.10, 0), 1)
    s += 20 * min(max(f["ret20"] / 0.25, 0), 1)
    s += 15 * f["pos"]
    f["trend"] = round(s)
    f["strong"] = round(0.40 * f["trend"] + 0.30 * min(max(f["pct"], 0) / 9.5, 1) * 100 + 0.30 * min(f["vr5"] / 3, 1) * 100)
    return f


def crossed_up(c, i):
    ma, mp = sma_at(c, 20, i), sma_at(c, 20, i - 1)
    return c[i] > ma and c[i - 1] <= mp


def golden(c, i):
    return sma_at(c, 5, i) > sma_at(c, 20, i) and sma_at(c, 5, i - 1) <= sma_at(c, 20, i - 1)


def make_plan(f, entry, struct_stop, target, note=""):
    entry = r_near(entry)
    stop, capped = struct_stop, False
    if entry - stop > entry * MAX_RISK:
        stop, capped = entry * (1 - MAX_RISK), True
    stop = r_down(stop)
    if stop >= entry:
        return None
    risk = entry - stop
    t1 = r_near(max(target, entry + risk))
    t2 = r_near(max(t1, entry + 3 * risk))
    rr = (t1 - entry) / risk
    days = int(min(HOLD_MAX, max(HOLD_MIN, round((t1 - entry) / (f["atr"] * 0.6)))))
    return {
        "entry": entry, "stop": stop, "t1": t1, "t2": t2, "rr": round(rr, 2),
        "risk_pct": round(risk / entry * 100, 1), "hold": days, "capped": capped,
        "ok": rr >= MIN_RR, "note": note,
    }


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


# ---------- 策略 A：均線突破（5MA、20MA、成交量） ----------
def strat_A(f, S):
    c, h, l, v = S["c"], S["h"], S["l"], S["v"]
    i = len(c) - 1
    ma5, ma20, slope = f["ma5"], f["ma20"], f["slope"]
    cross = any(crossed_up(c, i - k) for k in range(3))
    gold = any(golden(c, i - k) for k in range(5))
    close = c[-1]
    if close > ma5 and close > ma20 and slope >= 0 and (cross or gold) and f["vr5"] >= 1.3 and close / ma20 - 1 <= 0.10 and f["ret5"] <= 0.12:
        tags = ["站上20MA" if cross else "5MA黃金交叉", "量比%.1f" % f["vr5"]]
        stop = max(ma20 * 0.99, min(l[-3:]) * 0.995)
        hi20 = max(h[-20:])
        target = max(close + 2 * (close - stop), hi20 if hi20 > close * 1.02 else 0)
        sc = 50 + min((f["vr5"] - 1.3) * 15, 20) + clamp(slope * 1000, 0, 10) + (10 if f["pos"] >= 0.7 else 0) + (10 if cross and gold else 0)
        return {"type": "breakout", "score": round(clamp(sc)), "tags": tags, "plan": make_plan(f, close, stop, target, "跌回20MA下方視為失敗")}
    past = any(crossed_up(c, i - k) for k in range(3, 13))
    if past and ma20 * 0.995 <= close <= ma20 * 1.03 and min(l[-2:]) <= ma20 * 1.01 and slope > 0 and v[-1] < avg(v[-6:-1]) * 0.9:
        sc = 55 + clamp(slope * 1000, 0, 10) + (10 if v[-1] < avg(v[-6:-1]) * 0.7 else 0) + (10 if f["ma5"] > ma20 else 0)
        return {"type": "pullback", "score": round(clamp(sc)), "tags": ["回測20MA", "量縮%.0f%%" % (v[-1] / avg(v[-6:-1]) * 100)],
                "plan": make_plan(f, close, ma20 * 0.985, max(h[-12:]), "收盤跌破20MA即出場")}
    if 0.97 <= close / ma20 < 1 and f["ma5"] > f["ma5p"] and slope >= -0.002 and f["ma5"] >= ma20 * 0.98:
        gap = (1 - close / ma20) * 100
        sc = 45 + (10 - gap * 3) + (10 if f["ma5"] > f["ma5p"] * 1.002 else 0) + (10 if f["vr5"] >= 1 else 0)
        entry = ma20 * 1.01
        return {"type": "pre", "score": round(clamp(sc)), "tags": ["距20MA %.1f%%" % gap, "5MA上彎"],
                "plan": make_plan(f, entry, min(l[-3:]) * 0.99, max(h[-20:]), "站上觸發價且放量再進場")}
    return None


# ---------- 策略 B：結構突破（支撐壓力、平台、回踩、較高低點） ----------
def platform(h, l, end):
    """end 為平台最後一根（含）的索引；回傳 (壓力, 支撐, 寬度比)。"""
    hh, ll = max(h[end - 14:end + 1]), min(l[end - 14:end + 1])
    return hh, ll, (hh - ll) / ((hh + ll) / 2)


def strat_B(f, S):
    c, h, l, v = S["c"], S["h"], S["l"], S["v"]
    n = len(c)
    i = n - 1
    close = c[-1]
    ph, pl, width = platform(h, l, i - 1)
    L1, L2, L3 = min(l[-5:]), min(l[-10:-5]), min(l[-15:-10])
    higher_lows = L1 > L2 and L2 >= L3 * 0.98
    vr20 = f["vr20"]
    if close > ph and vr20 >= 1.3 and f["pos"] >= 0.6 and close <= ph * 1.05 and f["ret5"] <= 0.12 and width <= 0.18:
        tags = ["突破平台壓力%.1f" % ph, "量比%.1f" % vr20] + (["較高低點"] if higher_lows else [])
        stop = max(ph * 0.985, min(l[-3:]) * 0.995)
        sc = 55 + min((vr20 - 1.3) * 15, 15) + (10 if higher_lows else 0) + (10 if width <= 0.12 else 0) + (10 if f["pos"] >= 0.8 else 0)
        return {"type": "breakout", "score": round(clamp(sc)), "tags": tags, "level": ph,
                "plan": make_plan(f, close, stop, ph + (ph - pl), "跌回平台上緣下方視為假突破")}
    for k in range(2, 11):
        j = i - k
        if j - 15 < 0:
            break
        rh, rl, rw = platform(h, l, j - 1)
        if c[j] > rh and v[j] >= avg(v[j - 20:j]) * 1.3 and rw <= 0.18:
            near = min(l[-3:]) <= rh * 1.02 and close >= rh * 0.99 and close <= rh * 1.05
            shrink = avg(v[-3:]) < v[j] * 0.8
            if near and shrink:
                sc = 55 + (10 if higher_lows else 0) + (10 if avg(v[-3:]) < v[j] * 0.6 else 0) + (10 if close >= rh else 0) + (5 if rw <= 0.12 else 0)
                stop = min(rh * 0.98, min(l[-3:]) * 0.995)
                return {"type": "pullback", "score": round(clamp(sc)), "tags": ["突破後回踩%.1f" % rh, "量縮", "%d日前突破" % k] + (["較高低點"] if higher_lows else []),
                        "level": rh, "plan": make_plan(f, close, stop, max(h[j:]) if max(h[j:]) > close * 1.02 else rh + (rh - rl), "收盤跌回壓力轉支撐下方即出場")}
            break
    if ph * 0.97 <= close < ph and width <= 0.15 and higher_lows and close > f["ma20"]:
        gap = (ph / close - 1) * 100
        contracting = avg(v[-5:]) <= avg(v[-20:])
        sc = 50 + (10 - gap * 2.5) + (10 if contracting else 0) + (10 if width <= 0.10 else 0) + (5 if f["ma5"] > f["ma20"] else 0)
        entry = ph * 1.003
        return {"type": "pre", "score": round(clamp(sc)), "tags": ["距壓力%.1f %.1f%%" % (ph, gap), "較高低點"] + (["量縮整理"] if contracting else []), "level": ph,
                "plan": make_plan(f, entry, max(min(l[-5:]) * 0.99, pl), ph + (ph - pl), "站上壓力且放量再進場")}
    return None


# ---------- 策略 C：綜合（均線、成交量、支撐壓力、斐波那契） ----------
def fib_info(S, close):
    h, l = S["h"][-40:], S["l"][-40:]
    hi_i = max(range(len(h)), key=lambda k: h[k])
    if hi_i < 5:
        return None
    lo_i = min(range(hi_i), key=lambda k: l[k])
    hi, lo = h[hi_i], l[lo_i]
    move = hi - lo
    if move <= 0 or move / lo < 0.08:
        return None
    lv = {"0.382": hi - 0.382 * move, "0.5": hi - 0.5 * move, "0.618": hi - 0.618 * move}
    return {"hi": hi, "lo": lo, "retr": (hi - close) / move, "levels": lv, "ext": lo + 1.272 * move, "age": len(h) - 1 - hi_i}


def strat_C(f, S, a, b):
    c, h, l, v = S["c"], S["h"], S["l"], S["v"]
    close = c[-1]
    fib = fib_info(S, close)
    ma_s = (8 if close > f["ma20"] else 0) + (7 if f["ma5"] > f["ma20"] else 0) + (5 if f["slope"] > 0 else 0) + (5 if a else 0)
    up_v = sum(v[k] for k in range(-10, 0) if c[k] >= c[k - 1])
    dn_v = sum(v[k] for k in range(-10, 0) if c[k] < c[k - 1])
    vol_s = min(f["vr5"] / 2, 1) * 12 + (8 if up_v > dn_v * 1.2 else 0)
    typ = (b or a or {}).get("type")
    st_s = {"breakout": 30, "pullback": 26, "pre": 22}.get(typ, 8)
    fib_s, fib_tag, fib_zone = 0, None, False
    if fib:
        r = fib["retr"]
        for name, price in fib["levels"].items():
            if abs(close / price - 1) <= 0.015 and min(l[-3:]) >= fib["lo"] and f["slope"] > 0:
                fib_s, fib_tag, fib_zone = 25, "回撤%s %.1f" % (name, price), True
                break
        if not fib_s:
            if r < 0.236:
                fib_s, fib_tag = 15, "接近前高"
            elif r < 0.382:
                fib_s, fib_tag = 18, "回撤<0.382"
    total = ma_s + vol_s + st_s + fib_s
    if not typ and fib_zone and close > f["ma20"] * 0.99:
        typ = "pullback"
    if not typ or total < 55:
        return None
    base = b or a
    tags = ["綜合%d" % total] + ([fib_tag] if fib_tag else [])
    if b:
        tags += [t for t in b["tags"][:2]]
    if a and a is not base:
        tags += a["tags"][:1]
    if typ == "breakout":
        entry = close
        stop = max(f["ma20"] * 0.99, min(l[-3:]) * 0.995, (b or {}).get("level", 0) * 0.985)
        target = fib["ext"] if fib and fib["ext"] > close * 1.03 else close + 2 * (close - stop)
    elif typ == "pullback":
        entry = close
        if fib_zone:
            lvl = [p for p in fib["levels"].values() if abs(close / p - 1) <= 0.015][0]
            stop = min(lvl * 0.985, min(l[-3:]) * 0.995)
        else:
            stop = (base or {}).get("plan", {}).get("stop") if (base or {}).get("plan") else min(l[-3:]) * 0.99
        target = fib["hi"] if fib and fib["hi"] > close * 1.02 else close + 2 * (close - stop)
    else:
        entry = (base["plan"]["entry"] if base and base.get("plan") else close * 1.01)
        stop = min(l[-5:]) * 0.99
        target = fib["ext"] if fib and fib["ext"] > entry * 1.03 else entry * 1.08
    out = {"type": typ, "score": round(clamp(total)), "tags": tags[:5], "plan": make_plan(f, entry, stop, target, "綜合結構失守即出場")}
    if fib:
        out["fib"] = {"hi": round(fib["hi"], 2), "lo": round(fib["lo"], 2), "l382": round(fib["levels"]["0.382"], 2),
                      "l5": round(fib["levels"]["0.5"], 2), "l618": round(fib["levels"]["0.618"], 2), "ext": round(fib["ext"], 2)}
    return out


def slim(sig):
    if not sig:
        return None
    return sig


# ---------- 整體分析 ----------
def series_for(days_sorted, days, code, upto):
    S = {k: [] for k in "ohlcva"}
    for d in days_sorted[: upto + 1]:
        r = days[d].get(code)
        if not r:
            continue
        for k, x in zip("ohlcva", r):
            S[k].append(x)
    return S


def analyse(hist, ex, upto=None):
    days = hist["days"]
    ds = sorted(days)
    upto = len(ds) - 1 if upto is None else upto
    last = ds[upto]
    skip = {"low_volume": 0, "low_price": 0, "punish": 0, "full_delivery": 0, "manual": 0, "short_history": 0}
    stocks, scanned = [], 0
    for code in days[last]:
        scanned += 1
        S = series_for(ds, days, code, upto)
        if len(S["c"]) < MIN_BARS:
            skip["short_history"] += 1
            continue
        if code in ex["punish"]:
            skip["punish"] += 1
            continue
        if code in ex["full_delivery"]:
            skip["full_delivery"] += 1
            continue
        if code in ex["manual"]:
            skip["manual"] += 1
            continue
        if S["c"][-1] < MIN_PRICE:
            skip["low_price"] += 1
            continue
        f = features(S)
        if f["avgval20"] < MIN_AVG_VALUE or f["avglots20"] < MIN_AVG_LOTS:
            skip["low_volume"] += 1
            continue
        A, B = strat_A(f, S), strat_B(f, S)
        C = strat_C(f, S, A, B)
        stocks.append({
            "code": code, "name": hist["names"].get(code, code), "close": f["close"], "pct": round(f["pct"], 2),
            "val": S["a"][-1], "vol": S["v"][-1], "vr5": round(f["vr5"], 2), "vchg": round(f["vchg"] * 100, 1),
            "avgval20": round(f["avgval20"]), "ma5": round(f["ma5"], 2), "ma20": round(f["ma20"], 2),
            "trend": f["trend"], "strong": f["strong"], "ret5": round(f["ret5"] * 100, 1), "ret20": round(f["ret20"] * 100, 1),
            "attention": code in ex["attention"], "sig": {"A": A, "B": B, "C": C},
        })
    up = sum(1 for s in stocks if s["pct"] > 0)
    meta = {"date": last, "scanned": scanned, "passed": len(stocks), "skipped": skip, "up": up, "down": sum(1 for s in stocks if s["pct"] < 0)}
    return stocks, meta, ds[upto]


# ---------- 訊號紀錄 ----------
def simulate(entry_row, bars):
    """bars：訊號日之後的日 K（含 date,o,h,l,c）。回傳狀態與損益。"""
    e, st, tg, typ = entry_row["entry"], entry_row["stop"], entry_row["t1"], entry_row["type"]
    filled, fill_i, fill_px = False, None, None
    out = {"status": "pending", "days": 0, "ret": None, "max_gain": None, "max_dd": None, "exit": None}
    peak, trough = None, None
    for k, b in enumerate(bars):
        if not filled:
            if k >= NO_FILL_DAYS:
                out["status"] = "no_fill"
                return out
            if typ == "pullback":
                if b["l"] <= e:
                    filled, fill_px = True, min(b["o"], e)
            else:
                if b["h"] >= e:
                    filled, fill_px = True, max(b["o"], e)
            if not filled:
                continue
            fill_i = k
            peak, trough = fill_px, fill_px
        held = k - fill_i + 1
        peak, trough = max(peak, b["h"]), min(trough, b["l"])
        out.update(days=held, max_gain=round((peak / fill_px - 1) * 100, 1), max_dd=round((trough / fill_px - 1) * 100, 1))
        if b["l"] <= st:
            px = st if b["o"] > st else b["o"]
            out.update(status="stopped", exit=round(px, 2), ret=round((px / fill_px - 1) * 100, 1))
            return out
        if b["h"] >= tg:
            px = tg if b["o"] < tg else b["o"]
            out.update(status="target", exit=round(px, 2), ret=round((px / fill_px - 1) * 100, 1))
            return out
        if held >= HOLD_MAX:
            out.update(status="expired", exit=round(b["c"], 2), ret=round((b["c"] / fill_px - 1) * 100, 1))
            return out
        out.update(status="holding", ret=round((b["c"] / fill_px - 1) * 100, 1))
    return out


def update_log(log, stocks, hist, signal_date):
    days = hist["days"]
    ds = sorted(days)
    have = {(x["date"], x["code"], x["strategy"]) for x in log}
    recent = {(x["code"], x["strategy"]): x["date"] for x in log}
    for s in stocks:
        for strat in ("A", "B", "C"):
            sg = s["sig"][strat]
            if not sg or not sg.get("plan"):
                continue
            key = (signal_date, s["code"], strat)
            prev = recent.get((s["code"], strat))
            if key in have or (prev and (dt.date.fromisoformat(signal_date) - dt.date.fromisoformat(prev)).days < 10):
                continue
            p = sg["plan"]
            log.append({"date": signal_date, "code": s["code"], "name": s["name"], "strategy": strat, "type": sg["type"], "score": sg["score"],
                        "ref": s["close"], "entry": p["entry"], "stop": p["stop"], "t1": p["t1"], "rr": p["rr"], "ok": p["ok"], "tags": sg["tags"][:3]})
    for x in log:
        later = [d for d in ds if d > x["date"]]
        bars = []
        for d in later:
            r = days[d].get(x["code"])
            if r:
                bars.append({"d": d, "o": r[0], "h": r[1], "l": r[2], "c": r[3]})
        res = simulate(x, bars)
        x.update(res)
        x["ref_ret"] = round((bars[-1]["c"] / x["ref"] - 1) * 100, 1) if bars else None
        x["bars"] = len(bars)
    cutoff = (dt.date.fromisoformat(signal_date) - dt.timedelta(days=LOG_KEEP_DAYS)).isoformat()
    log[:] = [x for x in log if x["date"] >= cutoff]
    log.sort(key=lambda x: (x["date"], x["score"]), reverse=True)
    return log


# ---------- 模擬資料（--demo） ----------
DEMO_NAMES = ["示範半導體", "範例光電", "樣本伺服器", "假想散熱", "測試航運", "示例電池", "模擬生技", "虛構網通", "範本材料", "示範記憶體",
              "樣品機器人", "假設鋼鐵", "示意電動車", "測試軟體", "範例封測", "虛擬載板", "模型工具機", "示例金融", "假想營建", "樣本食品",
              "測試紡織", "範例塑化", "示範電源", "假想連接器", "樣本被動元件", "測試面板", "範例光學", "虛構汽車零件", "示意水泥", "模擬造紙",
              "示範保險", "範例百貨", "假想觀光", "樣本通路", "測試半導體設備", "範本印刷電路", "示例伺服器機殼", "虛擬雲端", "模型感測", "示意風電",
              "假設鋁業", "範例玻璃", "樣品橡膠", "測試電纜", "示範營造", "虛構租賃", "模擬物流", "範本紙業"]


def demo_history(seed=7, n=HISTORY_DAYS):
    rnd = random.Random(seed)
    end = dt.date(2026, 10, 15)
    dates = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= dt.timedelta(days=1)
    dates.reverse()
    days = {k: {} for k in dates}
    names = {}
    patterns = ["breakoutA", "breakoutB", "pre", "pullback", "trend", "range", "down"]
    for idx, nm in enumerate(DEMO_NAMES):
        code = str(9001 + idx)
        names[code] = nm
        pat = patterns[idx % len(patterns)]
        base = rnd.choice([18, 32, 48, 66, 95, 140, 260, 420])
        vol0 = rnd.choice([3000, 6000, 12000, 25000]) * 1000
        closes = []
        p = base
        for t in range(n):
            drift = 0.0
            if pat == "trend":
                drift = 0.004
            elif pat == "down":
                drift = -0.004
            elif pat in ("breakoutA", "pullback") and t < n - 14:
                drift = -0.002 if t < n - 24 else 0.0
            p *= 1 + drift + rnd.gauss(0, 0.012)
            closes.append(p)
        vols = [vol0 * (1 + rnd.gauss(0, 0.2)) for _ in range(n)]
        if pat == "breakoutA":
            for t in range(n - 14, n - 1):
                closes[t] = closes[n - 15] * (1 - 0.01 * (1 if t % 2 else -0.2))
            closes[-1] = max(closes[-14:-1]) * 1.04
            closes[-2] = min(closes[-14:-2]) * 1.0
            for t in range(n - 8, n - 1):
                closes[t] = closes[t - 1] * (1 + rnd.gauss(0.004, 0.006))
            closes[-1] = closes[-2] * 1.045
            vols[-1] *= 2.2
        elif pat == "breakoutB":
            plat = closes[-17] if closes[-17] > 0 else base
            for t in range(n - 16, n - 1):
                closes[t] = plat * (1 + 0.012 * math.sin(t * 0.9) + 0.0015 * (t - (n - 16)))
            closes[-1] = max(closes[-16:-1]) * 1.025
            vols[-1] *= 2.0
        elif pat == "pre":
            plat = closes[-17]
            for t in range(n - 16, n):
                closes[t] = plat * (1 + 0.012 * math.sin(t * 0.8) + 0.0025 * (t - (n - 16)))
            closes[-1] = max(closes[-16:-1]) * 0.992
            for t in range(n - 6, n):
                vols[t] *= 0.7
        elif pat == "pullback":
            plat = closes[-20]
            for t in range(n - 19, n - 7):
                closes[t] = plat * (1 + 0.010 * math.sin(t * 0.9))
            res = max(closes[n - 19:n - 7])
            closes[-7] = res * 1.04
            vols[-7] *= 2.4
            closes[-6], closes[-5], closes[-4] = res * 1.06, res * 1.045, res * 1.025
            closes[-3], closes[-2], closes[-1] = res * 1.012, res * 1.006, res * 1.01
            for t in range(n - 3, n):
                vols[t] *= 0.55
        for t in range(n):
            c = closes[t]
            o = closes[t - 1] if t else c
            hi = max(o, c) * (1 + abs(rnd.gauss(0, 0.006)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.006)))
            if pat in ("breakoutA", "breakoutB", "pre", "pullback") and t == n - 1 and pat != "pre":
                hi = max(hi, c * 1.002)
            v = max(vols[t], 500000)
            days[dates[t]][code] = [round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), round(v), round(v * (o + c) / 2)]
    return {"days": days, "holidays": [], "names": names}


# ---------- 主程式 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data.json"))
    args = ap.parse_args()
    tz_today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

    if args.demo:
        hist = demo_history()
        ex, status = {"punish": set(), "attention": {"9005"}, "full_delivery": set(), "manual": set()}, {"demo": "模擬"}
        log = []
        ds = sorted(hist["days"])
        for u in range(len(ds) - 12, len(ds)):
            stocks_u, meta_u, sd = analyse(hist, ex, upto=u)
            log = update_log(log, stocks_u, {"days": {d: hist["days"][d] for d in ds[: u + 1]}, "names": hist["names"]}, sd)
        stocks, meta, sd = analyse(hist, ex)
        log = update_log(log, stocks, hist, sd)
        meta["demo"] = True
    else:
        hist = load_json(os.path.join(DATA, "history.json"), {})
        hist = collect_history(hist, tz_today)
        if not hist.get("days"):
            sys.exit("沒有取得任何行情資料")
        save_json(os.path.join(DATA, "history.json"), hist)
        ex, status = fetch_excluded()
        log = load_json(os.path.join(DATA, "signal_log.json"), [])
        stocks, meta, sd = analyse(hist, ex)
        log = update_log(log, stocks, hist, sd)
        save_json(os.path.join(DATA, "signal_log.json"), log)
        meta["demo"] = False
    meta["sources"] = status
    meta["generated"] = (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    meta["params"] = {"min_avg_value": MIN_AVG_VALUE, "min_avg_lots": MIN_AVG_LOTS, "min_price": MIN_PRICE, "max_risk": MAX_RISK, "min_rr": MIN_RR,
                      "hold": [HOLD_MIN, HOLD_MAX]}
    stocks.sort(key=lambda s: s["strong"], reverse=True)
    save_json(args.out, {"meta": meta, "stocks": stocks, "log": log})
    n_sig = {k: sum(1 for s in stocks if s["sig"][k]) for k in "ABC"}
    print("date", meta["date"], "passed", meta["passed"], "signals", n_sig, "log", len(log), file=sys.stderr)


if __name__ == "__main__":
    main()
