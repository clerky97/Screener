#!/usr/bin/env python3
"""可轉債清單：櫃買中心「轉換公司債資訊看板」＋每日行情 → 轉換價值、溢價率"""
import csv, io, json, sys
import datetime as dt
import pandas as pd
import requests
from screener import ROOT, TODAY

TPEX = "https://www.tpex.org.tw"


def latest_csv(code):
    """回傳最近一個交易日的 CSV 連結"""
    for back in range(0, 40, 10):
        d = TODAY - dt.timedelta(days=back)
        r = requests.post(f"{TPEX}/www/zh-tw/bond/cbDaily", timeout=30,
                          data={"date": d.strftime("%Y/%m/%d"), "fileCode": code, "response": "json"})
        rows = r.json().get("tables", [{}])[0].get("data", [])
        if rows:
            return rows[0][0], TPEX + rows[0][1]
    sys.exit(f"找不到 {code} 的資料")


def read_csv(url):
    text = requests.get(url, timeout=30).content.decode("big5", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    head = next(r[1:] for r in rows if r and r[0] == "HEADER")
    body = [[x.strip() for x in r[1:]] for r in rows if r and r[0] == "BODY"]
    return pd.DataFrame(body, columns=head)


def num(s):
    return pd.to_numeric(s.str.replace(",", ""), errors="coerce")


def main():
    date, url = latest_csv("cbdrs001")
    info = read_csv(url)
    _, url = latest_csv("rsta0113")
    q = read_csv(url)
    q = q[q["交易"] == "等價"][["代號", "單位", "收市"]].rename(columns={"代號": "債券代碼", "單位": "成交張數"})

    df = info.merge(q, on="債券代碼", how="left")
    df["轉換價"] = num(df["轉換價格"])
    df["CB價"] = num(df["轉債參考價格"])
    df["股價"] = num(df["轉換標的股票價格"])
    df["成交張數"] = num(df["成交張數"]).fillna(0)
    df["餘額億"] = num(df["上月底發行餘額"]) / 1e8
    df["轉換價值"] = df["股價"] / df["轉換價"] * 100
    df["溢價率%"] = (df["CB價"] / df["轉換價值"] - 1) * 100
    df["股票代號"] = df["債券代碼"].str[:4]
    df = df[df["轉換價"] > 0].copy()

    def kind(r):
        if r["轉換價值"] >= 130:
            return "股性"
        if r["轉換價值"] <= 70:
            return "債性"
        return "平衡"

    df["性質"] = df.apply(kind, axis=1)
    cols = ["債券代碼", "債券簡稱", "股票代號", "CB價", "股價", "轉換價", "轉換價值", "溢價率%",
            "性質", "成交張數", "餘額億", "最近賣回權起日", "最近賣回權價格", "轉換迄日"]
    df = df[cols].round(2).sort_values("溢價率%")
    out = ROOT / "docs" / "cb.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 220)
    pd.set_option("display.unicode.east_asian_width", True)
    show = ["債券代碼", "債券簡稱", "CB價", "股價", "轉換價", "轉換價值", "溢價率%", "性質", "成交張數"]
    print(f"資料日 {date}，{len(df)} 檔可轉債，已存 {out.relative_to(ROOT)}\n")
    themes = json.loads((ROOT / "themes.json").read_text(encoding="utf-8"))
    mine = {s for ids in themes.values() for s in ids}
    hit = df[df["股票代號"].isin(mine)]
    print(f"== 選股名單裡有發可轉債的（{len(hit)} 檔）==")
    print(hit[show].to_string(index=False) if len(hit) else "（無）")
    active = df[df["成交張數"] >= 50]
    print("\n== 有量（>=50 張）且溢價率最低 10 檔：最貼近股價 ==")
    print(active.head(10)[show].to_string(index=False))
    print("\n== CB價 <= 102 且轉換價值 >= 85：下檔有保護、離轉換價不遠 ==")
    print(df[(df["CB價"] <= 102) & (df["轉換價值"] >= 85)].sort_values("溢價率%").head(10)[show].to_string(index=False))


if __name__ == "__main__":
    main()
