# Stock Screener — Simple Plan

Java 21 / Spring Boot 3.3 / MySQL 8. **4 tables.** Ingest NSE XBRL filings → compute ratios → store only what's queryable → filter with a query DSL → optional LLM verdict.

---

## 1. Core idea

```
XBRL filing  →  parse in memory  →  keep ~20 line items per period
                                    (financials table)
                                            ↓
                                    compute all ratios
                                            ↓
                              company_metrics  ← one flat row per company
                                            ↓
                              this is what queries hit
```

Two rules that keep it small:

1. **Only two things get stored:** the handful of raw line items needed to *compute* ratios, and the computed ratios themselves. Everything else in a filing is parsed, used, discarded.
2. **`company_metrics` is one flat row per company.** Every screener query hits one table, no joins. That's what makes `roe > 18% AND pe < 25 AND marketcap > 1000cr` a single indexed scan.

Deliberately dropped from the earlier design: raw XBRL fact staging, concept-mapping tables, instance archive in DB, segment reporting, related-party transactions, sector benchmarks, revision supersession chains. Cost of dropping them: no governance screens, and a parser fix means re-downloading instead of re-parsing. Worth it at this size.

---

## 2. Source data

**Index CSV:** `CF-Integrated-Filing-equities-*.csv` — 23,713 rows, 2,337 companies, 6 quarter-ends.

Columns used: `SYMBOL`, `COMPANY NAME`, `QUARTER END DATE`, `TYPE OF SUBMISSION`, `CONSOLIDATED / STANDALONE`, **`XBRL`** (the XML link), `BROADCAST DATE/TIME`.

**Parse the `XBRL` XML, not the `DETAILS` HTML.** Every row has one. XBRL tags each number with an explicit concept, period, and scale — a dictionary lookup instead of scraping a layout NSE can change.

**Two extra sources needed for what you asked for:**

| Want | Source |
|---|---|
| Market Cap, Price, P/E, P/B, High/Low, Dividend Yield | **NSE daily bhavcopy** (one CSV/day: symbol, close, high, low, volume) |
| Promoter / FII / DII holding | **NSE shareholding-pattern filing** (separate quarterly filing) |

Neither is in the financial filings. Both are single small loaders — see §6.

---

## 3. Schema — 4 tables

### 3.1 `companies`

```sql
CREATE TABLE companies (
  id                 BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  symbol             VARCHAR(30)  NOT NULL,
  company_name       VARCHAR(255) NOT NULL,
  isin               CHAR(12)     NULL,
  sector             VARCHAR(100) NULL,
  is_financial_co    BOOLEAN NOT NULL DEFAULT FALSE,
  face_value         DECIMAL(12,4) NULL,
  shares_outstanding DECIMAL(20,2) NULL,   -- paid_up_capital / face_value
  status             VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_symbol (symbol)
) ENGINE=InnoDB;
```

`is_financial_co` matters: ROCE and debt/equity are meaningless for banks and NBFCs. Flag them and skip those ratios rather than emitting nonsense.

### 3.2 `financials` — one table for quarterly **and** annual

`period_type` discriminates. Quarterly rows drive TTM and YoY; annual rows drive 3Y/5Y CAGR.

```sql
CREATE TABLE financials (
  id                 BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  company_id         BIGINT UNSIGNED NOT NULL,
  period_end         DATE     NOT NULL,
  period_type        VARCHAR(8) NOT NULL,   -- Q | FY
  consolidated       BOOLEAN  NOT NULL,
  months_covered     TINYINT  NOT NULL,     -- 3 or 12
  derivation         VARCHAR(10) NOT NULL,  -- REPORTED | YTD_DIFF
  source_url         VARCHAR(700) NULL,     -- audit trail back to the filing

  -- P&L  (₹ lakh)
  revenue            DECIMAL(22,2) NULL,
  other_income       DECIMAL(22,2) NULL,
  total_expenses     DECIMAL(22,2) NULL,
  operating_profit   DECIMAL(22,2) NULL,    -- EBITDA, derived at ingest
  depreciation       DECIMAL(22,2) NULL,
  finance_costs      DECIMAL(22,2) NULL,
  profit_before_tax  DECIMAL(22,2) NULL,
  tax_expense        DECIMAL(22,2) NULL,
  net_profit         DECIMAL(22,2) NULL,
  eps                DECIMAL(14,4) NULL,

  -- Balance sheet (half-yearly in practice — see §5.2)
  total_equity       DECIMAL(22,2) NULL,
  total_borrowings   DECIMAL(22,2) NULL,
  cash_and_investments DECIMAL(22,2) NULL,
  total_assets       DECIMAL(22,2) NULL,
  paid_up_capital    DECIMAL(22,2) NULL,

  -- Cash flow (half-yearly in practice)
  cash_from_operations DECIMAL(22,2) NULL,
  capex              DECIMAL(22,2) NULL,
  dividends_paid     DECIMAL(22,2) NULL,

  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_fin (company_id, period_end, period_type, consolidated),
  KEY idx_fin_lookup (company_id, period_type, period_end DESC),
  CONSTRAINT fk_fin_company FOREIGN KEY (company_id) REFERENCES companies(id)
) ENGINE=InnoDB;
```

21 numeric columns is enough for every ratio you listed. Everything else in the filing — segment tables, related-party detail, 30 cash-flow reconciliation lines, OCI breakdown — is parsed and thrown away.

### 3.3 `daily_prices`

```sql
CREATE TABLE daily_prices (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  company_id   BIGINT UNSIGNED NOT NULL,
  trade_date   DATE NOT NULL,
  close_price  DECIMAL(14,4) NOT NULL,
  high_price   DECIMAL(14,4) NULL,
  low_price    DECIMAL(14,4) NULL,
  volume       BIGINT NULL,
  UNIQUE KEY uk_price (company_id, trade_date),
  KEY idx_price_date (trade_date DESC),
  CONSTRAINT fk_price_company FOREIGN KEY (company_id) REFERENCES companies(id)
) ENGINE=InnoDB;
```

Needed only because 52-week high/low requires history. ~2,300 rows/day; keep 400 days and prune. Everything else price-derived lands in `company_metrics`.

### 3.4 `company_metrics` — the screener table

One row per company. Wide and flat on purpose: every query is a single-table indexed scan.

```sql
CREATE TABLE company_metrics (
  company_id            BIGINT UNSIGNED PRIMARY KEY,
  as_of_date            DATE NOT NULL,
  basis                 VARCHAR(12) NOT NULL,   -- CONSOLIDATED | STANDALONE

  -- Price & size
  current_price         DECIMAL(14,4) NULL,
  market_cap_cr         DECIMAL(20,2) NULL,
  high_52w              DECIMAL(14,4) NULL,
  low_52w               DECIMAL(14,4) NULL,
  pct_from_high         DECIMAL(10,4) NULL,

  -- Valuation
  pe_ratio              DECIMAL(14,4) NULL,
  pb_ratio              DECIMAL(14,4) NULL,
  book_value            DECIMAL(14,4) NULL,     -- per share
  ev_to_ebitda          DECIMAL(14,4) NULL,
  dividend_yield_pct    DECIMAL(10,4) NULL,
  earnings_yield_pct    DECIMAL(10,4) NULL,

  -- Returns
  roe_pct               DECIMAL(12,4) NULL,
  roce_pct              DECIMAL(12,4) NULL,
  roa_pct               DECIMAL(12,4) NULL,

  -- Margins
  operating_margin_pct  DECIMAL(12,4) NULL,
  net_profit_margin_pct DECIMAL(12,4) NULL,

  -- Leverage
  debt_to_equity        DECIMAL(12,4) NULL,
  interest_coverage     DECIMAL(12,4) NULL,

  -- TTM absolutes (₹ crore)
  sales_ttm_cr          DECIMAL(20,2) NULL,
  ebitda_ttm_cr         DECIMAL(20,2) NULL,
  net_profit_ttm_cr     DECIMAL(20,2) NULL,
  eps_ttm               DECIMAL(14,4) NULL,
  cfo_ttm_cr            DECIMAL(20,2) NULL,
  fcf_ttm_cr            DECIMAL(20,2) NULL,
  ocf_to_net_profit     DECIMAL(12,4) NULL,

  -- Growth: CAGR %
  sales_growth_1y_pct   DECIMAL(12,4) NULL,
  sales_growth_2y_pct   DECIMAL(12,4) NULL,
  sales_growth_3y_pct   DECIMAL(12,4) NULL,
  sales_growth_5y_pct   DECIMAL(12,4) NULL,
  profit_growth_1y_pct  DECIMAL(12,4) NULL,
  profit_growth_2y_pct  DECIMAL(12,4) NULL,
  profit_growth_3y_pct  DECIMAL(12,4) NULL,
  profit_growth_5y_pct  DECIMAL(12,4) NULL,
  eps_growth_3y_pct     DECIMAL(12,4) NULL,

  -- Latest quarter
  qtr_sales_yoy_pct     DECIMAL(12,4) NULL,
  qtr_profit_yoy_pct    DECIMAL(12,4) NULL,

  -- Shareholding %
  promoter_holding_pct  DECIMAL(8,4) NULL,
  promoter_pledge_pct   DECIMAL(8,4) NULL,
  fii_holding_pct       DECIMAL(8,4) NULL,
  dii_holding_pct       DECIMAL(8,4) NULL,
  public_holding_pct    DECIMAL(8,4) NULL,

  -- LLM verdict
  llm_verdict           VARCHAR(20) NULL,   -- STRONG_BUY|BUY|HOLD|AVOID|INSUFFICIENT_DATA
  llm_score             TINYINT NULL,       -- 0..100
  llm_summary           TEXT NULL,
  llm_analyzed_at       DATETIME NULL,

  -- Data quality
  data_completeness_pct DECIMAL(6,2) NULL,
  warnings              JSON NULL,
  updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_m_roe (roe_pct), KEY idx_m_roce (roce_pct),
  KEY idx_m_pe (pe_ratio), KEY idx_m_mcap (market_cap_cr),
  KEY idx_m_verdict (llm_verdict),
  CONSTRAINT fk_m_company FOREIGN KEY (company_id) REFERENCES companies(id)
) ENGINE=InnoDB;
```

Note this table stores **crore** (not lakh) and percentages as plain numbers — it's the display/query layer, so it matches how you'd type a query. `financials` stays in lakh where the filings live; conversion happens once, on write.

`warnings` and `data_completeness_pct` earn their place: with balance sheets arriving half-yearly and 5 quarters of history, plenty of cells are legitimately NULL. Without these you can't tell "3 companies matched" from "1,200 had no computable ROE."

---

## 4. Ratio formulas

Derived inputs first:

```
EBITDA        = PBT + finance_costs + depreciation − other_income
EBIT          = PBT + finance_costs
net_debt      = total_borrowings − cash_and_investments
shares        = paid_up_capital / face_value
market_cap    = shares × current_price
FCF           = cash_from_operations − capex
TTM x         = sum of last 4 consecutive quarterly rows
```

| Metric | Formula |
|---|---|
| Market Cap | shares × current price |
| P/E | price ÷ TTM EPS — **NULL if EPS ≤ 0** |
| Book Value | total equity ÷ shares |
| P/B | price ÷ book value |
| EV/EBITDA | (market cap + net debt) ÷ TTM EBITDA |
| Dividend Yield % | TTM dividends paid ÷ market cap × 100 |
| Earnings Yield % | TTM EPS ÷ price × 100 |
| High/Low | max/min `high_price`/`low_price` over 52 weeks |
| ROE % | TTM net profit ÷ avg total equity × 100 |
| ROCE % | TTM EBIT ÷ (total equity + total borrowings) × 100 — cash **not** netted, see note |
| ROA % | TTM net profit ÷ avg total assets × 100 |
| Operating margin % | TTM EBITDA ÷ TTM revenue × 100 |
| Net margin % | TTM net profit ÷ TTM revenue × 100 |
| Debt/Equity | total borrowings ÷ total equity |
| Interest coverage | TTM EBIT ÷ TTM finance costs |
| OCF/PAT | TTM CFO ÷ TTM net profit |
| Sales growth *n*y | CAGR of annual revenue: `(end/begin)^(1/n) − 1` |
| Profit growth *n*y | same on net profit |
| Qtr YoY % | (this quarter − same quarter last year) ÷ \|last year\| × 100 |

**ROCE convention.** Cash is not netted from capital employed. Both forms are defensible but they diverge sharply for cash-rich companies — on Thyrocare's H1 FY26 figures, netting ~₹15,000 lakh of cash and investments moves ROCE from **43.8% to 62.0%**. The unnetted form matches what Indian screeners report, so it's used here for comparability. Netting is reserved for enterprise value.

Five rules that prevent most real bugs:

1. **Any null input ⇒ null output. Never substitute zero.** A missing balance sheet is not zero equity.
2. **CAGR is NULL if the starting value ≤ 0.** Sign flips make CAGR meaningless.
3. **Annualise flows against stocks.** A 6-month profit over point-in-time equity reads as *half* the true ROE. Multiply by `12/months_covered` and record it in `warnings`.
4. **`is_financial_co` ⇒ skip ROCE, D/E, EV/EBITDA.**
5. **Clamp nothing, flag everything.** ROE of 412% stays 412% with `near_zero_equity` in `warnings` — and `strict` mode excludes it from screens.

`BigDecimal` throughout, never `double`. Put every formula in one `RatioCalculator` class with no Spring or DB dependency, so it's testable against a hand-verified filing.

---

## 5. Four things that will silently corrupt results

Short list, but skipping any of these produces confidently wrong screens.

### 5.1 Rounding level varies per filing
Each filing declares `Level of rounding` — **Lakhs, Thousands, Millions, or Crores**. The same concept arrives at 100× different magnitudes across companies. Normalise everything to lakh on write. If the field is missing, **fail the filing — don't guess.** Sanity check: `paid_up_capital ÷ face_value` should give a plausible share count; off by 100× means a scale bug.

### 5.2 Balance sheet and cash flow are half-yearly
Under SEBI LODR they're required half-yearly, not quarterly. So P&L metrics (margins, EPS, growth) refresh every quarter, but **ROE, ROCE, D/E and cash-flow metrics refresh twice a year.** Compute them from the latest available balance sheet and store `as_of_date` so it's visible.

### 5.3 Quarter vs YTD
Each filing carries both a 3-month and a year-to-date column; some filers populate only YTD. When the 3-month column is absent: `quarter = YTD(n) − YTD(n−1)`, same FY and same basis only. Record `derivation = YTD_DIFF`. Q4 is the trap — often filed as audited FY only, so Q4 = FY − 9M and every year-end adjustment lands in one quarter.

### 5.4 Consolidated vs standalone
Both are filed (13,687 vs 10,026 rows). Prefer **consolidated**, fall back to standalone, and store which in `company_metrics.basis`. Standalone hides subsidiary debt and losses. `consolidated` stays part of the `financials` unique key — never mix within one calculation.

**Plus one limitation to plan around:** your CSV spans 6 quarter-ends (Mar-2025 → Jun-2026). YoY and TTM work. **3Y/5Y CAGR does not** — those columns stay NULL until annual history accumulates. Start the daily index download on day one; history depth is the one thing you can't retrofit. To have 5Y growth sooner, backfill FY rows from older NSE annual-results filings.

---

## 6. Pipeline

```
1. IMPORT INDEX     POST /api/v1/filings  (index CSV)
                    upsert companies; queue filings not yet parsed
                    dedupe on XBRL url; latest broadcast wins per
                    (symbol, quarter_end, consolidated)

2. PARSE            download XBRL XML → StAX stream
                    read rounding level, normalise to ₹ lakh
                    extract ~21 line items → financials
                    derive missing quarters via YTD_DIFF
                    cache the XML on disk (URLs are immutable — fetch once)

3. PRICES           daily NSE bhavcopy → daily_prices
                    shares = paid_up_capital / face_value

4. SHAREHOLDING     quarterly NSE shareholding filing
                    → promoter / pledge / FII / DII % straight into company_metrics

5. COMPUTE          RatioCalculator → upsert company_metrics
                    TTM from last 4 quarters, CAGR from FY rows
                    set data_completeness_pct + warnings

6. LLM (optional)   send the company_metrics row as compact JSON
                    → verdict + score + summary back into the same row
                    skip when data_completeness_pct < 40

7. SERVE            GET /api/v1/screener?q=roe > 18% AND pe < 25
```

Spring Batch with steps `import → parse → prices → compute`, restartable, per-filing skip policy so one malformed instance can't kill a 23K-filing run. Rate-limit downloads to 2–4 concurrent.

Schedule: index + parse daily after market close, bhavcopy daily, shareholding + recompute weekly.

---

## 7. Screener query DSL

```
roe > 18%, roce > 20%, marketcap > 1000cr
```

Grammar — recursive descent, ~200 lines, no ANTLR:

```
query      := orExpr
orExpr     := andExpr ( "OR" andExpr )*
andExpr    := notExpr ( ("AND" | ",") notExpr )*     -- ',' = implicit AND
notExpr    := "NOT"? primary
primary    := "(" orExpr ")" | comparison
comparison := metric op value | metric "BETWEEN" value "AND" value
            | metric "IN" "(" value,... ")"
op         := > | >= | < | <= | = | != | ~          -- '~' = fuzzy text
value      := NUMBER unit?                           -- % cr L x d
```

### Metric registry — the security boundary

User identifiers **never** reach SQL. Every name resolves through a static whitelist; unknown ⇒ `400` with suggestions and no SQL is built. AST compiles to a JPA Criteria `Specification` with bound parameters, so injection is structurally impossible.

```java
record MetricDef(String key, Set<String> aliases,
                 String column, Unit unit, String label) {}
```

| Keys | Column family |
|---|---|
| `marketcap`, `price`, `high_52w`, `low_52w`, `pct_from_high` | Price & size |
| `pe`, `pb`, `book_value`, `ev_ebitda`, `div_yield`, `earnings_yield` | Valuation |
| `roe`, `roce`, `roa` | Returns |
| `opm` / `operating_margin`, `net_margin` | Margins |
| `de` / `debt_to_equity`, `interest_coverage` | Leverage |
| `sales`, `ebitda`, `net_profit`, `eps`, `cfo`, `fcf`, `ocf_to_pat` | TTM absolutes |
| `sales_growth_1y/2y/3y/5y`, `profit_growth_1y/2y/3y/5y`, `eps_growth_3y` | Growth |
| `qtr_sales_yoy`, `qtr_profit_yoy` | Latest quarter |
| `promoter_holding`, `promoter_pledge`, `fii_holding`, `dii_holding`, `public_holding` | Shareholding |
| `verdict`, `llm_score` | LLM |
| `symbol`, `name`, `sector`, `is_financial_co`, `data_completeness` | Profile |

Adding a metric = one registry entry. No parser change, no endpoint change.

### Unit normalisation — the most common source of wrong results

| Input | Resolves to | Rule |
|---|---|---|
| `roe > 18%` / `roe > 18` | `roe_pct > 18` | bare number on a % metric ⇒ percent |
| `roe > 0.18` | — | **reject**: "did you mean 18%?" |
| `marketcap > 1000cr` / `> 1000` | `market_cap_cr > 1000` | stored in crore already |
| `sales > 500cr` | `sales_ttm_cr > 500` | |
| `de < 0.5x` | `0.5` | `x` optional on ratios |

Anything ambiguous returns `400` with an explanation. **Silent wrong answers are worse than errors.**

### NULL semantics

- Comparisons **exclude** NULLs — `roe > 18` never matches a company with no computed ROE. (SQL does this naturally; the surprise is that `NOT (roe > 18)` also excludes it.)
- `strict=false` (default) also excludes rows whose `warnings` touch a filtered metric. You don't want `roe > 18%` matching a company whose ROE is 412% because equity is near zero.
- Banks/NBFCs auto-excluded when filtering `roce`, `de`, `ev_ebitda` unless `includeFinancials=true`.
- Response reports `excludedForMissingData` so "6 of 2,337 matched" is explicable.

### API

```
GET /api/v1/screener
    ?q=roe > 18% AND roce > 20% AND marketcap > 1000cr
    &sort=roce:desc&page=0&size=50&strict=false
```

```json
{
  "query": "roe > 18% AND roce > 20% AND marketcap > 1000cr",
  "interpretation": [
    {"metric":"roe","column":"roe_pct","op":">","value":18.0,"unit":"PERCENT"},
    {"metric":"roce","column":"roce_pct","op":">","value":20.0,"unit":"PERCENT"},
    {"metric":"marketcap","column":"market_cap_cr","op":">","value":1000.0,"unit":"CRORE"}
  ],
  "universe": 2337, "totalHits": 41,
  "excludedForMissingData": 1208, "excludedFinancialCos": 143,
  "results": [
    {"symbol":"CIPLA","name":"Cipla Limited","roe":18.4,"roce":22.1,
     "pe":24.6,"marketcap":118500,"basis":"CONSOLIDATED","verdict":"BUY"}
  ],
  "disclaimer": "Derived from public NSE filings. Not investment advice."
}
```

Echoing `interpretation` is what makes the DSL trustworthy — the user sees how `18%` and `1000cr` were read before believing the results.

Endpoints: `GET /screener`, `GET /screener/metrics` (autocomplete), `POST /screener/validate` (parse-only), `GET /companies/{symbol}` (metrics + quarterly history), `POST /companies/{symbol}/analysis` (force LLM re-run).

---

## 8. LLM verdict

Send the `company_metrics` row as compact JSON — ~1K tokens, not raw filings. **Java computes every ratio; the LLM only interprets.** That separation is the whole reliability story.

- System prompt: conservative fundamental analyst, judge only on supplied data, return `INSUFFICIENT_DATA` when key inputs are missing, no personalised advice.
- Pass `warnings` and NULLs through explicitly. A model told "ROE annualised from 6 months, no 5Y history" reasons correctly about what it can't conclude.
- Enforced JSON output: `verdict`, `score`, `summary` (≤120 words), `strengths[]`, `concerns[]`. `temperature: 0`.
- Cache on `SHA256(payload)` — 2,337 companies × repeated runs is real money. Skip entirely below 40% completeness.
- `LlmClient` interface with an Anthropic implementation; swap via config.

---

## 9. Build order

| Phase | Deliverable |
|---|---|
| 1 | Boot skeleton, Flyway, Docker Compose MySQL, index CSV → `companies` + filing queue |
| 2 | XBRL StAX parser → `financials`, **rounding normalisation with assertions** |
| 3 | Quarter/YTD derivation + TTM assembly |
| 4 | `RatioCalculator` + unit tests against 3 hand-verified filings |
| 5 | Bhavcopy loader → `daily_prices` ⇒ market cap, P/E, P/B, 52w H/L unlock |
| 6 | `company_metrics` compute job |
| 7 | Metric registry + fixed-param filtering (proves the data layer) |
| 8 | **Screener DSL** parser → `Specification` compiler + unit normalisation |
| 9 | Shareholding loader (promoter / FII / DII) |
| 10 | LLM verdicts |
| 11 | Spring Batch orchestration, skip policies, scheduling |

**Do not move past phase 4 until ratios match a hand-checked filing.** Pick three companies, compute ROE/ROCE/margins by hand from the iXBRL HTML, and assert. A scale or annualisation bug at this layer is invisible to every test above it and makes the whole screener confidently wrong.

Phase 7 before 8 on purpose: get the columns and indexes right with dumb query params, so when the parser lands you're debugging grammar, not SQL.

---

## 10. Example queries

```
-- Quality compounders
roe > 18%, roce > 20%, de < 0.5, sales_growth_3y > 15%

-- Your original
roe > 18% AND roce > 20% AND marketcap > 1000cr

-- Value
pe < 15 AND pb < 2 AND roe > 15% AND div_yield > 2%

-- Profits are real cash
ocf_to_pat > 0.9 AND net_margin > 12% AND roce > 20%

-- Accounting-risk watchlist
profit_growth_1y > 30% AND ocf_to_pat < 0.6

-- Quality small/mid caps
marketcap BETWEEN 500cr AND 5000cr AND roce > 20% AND de < 0.3

-- High promoter conviction, no pledge
promoter_holding > 55% AND promoter_pledge = 0 AND roe > 15%

-- Institutional accumulation
fii_holding > 10% AND dii_holding > 10% AND sales_growth_3y > 12%

-- Near 52-week low but fundamentally sound
pct_from_high < -30% AND roce > 18% AND de < 0.5 AND ocf_to_pat > 0.8

-- Debt-free growth
de = 0 AND sales_growth_3y > 20% AND opm > 15%

-- Leverage watchlist
de > 2 AND interest_coverage < 2

-- Turnaround
qtr_profit_yoy > 50% AND qtr_sales_yoy > 15% AND opm > 10%

-- LLM shortlist
verdict IN ("BUY","STRONG_BUY") AND llm_score > 70 AND data_completeness > 70
```

Ship this list as the parser's test suite — each line asserted against expected SQL parameters and a fixture dataset. Cheapest possible guard against a grammar change silently altering what a query means.

---

## 11. Appendix — data coverage from the index CSV

Three layers: the CSV's own columns, the XBRL filing each row links to, and what's nowhere in this source.

### 11.1 The CSV's 13 columns

The CSV carries **zero financial numbers** — it's an index. Everything else comes from the `XBRL` links.

| Column | Fills |
|---|---|
| SYMBOL | `companies.symbol` |
| COMPANY NAME | `companies.company_name` |
| QUARTER END DATE | `financials.period_end` |
| CONSOLIDATED / STANDALONE | `financials.consolidated` |
| **XBRL** | `financials.source_url` + the download target |
| DETAILS | Human-readable audit link (iXBRL HTML) |
| TYPE OF SUBMISSION | Dedupe / supersession |
| AUDITED / UNAUDITED | Data-quality flag |
| BROADCAST DATE/TIME | Which filing wins per natural key |
| REVISED DATE/TIME · REVISION REMARKS · DISSEMINATION · TIME TAKEN | Not needed |

### 11.2 Inside the XBRL filings

**`financials` — all 25 columns fillable.**

| Column | Source | Note |
|---|---|---|
| revenue · other_income · total_expenses | P&L | Direct |
| depreciation · finance_costs | P&L | Direct |
| profit_before_tax · tax_expense · net_profit | P&L | Direct |
| eps | P&L | Basic and diluted both available |
| paid_up_capital · **face_value** | P&L | Unlocks share count |
| total_assets · total_equity | Balance sheet | **Half-yearly only** |
| cash_from_operations · capex · dividends_paid | Cash flow | **Half-yearly only**; capex explicitly tagged as "Purchase of PPE" |
| operating_profit | *Derived* | `PBT + finance_costs + depreciation − other_income` |
| total_borrowings | *Derived* | current + non-current borrowings |
| cash_and_investments | *Derived* | cash + bank balances + current investments |
| months_covered · derivation | Period context | 3-month and YTD columns both present |

**`companies` — 5 of 8.** `isin` and `face_value` come from the general-info block; `shares_outstanding` derives from `paid_up_capital ÷ face_value`.

Two traps found in the Thyrocare filing:

- `Reserves excluding revaluation reserve` was **blank** — take equity from the balance sheet (`equity_share_capital + other_equity`), not that field.
- The filing's own `Debt equity ratio`, `DSCR`, `ISCR` all read `0` despite ₹122 lakh of finance costs. Ignore them; compute your own.

### 11.3 What's missing

| Blocked | Metrics affected | Fix |
|---|---|---|
| **No price data** | current_price, market_cap, high_52w, low_52w, pct_from_high, **pe, pb**, ev_ebitda, div_yield, earnings_yield — 10 metrics + the whole `daily_prices` table | NSE daily bhavcopy (1 CSV/day) |
| **No shareholding** | promoter_holding, promoter_pledge, fii, dii, public — 5 metrics | NSE shareholding-pattern filing (separate quarterly) |
| **No sector** | `companies.sector`, `is_financial_co` → and the bank/NBFC ratio-skip rule | NSE industry-classification mapping |
| **Only 2 FY data points** | sales_growth_2y/3y/5y, profit_growth_2y/3y/5y, eps_growth_3y — 7 metrics | Backfill older filings, or accrue |

On the last row: the 31-MAR-2026 and 31-MAR-2025 filings **do** yield FY26 and FY25 annual figures via their YTD columns, so `sales_growth_1y` and `profit_growth_1y` work from annual data. `2y` needs FY24, which isn't in the file. Quarterly YoY works because both 30-JUN-2025 and 30-JUN-2026 are present.

### 11.4 Scorecard

| Table | Fillable from the CSV alone |
|---|---|
| `companies` | 6 / 8 — missing sector, is_financial_co |
| `financials` | **25 / 25** |
| `daily_prices` | **0 / 4** — entirely bhavcopy |
| `company_metrics` | ~23 / 45 — blocked by price (10), shareholding (5), history (7) |

Computable today with nothing but this CSV: **ROE, ROCE, ROA, operating margin, net margin, debt/equity, interest coverage, book value, OCF/PAT, FCF, all TTM absolutes, 1Y growth, quarterly YoY.**

Adding the bhavcopy loader is one small job and unlocks 10 more metrics including P/E and P/B — highest-leverage next step by a wide margin.

---

## Not investment advice

Derived from public NSE filings. Parsers break silently, filers restate figures, balance sheets arrive half-yearly, and an LLM verdict is an opinion generated from a prompt — not a recommendation. Every `financials` row keeps its `source_url` so any number can be checked against the original filing. I'm not a financial advisor.
