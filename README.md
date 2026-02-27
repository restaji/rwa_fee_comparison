# RWA Fee Comparison — Backend Documentation

A multi-exchange slippage & fee comparison API for Real-World Assets (RWA) perp DEXs.

## Supported Assets

| Key | Name | Class |
|---|---|---|
| `XAU` | XAU/USD | Commodity |
| `XAG` | XAG/USD | Commodity |
| `EURUSD` | EUR/USD | Forex |
| `GBPUSD` | GBP/USD | Forex |
| `USDJPY` | USD/JPY | Forex |
| `AAPL` `MSFT` `GOOG` `AMZN` `META` `NVDA` `TSLA` `COIN` | MAG7 + COIN | Stock |
| `SPY` `QQQ` | Index | Index |

## Supported Exchanges

| Exchange | Type | Orderbook |
|---|---|---|
| **Hyperliquid** | Perp DEX (Arbitrum L1) | Real L2 book (`xyz:` dex) |
| **Lighter** | Perp DEX | Real L2 book |
| **Aster** | Perp DEX | Real L2 book |
| **Extended** | Perp DEX (Starknet) | Real L2 book |
| **Avantis** | Oracle-based Perp DEX | Synthetic (no traditional book) |
| **Ostium** | Oracle-based Perp DEX | Oracle price + dynamic spread |

---

## Calculation Processes

All values are expressed in **basis points (bps)** unless stated otherwise.  
`1 bps = 0.01%`

---

### 1. Spread

The spread measures how much the execution price deviates from the true mid price.

#### Orderbook-Based Exchanges (Hyperliquid, Lighter, Aster, Extended)

The spread is calculated by **walking the orderbook** — consuming levels until the full order size is filled.

```
mid_price           = (best_bid + best_ask) / 2
avg_execution_price = total_cost_filled / total_qty_filled   ← walk the book
slippage_bps        = abs((avg_execution_price - mid_price) / mid_price) × 10,000
```

- **Buy side**: walks the ask levels (lowest price first)
- **Sell side**: walks the bid levels (highest price first)
- The **average** of both sides is reported as `slippage_bps`

#### Hyperliquid — Precision Cascade

Hyperliquid's `xyz` dex uses a two-step precision strategy to handle thin orderbooks (especially for RWA forex):

1. **Default precision** (no `nSigFigs`) — most granular, best prices
2. **`nSigFigs=4`** — aggregated levels, much deeper liquidity

If the default book has < $1M of visible depth on either side, the API automatically falls back to `nSigFigs=4` and preserves the **true best bid/ask** from the granular book for an accurate mid-price and spread anchor.

```
If bids_usd < 1,000,000 OR asks_usd < 1,000,000:
    → Fetch nSigFigs=4 book
    → store true_best_bid / true_best_ask from the granular book
    → use aggregated book for slippage walk
```

#### Ostium — Dynamic Spread (Pade Approximation)

Ostium is oracle-based. Its spread is **not** from a real orderbook but is calculated using a Solidity-compatible formula:

```
spread_bps = (market_spread / 2) + (decayed_volume + trade_size / 2) × priceImpactK / 1e27 × 10,000
```

- `market_spread` = the raw `(ask - bid) / mid` from the oracle price feed
- `decayed_volume` = existing open interest decayed using a **Pade approximation** to simulate natural volume decay over time
- `priceImpactK` = per-pair constant from the pairs API
- Computed separately for **buy** (open long) and **sell** (close long), then averaged

#### Avantis — Dynamic Spread (Risk API)

Avantis queries a dedicated risk API that returns a real-time `spreadP` value per pair and direction:

```
GET https://risk-api.avantisfi.com/spread/dynamic
    ?pairIndex=<id>&positionSizeUsdc=<size_in_wei>&isLong=true&isPnl=false

spread_bps = spreadP / 1e10 × 100
```

If dynamic spread is not available for the pair, it falls back to the static `spreadP` from the socket API.

---

### 2. Trading Fees (Open & Close Fee)

All fees are in **bps**.

#### Hyperliquid

Fees are computed dynamically from three public API calls — **no hardcoded values**:

| API | Purpose |
|---|---|
| `perpDexs` | `deployerFeeScale` for the `xyz` dex |
| `userFees` (zero address) | `userCrossRate` (taker) and `userAddRate` (maker) |
| `metaAndAssetCtxs` | Per-asset `growthMode` flag |

```
scale_if_hip3  = (deployerFeeScale + 1)    if deployerFeeScale < 1
               = (deployerFeeScale × 2)    if deployerFeeScale ≥ 1

growth_scale   = 0.1    if growthMode == "enabled"   ← 90% discount
               = 1.0    otherwise

taker_fee_bps  = userCrossRate × 100 × scale_if_hip3 × growth_scale × 100
maker_fee_bps  = userAddRate   × 100 × scale_if_hip3 × growth_scale × 100
```

#### Lighter, Aster, Extended

Fees are fetched dynamically from each exchange's public API at request time, cached for 5 minutes.

#### Ostium

Fee is loaded from the Ostium `pairs` API (`takerFeeP` / `makerFeeP`):

```
taker_fee_bps = takerFeeP / 10,000
```

Season overrides are applied if an active season has a `newFee` for the asset (from the `seasons/current` API).

#### Avantis — Skew-Adjusted Opening Fee

The opening fee is not flat — it depends on the **open interest imbalance** between longs and shorts:

```
For a long:
    open_interest_pct  = floor(100 × short_OI / (long_OI + position_size + short_OI))
    pct_index          = min(floor(open_interest_pct / 10), len(skewEqParams) - 1)
    param1, param2     = skewEqParams[pct_index]
    open_fee_bps       = ((param1 × open_interest_pct + param2) / 10,000) × 100
```

The closing fee is a flat rate from `closeFeeP × 100`.

---

### 3. Max Leverage

| Exchange | Source |
|---|---|
| **Hyperliquid** | `metaAndAssetCtxs` API (`maxLeverage` per asset) |
| **Ostium** | `pairs` API (`maxLeverage`, `makerMaxLeverage`, or group `maxLeverage`) — divided by 100 |
| **Avantis** | `pairInfos[pairIndex].leverages.maxLeverage` from socket API |
| **Lighter/Aster/Extended** | Exchange-specific market metadata APIs |

---

### 4. Margin / Funding Fee (Rollover)

The holding cost for keeping a position open.

#### Hyperliquid

The 1H funding rate is fetched via `metaAndAssetCtxs` (`funding` field per asset):

```
funding_1h_pct  = funding_raw × 100          ← fraction → percentage
funding_24h_pct = floor(funding_1h_pct × 24 × 1,000,000) / 1,000,000
```

Positive = longs pay; Negative = longs receive.

#### Ostium — Rollover Fee (Arbitrum Block-based)

The rollover fee is accrued per Arbitrum block, using a live block rate fetched from the Arbitrum public RPC:

```
blocks_per_day = (current_block - ref_block) / (current_time - ref_funding_time) × 86,400

long_rate_pct_24h  = rolloverFeePerBlock × blocks_per_day / 1e18 × 100
short_rate_pct_24h = −long_rate_pct_24h   (clamped to 0 if negativeRollover not allowed)
```

- `rolloverFeePerBlock` — per-pair constant from the pairs API (raw 18-decimal integer)
- Falls back to `345,600 blocks/day` (~4 blk/s) if the RPC is unavailable

#### Avantis — Margin Fee

Comes directly from the `marginFee` field in the socket API response:

```
marginFee.long  → funding_1h_long_pct   (% of notional per hour)
marginFee.short → funding_1h_short_pct

funding_24h_long_pct  = floor(funding_1h_long_pct  × 24 × 1,000,000) / 1,000,000
funding_24h_short_pct = floor(funding_1h_short_pct × 24 × 1,000,000) / 1,000,000
```

---

### 5. Total Cost & Winner

The final total cost is the sum of all components for a round-trip trade:

```
effective_spread_bps = slippage_bps × 2         ← for orderbook exchanges (open + close)
                     = slippage_bps              ← for Avantis (spread already one-way)

total_cost_bps = effective_spread_bps + open_fee_bps + close_fee_bps
```

The **winner** is the exchange with the **lowest** `total_cost_bps` for a fully-filled order.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/assets` | List all supported assets and which exchanges support them |
| `POST` | `/api/compare` | Compare all exchanges (JSON body: `asset`, `order_size`, `order_type`, `direction`) |
| `GET` | `/api/compare/<asset>` | Same as POST but via query params: `?size=1000000&order_type=taker&direction=long` |
| `WS` | `compare` event | WebSocket equivalent of the POST compare |

### Example

```bash
curl "http://localhost:5001/api/compare/USDJPY?size=1000000&order_type=taker&direction=long"
```

---

## Running Locally

```bash
pip install -r requirements.txt
python app.py
```

Server starts on port `5001` by default (overridable via `PORT` env var).

## Deploying to Railway

1. Push this folder to a GitHub repository
2. Create a new Railway project → **Deploy from GitHub repo**
3. Railway auto-detects `requirements.txt` and `Procfile`
4. Add your environment variables in Railway's **Variables** tab (copy from `.env`)
5. Railway generates a public URL — plug it into your frontend

The `Procfile` runs:
```
web: gunicorn -k eventlet -w 1 app:app
```
`eventlet` is required for WebSocket (SocketIO) support in production.
