#!/usr/bin/env python3
"""台股選股儀表板：抓 FinMind → 計算條件 → 輸出 docs/index.html"""
import json, math, os, sys, time
import datetime as dt
from pathlib import Path
import pandas as pd
import requests

API = "https://api.finmindtrade.com/api/v4"
TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
USE_BROKER = os.environ.get("USE_BROKER", "0") == "1"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "index.html"
TEMPLATE = ROOT / "template.html"
TZ = dt.timezone(dt.timedelta(hours=8))
TODAY = dt.datetime.now(TZ).date()

S = requests.Session()
if TOKEN:
    S.headers["Authorization"] = f"Bearer {TOKEN}"
CALLS = 0


def get(path, params, retries=3):
    global CALLS
    r = None
    for i in range(retries):
        CALLS += 1
        try:
            r = S.get(f"{API}/{path}", params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  ! 連線失敗 {e}")
            time.sleep(3 * (i + 1))
            continue
        if r.status_code == 402:
            sys.exit("FinMind 額度用完（HTTP 402）：減少股票數，或一小時後再跑")
        if r.status_code == 200:
            return pd.DataFrame(r.json().get("data", []))
        time.sleep(3 * (i + 1))
    code = r.status_code if r is not None else "-"
    print(f"  ! {path} {params.get('dataset', '')} {params.get('data_id', '')} 失敗 {code}")
    return pd.DataFrame()


def ds(name, sid, days):
    start = (TODAY - dt.timedelta(days=days)).isoformat()
    return get("data", {"dataset": name, "data_id": sid, "start_date": start})


def rnd(x, n=2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, n)


def col(df, name):
    return df[name] if name in df.columns else pd.Series(0.0, index=df.index)


def technical(px):
    if px.empty or len(px) < 61:
        return {}
    px = px.sort_values("date").reset_index(drop=True)
    c = px["close"].astype(float)
    hi = px["max"].astype(float)
    v = px["Trading_Volume"].astype(float) / 1000
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    vma20 = v.rolling(20).mean().shift(1)
    prev_high = hi.rolling(20).max().shift(1)
    spread = float(px["spread"].iloc[-1])
    prev_close = c.iloc[-1] - spread
    return {
        "date": px["date"].iloc[-1],
        "close": rnd(c.iloc[-1]),
        "chg": rnd(spread / prev_close * 100 if prev_close else None),
        "ma20": rnd(ma20.iloc[-1]),
        "ma60": rnd(ma60.iloc[-1]),
        "vol": rnd(v.iloc[-1], 0),
        "vol_ratio": rnd(v.iloc[-1] / vma20.iloc[-1] if vma20.iloc[-1] else None),
        "bull": bool(c.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1] and ma20.iloc[-1] > ma20.iloc[-6]),
        "breakout": bool(c.iloc[-1] > prev_high.iloc[-1] and v.iloc[-1] >= 1.5 * vma20.iloc[-1]),
        "spark": [rnd(x) for x in c.tail(60)],
    }


def institutional(df):
    if df.empty:
        return {}
    df = df.assign(net=(df["buy"].astype(float) - df["sell"].astype(float)) / 1000)
    g = df.pivot_table(index="date", columns="name", values="net", aggfunc="sum").sort_index().fillna(0)
    foreign = col(g, "Foreign_Investor") + col(g, "Foreign_Dealer_Self")
    trust = col(g, "Investment_Trust")
    dealer = col(g, "Dealer_self") + col(g, "Dealer_Hedging")
    streak = 0
    for x in reversed(trust.tolist()):
        if x > 0:
            streak += 1
        else:
            break
    return {
        "foreign5": rnd(foreign.tail(5).sum(), 0),
        "trust5": rnd(trust.tail(5).sum(), 0),
        "dealer5": rnd(dealer.tail(5).sum(), 0),
        "trust_streak": streak,
    }


def margin(df):
    if df.empty or len(df) < 6:
        return {}
    df = df.sort_values("date")
    mb = df["MarginPurchaseTodayBalance"].astype(float)
    sb = df["ShortSaleTodayBalance"].astype(float)
    return {
        "margin_bal": rnd(mb.iloc[-1], 0),
        "margin_chg5": rnd(mb.iloc[-1] - mb.iloc[-6], 0),
        "short_ratio": rnd(sb.iloc[-1] / mb.iloc[-1] * 100 if mb.iloc[-1] else None),
    }


def broker(sid, date, volume):
    df = get("taiwan_stock_trading_daily_report", {"data_id": sid, "date": date})
    if df.empty or not volume:
        return {}
    df = df.assign(net=(df["buy"].astype(float) - df["sell"].astype(float)) / 1000)
    net = df.groupby("securities_trader")["net"].sum().sort_values()
    top_buy, top_sell = net.tail(15), net.head(15)
    conc = (top_buy[top_buy > 0].sum() + top_sell[top_sell < 0].sum()) / volume * 100
    return {
        "conc": rnd(conc, 1),
        "top_buyers": [[k, rnd(v, 0)] for k, v in net.tail(3)[::-1].items() if v > 0],
    }


def revenue(df):
    if df.empty:
        return {}
    df = df.sort_values("date")
    rev = {(int(r.revenue_year), int(r.revenue_month)): float(r.revenue) for r in df.itertuples()}
    keys = sorted(rev)
    y, m = keys[-1]

    def yoy(y, m):
        last = rev.get((y - 1, m))
        return (rev[(y, m)] / last - 1) * 100 if last else None

    pm = (y, m - 1) if m > 1 else (y - 1, 12)
    streak = 0
    for k in reversed(keys):
        v = yoy(*k)
        if v is not None and v > 0:
            streak += 1
        else:
            break
    return {
        "rev_month": f"{y}/{m:02d}",
        "rev": rnd(rev[(y, m)] / 1e8, 2),
        "rev_yoy": rnd(yoy(y, m), 1),
        "rev_mom": rnd((rev[(y, m)] / rev[pm] - 1) * 100 if rev.get(pm) else None, 1),
        "rev_streak": streak,
    }


def eps(df):
    if df.empty:
        return {}
    e = df[df["type"] == "EPS"].sort_values("date")
    if e.empty:
        return {}
    vals = dict(zip(e["date"], e["value"].astype(float)))
    last_d = e["date"].iloc[-1]
    ly = f"{int(last_d[:4]) - 1}{last_d[4:]}"
    cur, prev = vals[last_d], vals.get(ly)
    q = (int(last_d[5:7]) - 1) // 3 + 1
    return {
        "eps_q": f"{last_d[:4]}Q{q}",
        "eps": rnd(cur),
        "eps_prev": rnd(prev),
        "eps_ttm": rnd(e["value"].astype(float).tail(4).sum()) if len(e) >= 4 else None,
        "eps_up": bool(prev is not None and cur > prev),
    }


def main():
    if not TOKEN:
        print("提醒：沒設 FINMIND_TOKEN，額度只有每小時 300 次")
    themes = json.loads((ROOT / "themes.json").read_text(encoding="utf-8"))
    stock_themes = {}
    for t, ids in themes.items():
        for sid in ids:
            stock_themes.setdefault(sid, []).append(t)

    info = get("data", {"dataset": "TaiwanStockInfo"})
    names = {}
    if not info.empty:
        info = info.sort_values("date").drop_duplicates("stock_id", keep="last")
        names = dict(zip(info["stock_id"], info["stock_name"]))

    rows = []
    for i, (sid, ts) in enumerate(stock_themes.items(), 1):
        print(f"[{i}/{len(stock_themes)}] {sid} {names.get(sid, '')}")
        t = technical(ds("TaiwanStockPrice", sid, 150))
        if not t:
            print("  ! 價格資料不足，略過")
            continue
        row = {"id": sid, "name": names.get(sid, sid), "themes": ts, **t}
        row.update(institutional(ds("TaiwanStockInstitutionalInvestorsBuySell", sid, 20)))
        row.update(margin(ds("TaiwanStockMarginPurchaseShortSale", sid, 20)))
        row.update(revenue(ds("TaiwanStockMonthRevenue", sid, 800)))
        row.update(eps(ds("TaiwanStockFinancialStatements", sid, 800)))
        if USE_BROKER:
            row.update(broker(sid, t["date"], t["vol"]))

        flags = {
            "多頭排列": row["bull"],
            "量價突破": row["breakout"],
            "外資買超": (row.get("foreign5") or 0) > 0,
            "投信買超": (row.get("trust5") or 0) > 0,
            "投信連買": row.get("trust_streak", 0) >= 3,
            "融資減少": (row.get("margin_chg5") or 0) < 0,
            "營收年增20%": (row.get("rev_yoy") or 0) > 20,
            "EPS年增": row.get("eps_up", False),
        }
        if USE_BROKER:
            flags["分點集中"] = (row.get("conc") or 0) >= 5
        row["flags"] = [k for k, v in flags.items() if v]
        row["score"] = len(row["flags"])
        rows.append(row)

    data = {
        "generated": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "date": max((r["date"] for r in rows), default=""),
        "broker": USE_BROKER,
        "themes": list(themes),
        "stocks": rows,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"完成：{len(rows)} 檔，共 {CALLS} 次請求")


if __name__ == "__main__":
    main()

