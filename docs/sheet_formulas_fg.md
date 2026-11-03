# 持倉統計 F／G 欄：合約乘數改用「最新一筆買進」

目前 F2、G2 用 `INDEX(Trade_Log!I:I, MATCH(tick, Trade_Log!E:E, 0))` 取乘數，抓到的是 Trade_Log 第一筆同標的的乘數（不分帳戶、種類、買賣）。
D2／H2／K2 用的是「同帳戶＋種類＋標的、最新一筆買進」，F／G 應該一致。這是 Sheet 公式，程式只讀值，所以這個 PR 不改 Sheet；請手動貼上。

## 乘數運算式（取代原本的 IFERROR(INDEX(...MATCH...),1)）

```
LET(m, IFERROR(CHOOSEROWS(FILTER(Trade_Log!I2:I, Trade_Log!B2:B=acc, Trade_Log!C2:C=type, Trade_Log!E2:E=tick, Trade_Log!D2:D="買進"), -1), ""), IF(AND(ISNUMBER(m), m>0), m, IF(tick="TX",200,IF(tick="MTX",50,IF(OR(tick="FMF",tick="TMF"),10,IFERROR(INDEX(Trade_Log!I:I, MATCH(tick, Trade_Log!E:E, 0)), 1))))))
```

找不到買進列（或乘數空白／0）時：期貨用固定乘數 TX 200、MTX 50、FMF／TMF 10，其他維持原本的第一筆比對，最後退回 1。

## F2

```
=MAP(A2:A, B2:B, C2:C, LAMBDA(acc, type, tick, IF(tick="", "", LET(buy_qty, SUMIFS(Trade_Log!G:G, Trade_Log!B:B, acc, Trade_Log!C:C, type, Trade_Log!E:E, tick, Trade_Log!D:D, "買進"), sell_qty, SUMIFS(Trade_Log!G:G, Trade_Log!B:B, acc, Trade_Log!C:C, type, Trade_Log!E:E, tick, Trade_Log!D:D, "賣出"), buy_amt, SUMIFS(Trade_Log!J:J, Trade_Log!B:B, acc, Trade_Log!C:C, type, Trade_Log!E:E, tick, Trade_Log!D:D, "買進"), sell_amt, SUMIFS(Trade_Log!J:J, Trade_Log!B:B, acc, Trade_Log!C:C, type, Trade_Log!E:E, tick, Trade_Log!D:D, "賣出"), mult, LET(m, IFERROR(CHOOSEROWS(FILTER(Trade_Log!I2:I, Trade_Log!B2:B=acc, Trade_Log!C2:C=type, Trade_Log!E2:E=tick, Trade_Log!D2:D="買進"), -1), ""), IF(AND(ISNUMBER(m), m>0), m, IF(tick="TX",200,IF(tick="MTX",50,IF(OR(tick="FMF",tick="TMF"),10,IFERROR(INDEX(Trade_Log!I:I, MATCH(tick, Trade_Log!E:E, 0)), 1)))))), IF(buy_qty > 0, IFERROR(buy_amt / buy_qty / mult, 0), IFERROR(sell_amt / sell_qty / mult, 0))))))
```

## G2

```
=MAP(A2:A, B2:B, C2:C, E2:E, F2:F, LAMBDA(acc, type, tick, qty, cost, IF(qty="", "", qty * cost * LET(m, IFERROR(CHOOSEROWS(FILTER(Trade_Log!I2:I, Trade_Log!B2:B=acc, Trade_Log!C2:C=type, Trade_Log!E2:E=tick, Trade_Log!D2:D="買進"), -1), ""), IF(AND(ISNUMBER(m), m>0), m, IF(tick="TX",200,IF(tick="MTX",50,IF(OR(tick="FMF",tick="TMF"),10,IFERROR(INDEX(Trade_Log!I:I, MATCH(tick, Trade_Log!E:E, 0)), 1)))))))))
```

貼上後請檢查 FMF 那列：目前 G = 1 × 46,733 × 10 = 467,330，改完應該不變。
