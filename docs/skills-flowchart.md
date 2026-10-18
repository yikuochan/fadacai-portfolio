# Fadacai Portfolio Skills — Complete Flowchart & Boundaries

一一解釋 10 個 skill 的流程、邊界、及彼此之間的關係。

---

## System Overview

```
                           Captain
                              |
                              v
                          Briefing (主入口)
                         /  |  |  \
                        /   |  |   \
                   Portfolio Stock Trade   EV-Check
                   Review    Analysis Review (驗證器)
                   |  \      |       /
                   |   \     |      /
                Options Trade-Journal
                Strategy
                   
                配置層: plan.md + feedback/*.md
                緩存層: macro, earnings, fundamentals, signals, ledgers
                MCP層:  yfinance, SEC, FMP, EODHD, technical, firstrade, polymarket
```

---

## Skill 1: Briefing（每日簡報，主入口）

**何時觸發：** `/briefing` (Quick)、`/briefing full` (Full)、`/briefing deep` (Deep)

### 流程

| 步驟 | 說明 | 必做 |
|------|------|------|
| **Step 0a** | 讀 `plan.md` + `feedback/*.md` | ✅ |
| **Step 0b** | 呼叫 `get_account_position` 取即時持倉 | ✅ |
| **Step 0c/0d** | journal 確認 + gap-fill | ✅ |
| **Step 0.5** | 讀 cache：macro-snapshot.json | ✅ |
| **Step 0.55** | 讀 cache：leading-indicators.json | ✅ |
| **Step 0.6** | 讀 cache：earnings-history.json | ✅ |
| **Step 0.65** | 讀 cache：fundamentals-snapshot.json | ✅ |
| **Step 0.67** | 讀 cache：news-articles.json | ✅ (Deep only) |
| **Step 0.68** | 讀 cache：twitter-signals.json | ✅ (Deep only) |
| **Step 0.7** | 執行 thesis-ledger.py due (到期驗收) | ✅ |
| **Phase 1** | Quick tier (5 sections: snapshot, movers, options, technical, key alerts) | ✅ |
| **Phase 2** | Full tier (+ 個股估值、情緒面、市場動態) | ❌ (full/deep only) |
| **Phase 3** | Deep tier (+ 訊號擷取、來源訊號、Codex 第二意見) | ❌ (deep only) |
| **EV-Check** | 呼叫 probability-honesty-checker agent | ✅ (Verdict 前) |
| **Thesis Ledger** | add/登錄本期新 thesis | ✅ (如有觸發點) |

### 邊界

**必須讀的 cache（缺失時標記 ⚠️ 但繼續）：**
- `macro-snapshot.json` (36h stale check)
- `earnings-history.json` (36h stale check)
- `earnings-dates.json` (即時財報日期)
- `fundamentals-snapshot.json` (24h stale check)

**決策門檻：**
- RSI 過高（>75）：**不標記**、不觸發任何減碼（強者愈強，per 2026-07-01）
- Earnings ±48h 窗內：**不執行任何期權新開倉**、不做 harvest 決策（等 earnings settle）
- 財報叢集（7d 內 >20%）：認列桶提前 harvest、暫停新增同窗曝險
- 飛輪檢查：梯級停利、Swing Risk、Harvest 配對 redeploy

**禁止：**
- 寫質性語言如「略偏正」「應該會」（改用 EV-check 驗證）
- 直接從 RSI 過高推導減碼
- 在 earnings window 內做新期權倉決策

**輸出：**
- Quick tier：5 個 section (~1 min)
- Full tier：+ 個股估值 (~3 min)
- Deep tier：+ 訊號擷取 + Codex (~5 min)

---

## Skill 2: Portfolio Review（組合全景）

**何時觸發：** `/portfolio-review` 或 `/portfolio-review --codex`

### 流程

| 步驟 | 說明 | 資料來源 |
|------|------|---------|
| **Step 0** | 統一規範 (讀 plan、持倉、journal) | MCP + 檔案 |
| **Step 0.5** | Cache load (macro, earnings, fundamentals, leading) | Cache JSON |
| **Step 0.6** | Thesis ledger 驗收 & 逾期掃描 | tools/thesis_ledger.py due |
| **Step A** | Portfolio summary (總市值、損益) | get_account_position |
| **Step B** | 板塊分配表 (Sector ETF 分類) | yfinance stock_info |
| **Step C** | Top 5 winners & losers | MCP 持倉數據 |
| **Step D** | Position sizing & 分桶對照 (vs plan.md) | 即時持倉 |
| **Step E** | Options summary + Swing Risk 健檢 | 選擇權 + 梯級停利檢查 |
| **Step F** | Key Alerts (過度集中、叢集、regime-break) | Multi-source |
| **Step G** | 個股趨勢 (平行 Agent 數據收集) | yfinance + technical + sentiment |
| **Step G3.5** | 三錨點公允價 (Fair PE) | fundamentals cache |
| **Step H** | SEC EDGAR (top 5 持倉) | SEC EDGAR MCP |
| **Step I** | FMP 市場動態 & PMCC 掃描 | FMP MCP + tools/pmcc_scan.py |
| **Section K** | 第一性檢查 (機率分布) | probability-honesty-checker agent |

### 邊界

**停利梯級（per feedback/tiered-profit-taking.md）：**
- L1: +30% → 減 1/3
- L2: +60% → 減下一級比例
- L3: +100% → 減到保底 30% runner
- L4+: 每 +50pp 級距

**飛輪檢查必列：**
| 動作 | 標的 | 觸發依據 | 執行結構 | 可掛單 |
|------|------|---------|---------|--------|
| Harvest | DDOG | revision 惡化 + β3.5 | GTC 賣單或 CC | 可掛 |
| Redeploy | MU | L1 revision 最陡 | Bull call spread | 可掛 |

**禁止：**
- 10%+ 倉位不標 🔴
- 信念桶不開 covered call 封頂（除了 >10% 紅線減碼）
- 梯級停利缺口無掛單

---

## Skill 3: Stock Analysis（個股研究）

**何時觸發：** `/stock-analysis PLTR` 或 `/stock-analysis PLTR --current --codex`

### 流程（高度複雜，分 3 個階段）

**階段 1：數據收集 (MCP + Agent)**
- 平行派 3 組 Agent (yfinance, SEC, technical+sentiment+EODHD)
- Agent 3 若 fundamentals cache miss/stale → 補跑 `fetch_fundamentals.py --ticker`

**階段 2：分析與估值**
| 數據 | 欄位 | 規則 |
|------|------|------|
| **A1 市場 PE** | pe_ratio | `== 0 or null` → N/A |
| **A2 PEG 錨** | peg × growth | AI 龍頭 1.5，其餘 1.0 |
| **A3 分析師** | PT ÷ fwdEPS | 優先 curr_fy → next_fy → approx |
| **A4 自建** | self_valuation.own_target | A4 不進 median、不進 EV |
| **Fair PE** | median(A1,A2,A3) | 不含 A4 |
| **EPS 修正** | revisions_up/down 30d | 共識上修 = positive lead signal |

**階段 3：第一性檢查（Step 0e 必做）**
- 核心 thesis：1 句可驗證命題
- 證偽條件：2-3 個 falsifiable 觀察點
- 機率分布 + EV：呼叫 probability-honesty-checker

**階段 4：Verdict & Ledger**
- Strong Buy / Buy / Hold / Sell / Avoid（必帶理由）
- 如有明確觸發點 → `thesis_ledger.py add`

### 邊界

**必須有逐字 quote（≤120 字）的訊號才入 thesis：**
- SEC 8-K 硬數字 → confidence: high
- 財報逐字稿量化句 → confidence: high
- News body 或 pricing_watch → confidence: medium
- 來源訊號（Twitter/Substack） → Trusted/Core only

**禁止：**
- A4 超過 50% 偏離 A3 時不猜機率（標 ⚠️⚠️ 異常大偏離）
- 套用 default 30/45/25 機率分布（必須走 probability-honesty-checker）
- 不帶 DCF 交叉（sanity check）

---

## Skill 4: Trade Review（兩週檢討 & 規則計分）

**何時觸發：** `/trade-review` 或 `/trade-review 4w` 或 `/trade-review since 2026-06-04`

### 流程

**Step 1：交易帳 ingest & 歸因**
```bash
python3 tools/trade_ledger.py snapshot-orders
python3 tools/trade_ledger.py ingest --range 2m
python3 tools/trade_ledger.py backfill-origin --since <期初>
python3 tools/trade_ledger.py stats
```

**Step 2：三個獨立指標**

| 指標 | 答什麼 | 資料來源 |
|------|--------|---------|
| **交易 α** | 進出決策對不對 | score --by origin-side |
| **持有 α** | 該不該繼續抱 | holding-alpha --window 90 |
| **Beta capture** | 行情好的時候吃到沒 | beta-capture --window 180 |

**Step 3：最佳/最差各 5 筆 + 逐筆追問**
- 當下有沒有訊號被忽略（revision、情緒、A4 旗標）
- Origin 判定可靠嗎
- 是規則問題還是執行問題

**Step 4：影子訊號驗收**
- A4 高估旗標（30 天後實際超額 α）
- Thesis 中途證偽（不等觸發日）
- 來源信用帳（hit_rate vs pnl_hit_rate）

**Step 5：更新規則命中率帳本**
- 編輯 `feedback/RULES-LEDGER.md`
- 失效 ≥2 次 → 狀態改 🔴 強制覆審

### 邊界

**必須至少達成 50% 歸因覆蓋率（journaled 期間的 origin 記錄）**
- 若 `unknown_sample` 有剩餘 → 翻當日 journal 人工補注

**禁止：**
- 不足 n=10 的分組單獨下結論（標 樣本不足）
- 在 3 個指標互相矛盾時只看其中一個（矛盾本身是重要發現）
- 重複使用舊 thesis 來合理化新虧損（檢查是否創建了新 thesis 繞過警示）

**旗標稽核（2026-07-25 發現最貴漏口）：**
```bash
python3 tools/trade_ledger.py flags
```
- `forced` 清單：每筆必須有 resolve-flag（減碼/出場/撤旗）
- `post_flag_fills`：警示後仍加碼 → 記錄理由
- `total_cost_since_flag`：往下走才算修好

---

## Skill 5: EV Check（機率分布誠實檢查）

**何時觸發：** 
- 獨立：`/ev-check` 或 `/ev-check 30d`
- 被呼叫：briefing、portfolio-review、stock-analysis Verdict 前必做

### 流程

**Step 1：收集 9 項輸入（缺一不可）**

| 項目 | 資料來源 | 缺值處理 |
|------|---------|---------|
| 1a. RSI 分布 | get_batch_indicators | 每 bucket 檔數 + % |
| 1b. 距 52w 高 | get_stock_info | 中位/最大/最小 |
| 1c. 波動 (5d/2d) | 計算 | 最大單日 |
| 1d. Binary catalyst + beat rate | earnings-history cache | N/8 beat + avg% |
| 1e. 集中度 | 持倉計算 | top 1 / top 5 / 最大板塊 |
| 1f. 板塊輪動 | get_sector_rotation | leading / lagging % |
| 1g. Sentiment | get_sentiment_trend | 7d/30d 對比 |
| 1h. Thesis 健康度 | plan.md + 新聞 | fundamental intact X/Y |
| 1i. Macro state | macro-snapshot cache | 5 series + regime_tag |

**Step 2-6：執行強制流程**
1. Input Enumeration 確認（9 項齊全，缺項拒絕計算）
2. 形狀反推（從 33/34/33 開始，逐條 nudge）
3. Conditional probabilities（每 catalyst 帶 base rate）
4. 三情境合成（sum = 100% 機械 check）
5. EV 計算 = Σ(機率 × 中點)
6. Self-audit checklist（全勾才算 showcase）

### 邊界

**禁止偷懶的 6 條紅線：**
1. ❌ 30/45/25、35/45/20、20/45/35 default mirror（無依據時禁止）
2. ❌ 寫「略偏正」「應該會」「中性偏多」（全部用數字替代）
3. ❌ 跳過 Step 1 直接給機率
4. ❌ Sum ≠ 100%
5. ❌ 中點手動偏移而非 (max+min)/2
6. ❌ 主 skill 自己套機率（等 agent 輸出）

**User push back 時的處理：**
- 不辯解、不重組原數字
- 重跑 agent，明確要求 audit checklist 全勾
- 發現偷懶 → 老實承認 + 顯示新算

---

## Skill 6: Trade Journal（交易日誌 & 自動偵測）

**何時觸發：** `/trade-journal log` 或 `/trade-journal auto` 或 `/trade-journal review`

### 流程

**Auto-detect（完整倉位變動偵測）：**

```
後倉位（即時）
     |
     v
  比較（後 - 前）
     |
     +---> 新建倉：{ticker} {qty} 股 🆕
     +---> 加碼/減碼：{ticker} {前qty} → {後qty}
     +---> 已清倉：{ticker} {前qty} 股 🔴
```

**對應計畫檢查：**
- ✅ 符合 plan.md 待辦 → 標記計畫項目
- ❓ 非計畫操作 → 提醒記錄原因

**輸出 journal 檔：** `journal/YYYY-MM-DD.md`
- 帳戶總覽（股票市值、選擇權、損益）
- 今日交易表（時間、操作、代碼、合約、數量、價格）
- 現股持倉快照（作為下次比較基準）
- 選擇權持倉快照

### 邊界

**核心規則：**
- 每個 journal 檔必須包含完整倉位快照（股票 + 選擇權）
- 無快照 → 下次無法偵測變動 → 斷線
- 倉位變動自動偵測優先於手動輸入

**禁止：**
- journal 檔缺失超過 1 天（偵測會有缺口）
- 倉位表格格式不一致（比較會失敗）

---

## Skill 7: Options Strategy（選擇權計算器）

**何時觸發：** `/options-strategy MU sell-put` 或 `/options-strategy PLTR AMD MU sell-put --codex`

### 流程

**Step 0：配置同步 + 倉位偵測**
- 讀 plan.md（計畫中的進場策略與 strikes）
- 呼叫 get_account_position（確認現有部位）

**多標的平行模式：**
- 派 Agent 子代理給每個 ticker
- 每個 Agent：get_stock_info + get_option_chain + get_technical_indicators + get_support_resistance
- 主 skill 計算所選策略 3-4 個 strike 的損益數據

**策略計算（每個 strike 一行）：**

| Strategy | Strike | 計算欄位 | 紀律 |
|----------|--------|--------|------|
| Sell Put | ATM-20% | Premium / Margin Est / Annualized Return / P(assign) | Margin ≤25% × strike×100；Quarter-Kelly ≤5% 帳戶 |
| Covered Call | OTM | Premium / Max Profit / Annualized Yield | 信念桶不開 CC 封頂（除了 >10% 紅線） |
| LEAPS | 10-25% ITM | Cost / Delta / Equiv Shares | <90 DTE 評估 roll；+80% 未實現評估 roll-up |
| Bull Call Spread | 加速領導者 | Max Loss = net debit | Max loss = Spread 寬度 × 100 - premium |
| Bull Put Spread | 支撐區 | Max Profit = net premium；Max Loss = 寬度 - premium | Short strike 貼支撐；寬 $10-20 |

**E_adj 波動率標準化比較：**
```
E_adj = 損益比 / ATR%
      = (Max Profit / Max Loss) / (ATR / 現價 × 100)
```
E_adj 越高 = 風險調整報酬越好，優先開倉

**飛輪定位（Step 5.5，開倉前必做）：**

| 策略 | 角色 | 適用桶 | 紀律 |
|------|------|--------|------|
| Covered Call | Harvest | 🟢 認列 ≥100 | strike 近 R1 / PT |
| Bull Call Spread | Redeploy | 加速領導者 | Max loss 定義風險 |
| Bull Put Spread | Redeploy | 支撐區進場 | Short 貼支撐 |
| LEAPS deep ITM | 信念 | 🔵 信念桶多年 | delta 0.80-0.88 |

**第一性檢查（Step 8，Recommendation 前必做）：**
- 方向 thesis：1 句可驗證命題
- 證偽條件：2-3 個 falsifiable
- IV / 機率分布：三情境表（樂/基/悲）
- Expected P/L vs 單純買現股

### 邊界

**財報 ±48h 禁令（options-leaps-playbook）：**
- 方向性建倉被 IV 扭曲定價 → 禁止
- 但本 skill 是有意識事件買方（吃 gamma / vol）→ 例外
- 賣方策略（BPS/BCS/CC）仍完全遵守禁令

**禁止：**
- 信念桶開 CC 封頂（破壞飛輪）
- 短 DTE OTM Call 不用 LEAPS 當樂透（vega 干擾）
- 跨財報短腿無特殊考量（見 feedback/options-leaps-playbook.md ±48h）
- 用 RSI 過高拒絕 bull call spread（method 本來是 revision 閘門）

---

## Skill 8: Todo（優先行動清單）

**何時觸發：** `/todo` 或 Captain 問「明天開盤要做什麼」

### 流程

**Step 1：取得即時數據**
- `get_account_position`（持倉）
- 讀 plan.md（待辦事項）
- 快速技術面（rsI/動能，非完整）

**Step 2：掃描 6 個維度，找需要行動項**

| 維度 | 優先級 | 例子 |
|------|-------|------|
| A. 選擇權急迫性 | 🔴 | <14d 到期；PMCC 距 strike <5% |
| B. 計畫中待執行 | 🔴 | plan.md ⏳ 項目觸發條件已達成 |
| C. 個股異常 | 🟡 | 日內跌 >5%（需根因分類）；日內漲 >8%（需 revision 確認） |
| D. 待開新倉位 | 🟡 | 討論過但尚未進場的 Spread |
| E. 待設警報 | 🟡 | 不需立刻動，但需盯的價位 |
| F. 飛輪/停利 | 🔴 | 梯級停利到價；Swing Risk 🔴 |

**Step 3：根因分類（必做）**

對任何弱勢 / 強勢訊號，先回答根因：
- (a) Thesis 破裂？→ 可執行汰弱
- (b) Earnings reaction？→ HOLD wait
- (c) Sector rotation？→ 評估板塊配置
- (d) Noise？→ 忽略

**Step 4：第一性檢查**
- 每個行動項：核心 thesis + 觸發條件 + 機率
- 寫不出 thesis → 改放「⏸ 不動」並說明原因

**輸出格式：**
```
## 🔴 立即處理
[開盤優先的項目]

## 🟡 盤中監控
[設好警報、等價格再決定的項目]

## 🟢 計畫進場
[條件確認後執行的 Spread + 倉位]

## ⏸ 不動
[簡短說明為何持有/不動]

## 📋 開盤時間軸
09:30 — ...
09:45 — ...
```

### 邊界

**精簡原則（禁止事項）：**
- 純持有觀察的倉位禁止繁複展開（簡短帶過即可）
- 同一標的多個操作禁止拆成多行（合併為一項）
- 禁止重複列已經在 briefing 或 portfolio-review 提過的行動
- 寫不出 thesis 或證偽條件的項目，禁止列入行動項（改放「⏸ 不動」）

**Phase 2（如有 --codex）：**
- Codex 獨立排 priorities（不看 Claude 的排序）
- 並排比較真實共識和真實分歧

---

## Skill 9: Event Vol Scan（事件前買方掃描）

**何時觸發：** `/event-vol-scan` 或 `/event-vol-scan 14 NVDA PLTR` 或 `/event-vol-scan 7`

### 流程

**Step 1：組建 Universe**
- 預設：持倉全部 + plan.md L1 On-Deck
- 宏觀事件：抓 CPI / FOMC / NFP 日期

**Step 2：機械掃描引擎**
```bash
python3 tools/event_vol_scan.py \
  --tickers <逗號清單> \
  --days <N，預設 14> \
  --account-value <live 帳戶總值>
```

回傳：過去 8Q 實際移動、隱含移動（VRP = 隱含/歷史）、beat rate + revisions

**Step 3：機械裁決**

| 裁決 | 條件 | 意義 |
|------|------|------|
| `BUY_CALL` | beat ≥ 7/8 + rev up ≥5×dn + avg surprise ≥+5% + VRP ≤ 1.2 | 方向凸性 |
| `STRADDLE` | VRP < 0.85 + 歷史中位 ≥ 4% | 雙買（隱含 < 歷史） |
| `SKIP_RICH` | VRP > 1.2 | 隱含太貴 |
| `EXCLUDE` | 5d 漲 >30% 或流動性差 | 淘汰 |
| `WATCH` | 無明確 edge | 列示不建議 |

**Step 4：判斷層審查（Claude 層）**
1. STRADDLE_RV 的 rv 灌水檢查（近 5d 若有 >3% 單日 → 降級 WATCH）
2. BUY_CALL 的 thesis 一致性（與現有持倉同題材不重複）
3. 末日進場時點：
   - 雙買：事件前 2-5 個交易日（IV ramp 前段）
   - 純末日 call：事件前 1 日或當日（最大 gamma）
   - 今天距事件 >5d → 價格警報 + 預定結構
4. 口數與預算：總 ≤ 2% 帳戶；單筆 1-2 口
5. 出場紀律（開倉時就寫死）：
   - 雙買：事件後首個交易日了結（IV crush 前）
   - 末日 call：+100% 賣半回本

**輸出（≤3 筆入選）：**

| # | 標的 | 事件 | 結構 | 成本 | Max Loss | 進場窗 | 出場紀律 |

---

## Skill 10: MCP Health（連線狀態檢查）

**何時觸發：** `/mcp-health`

### 流程

**Step 1：平行測試 7 個 MCP Server**

| Server | 測試工具 | 測試呼叫 | 狀態判定 |
|--------|---------|--------|---------|
| firstrade-server | get_account_balance | () | ✅ ok / ⚠️ degraded / ❌ failed |
| yfinance-advanced | get_stock_info | ("AAPL") | ✅ ok / ⚠️ degraded / ❌ failed |
| sec-edgar-mcp | get_company_info | ("AAPL") | ✅ ok / ⚠️ degraded / ❌ failed |
| fmp-mcp | getCompanyProfile | ("AAPL") | ✅ ok / ⚠️ degraded / ❌ failed |
| technical-mcp | get_technical_indicators | ("AAPL") | ✅ ok / ⚠️ degraded / ❌ failed |
| eodhd-mcp | get_sentiment_trend | ("AAPL.US", 7) | ✅ ok / ⚠️ degraded / ❌ failed |
| polymarket-mcp | get_trending_markets | () | ✅ ok / ⚠️ degraded / ❌ failed |

**Step 2：判定狀態**
- **✅ Healthy** — 正常返回數據
- **⚠️ Degraded** — 返回但數據不完整或響應異常慢
- **❌ Failed** — 呼叫失敗或超時

**Step 3：輸出狀態表**
- Server 列表 + 狀態 + 延遲 + 備註
- 健康: X/7 | 異常: X/7

**Step 4：失敗時手動重啟指令**
- uvx 工具（stdio subprocess）→ 重開 Claude session 恢復
- Docker 容器（非 stdio）→ `docker compose restart`
- FMP 旁路驗證 → `python3 tools/fmp_query.py`

### 邊界

**禁止：**
- 自動重啟（僅提示指令）
- 長期監控（只做一次性檢查）

---

## Cross-Skill Dependencies（技能間依賴）

```
Briefing 呼叫 →
  ├─ Portfolio Review → EV Check ✓
  ├─ Stock Analysis → EV Check ✓
  ├─ Trade Review → (thesis ledger update)
  └─ Trade Journal (自動偵測)

Trade Review 呼叫 →
  └─ Update RULES-LEDGER (feedback/)

Options Strategy 呼叫 →
  └─ PMCC Scan (tools/pmcc_scan.py)

Event Vol Scan 呼叫 →
  └─ Thesis Ledger (add 新 thesis)

Todo 呼叫 →
  └─ (無直接呼叫，但整合所有 briefing 邏輯)
```

---

## 共享資料層

| 層級 | 檔案 | 所有者 | 讀/寫 | TTL / 更新 |
|------|------|--------|-------|----------|
| **Cache** | macro-snapshot.json | fetch_macro.py | R/W (write 側) | 36h stale check |
| | earnings-history.json | earnings_history.py | R/W | 36h stale check |
| | earnings-dates.json | earnings_history.py | R (即時日期) | Daily |
| | fundamentals-snapshot.json | fetch_fundamentals.py | R/W | 24h TTL |
| | leading-indicators.json | fetch_leading.py | R/W | 20h TTL |
| | news-articles.json | fetch_news.py | R (Deep only) | 6h TTL |
| | twitter-signals.json | fetch_twitter.py | R/W (add-claim) | 20h TTL |
| **Ledgers** | thesis-ledger.json | tools/thesis_ledger.py | R/W (add/resolve) | Durable |
| | ev-ledger.json | tools/ev_ledger.py | R/W (add/resolve-due) | Durable |
| | source-credit.jsonl | tools/source_credit.py | R/W (add-claim) | Durable |
| | trade_ledger.* | tools/trade_ledger.py | R/W (snapshot/ingest) | Durable |
| **Config** | plan.md | User | R/W | Manual |
| | feedback/*.md | User | R/W | Manual |
| | journal/YYYY-MM-DD.md | Trade Journal | W | Daily |

---

## 決策矩陣（何時呼叫哪個 skill）

| 需求 | 呼叫 Skill | 理由 |
|------|-----------|------|
| 每日早檢查 | `/briefing` (quick) | 快速掃描，~1 min |
| 組合全景檢查 | `/portfolio-review` | Deep dive，包括倉位 + 技術 + 基本面 |
| 個股深度研究 | `/stock-analysis TICKER` | 三錨點估值 + 多 MCP 匯聚 |
| 兩週績效回顧 | `/trade-review` | 歸因 alpha + 規則計分 |
| 選擇權結構評估 | `/options-strategy TICKER` | E_adj 優先序 + 飛輪定位 |
| 優先事項清單 | `/todo` | 盤前行動規劃 |
| 財報前買方機會 | `/event-vol-scan` | 二元買方 (末日 call + straddle) |
| 連線除錯 | `/mcp-health` | MCP 健康狀況檢查 |
| 交易記錄 | `/trade-journal auto` | 自動倉位變動偵測 |

---

## 安全邊界（紅線）

🔴 **絕對禁止：**
1. 在 earnings ±48h 開新期權倉（方向性被 IV 扭曲）
2. 信念桶開 covered call 封頂（破壞飛輪）
3. 單一倉位 >10%（紅線強制標記）
4. 14-18 支倉位要求：>18 新倉砍一進一；<14 補 L1
5. RSI 過高觸發任何減碼動作（改由 revision 定方向）
6. 在 thesis-broken / 警示未決狀況下淨增倉（梯級停利缺口）

🟡 **需要人工確認：**
1. 倉位加碼（>3% 持倉 + 未在 plan 中）
2. 新開選擇權部位（需驗證 IV 環境）
3. 跨板塊複製曝險（相關度 >0.7）

✅ **自動執行（無人工確認）：**
1. 梯級停利掛單（GTC 限價）
2. Bull put spread 進場（支撐區 + 預定結構）
3. 期權自動掃描（PMCC、事件 straddle）

---

Captain，這份文檔涵蓋：
- ✅ 10 個 skill 的完整流程（Step by step）
- ✅ 每個 skill 的邊界和禁止事項
- ✅ 共享資料層與依賴關係
- ✅ 決策矩陣與安全邊界

可作為：
1. **Onboarding 指南** — 新人理解系統
2. **Code Review 檢查表** — 驗證每個 skill 遵守邊界
3. **操作手冊** — Captain 快速查詢何時用哪個 skill

需要視覺圖表嗎（待 Archify 修復後）？或這份 Markdown 已足以？
