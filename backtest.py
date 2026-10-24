#!/usr/bin/env python3
"""回測 screener.py 的條件：每天算分數，看隔天開盤買進後 5/10/20 日報酬"""
import json, os, sys
import datetime as dt
import pandas as pd
from screener import ROOT, TODAY, get, col

START = os.environ.get("BT_START", "2023-01-01")
WARMUP = (pd.Timestamp(START) - pd.Timedelta(days=150)).date().isoformat()
HOLDS = [5, 10, 20]
FLAGS = ["多頭排列", "量價突破", "外資買超", "投信買超", "投信連買", "融資減少", "營收年增20%", "EPS年增"]


def fetch(name, sid, start=WARMUP):
    return get("data", {"dataset": name, "data_id": sid, "start_date": start})


def features(sid):
    px = fetch("TaiwanStockPrice", sid)
    if len(px) < 80:
        return pd.DataFrame()
    px = px[px["Trading_Volume"] > 0].sort_values("date")
    px["date"] = pd.to_datetime(px["date"])
    f = px.set_index("date")[["open", "close", "max", "Trading_Volume"]].astype(float)
    c, v = f["close"], f["Trading_Volume"] / 1000
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    vma20 = v.rolling(20).mean().shift(1)
    f["多頭排列"] = (c > ma20) & (ma20 > ma60) & (ma20 > ma20.shift(5))
    f["量價突破"] = (c > f["max"].rolling(20).max().shift(1)) & (v >= 1.5 * vma20)

    inst = fetch("TaiwanStockInstitutionalInvestorsBuySell", sid)
    if not inst.empty:
        inst = inst.assign(net=inst["buy"].astype(float) - inst["sell"].astype(float))
        g = inst.pivot_table(index="date", columns="name", values="net", aggfunc="sum").fillna(0)
        g.index = pd.to_datetime(g.index)
        g = g.reindex(f.index).fillna(0)
        foreign = col(g, "Foreign_Investor") + col(g, "Foreign_Dealer_Self")
        trust = col(g, "Investment_Trust")
        f["外資買超"] = foreign.rolling(5).sum() > 0
        f["投信買超"] = trust.rolling(5).sum() > 0
        f["投信連買"] = (trust > 0).rolling(3).sum() == 3

    mg = fetch("TaiwanStockMarginPurchaseShortSale", sid)
    if not mg.empty:
        mb = mg.assign(date=pd.to_datetime(mg["date"])).set_index("date")["MarginPurchaseTodayBalance"]
        mb = mb.astype(float).reindex(f.index).ffill()
        f["融資減少"] = mb - mb.shift(5) < 0

    # 月營收：次月 10 日才公布
    rv = fetch("TaiwanStockMonthRevenue", sid, "2021-01-01")
    if not rv.empty:
        rv = rv.assign(ym=rv["revenue_year"].astype(int) * 12 + rv["revenue_month"].astype(int) - 1)
        rv = rv.drop_duplicates("ym", keep="last").set_index("ym")["revenue"].astype(float)
        yoy = (rv / rv.reindex(rv.index - 12).values - 1) * 100
        avail = [pd.Timestamp(ym // 12, ym % 12 + 1, 1) + pd.DateOffset(months=1, days=9) for ym in yoy.index]
        s = pd.Series(yoy.values, index=avail).sort_index()
        f["營收年增20%"] = s.reindex(f.index, method="ffill") > 20

    # EPS：Q1 5/15、Q2 8/14、Q3 11/14、Q4 隔年 3/31 公布
    fs = fetch("TaiwanStockFinancialStatements", sid, "2021-01-01")
    if not fs.empty:
        e = fs[fs["type"] == "EPS"].assign(date=lambda d: pd.to_datetime(d["date"]))
        e = e.drop_duplicates("date", keep="last").set_index("date")["value"].astype(float)
        up = e > e.reindex(e.index - pd.DateOffset(years=1)).values
        lag = {3: (5, 15, 0), 6: (8, 14, 0), 9: (11, 14, 0), 12: (3, 31, 1)}
        avail = [pd.Timestamp(d.year + lag[d.month][2], lag[d.month][0], lag[d.month][1]) for d in e.index]
        s = pd.Series(up.values, index=avail).sort_index()
        f["EPS年增"] = s.reindex(f.index, method="ffill").fillna(False).astype(bool)

    for k in FLAGS:
        f[k] = f[k].fillna(False).astype(bool) if k in f else False
    f["score"] = f[FLAGS].sum(axis=1)
    entry = f["open"].shift(-1)
    for n in HOLDS:
        f[f"r{n}"] = (f["close"].shift(-n) / entry - 1) * 100
    f["id"] = sid
    return f[f.index >= START]


def stats(df, by):
    out = []
    for key, g in df.groupby(by):
        row = {by: key, "樣本": len(g)}
        for n in HOLDS:
            r = g[f"r{n}"].dropna()
            row[f"{n}日均報酬%"] = round(r.mean(), 2)
            row[f"{n}日勝率%"] = round((r > 0).mean() * 100, 1)
        out.append(row)
    return pd.DataFrame(out).set_index(by)


def portfolio(df, min_score, hold=5):
    """每 hold 個交易日換股：分數 >= min_score 等權買進，對照全部股票等權"""
    days = sorted(df.index.unique())
    eq_s, eq_b, n_pos = 1.0, 1.0, []
    for d in days[::hold]:
        day = df.loc[[d]].dropna(subset=[f"r{hold}"])
        if day.empty:
            continue
        pick = day[day["score"] >= min_score]
        eq_b *= 1 + day[f"r{hold}"].mean() / 100
        if not pick.empty:
            eq_s *= 1 + pick[f"r{hold}"].mean() / 100
        n_pos.append(len(pick))
    return eq_s, eq_b, sum(n_pos) / max(len(n_pos), 1)


def main():
    themes = json.loads((ROOT / "themes.json").read_text(encoding="utf-8"))
    sids = list(dict.fromkeys(s for ids in themes.values() for s in ids))
    frames = []
    for i, sid in enumerate(sids, 1):
        print(f"[{i}/{len(sids)}] {sid}", file=sys.stderr)
        f = features(sid)
        if not f.empty:
            frames.append(f)
    df = pd.concat(frames)
    pd.set_option("display.width", 200)
    print(f"\n期間 {df.index.min().date()} ~ {df.index.max().date()}，{df['id'].nunique()} 檔，{len(df)} 個股票日")
    print("（隔日開盤買進，持有 N 日收盤賣出，未計手續費與稅）\n")
    print("== 依分數 ==")
    print(stats(df, "score").to_string())
    print("\n== 單一條件成立時 ==")
    rows = []
    for k in FLAGS:
        s = stats(df[df[k]].assign(條件=k), "條件")
        rows.append(s)
    print(pd.concat(rows).to_string())
    print("\n全部股票日（基準）:")
    print(stats(df.assign(全部="all"), "全部").to_string())
    print("\n== 每週換股組合（持有 5 日）==")
    for m in range(3, 7):
        s, b, n = portfolio(df, m)
        print(f"分數>={m}: 累積 {(s - 1) * 100:7.1f}%  基準 {(b - 1) * 100:7.1f}%  平均持股 {n:.1f} 檔")


if __name__ == "__main__":
    main()
