#!/usr/bin/env python3
"""
exchanges/hyperliquid.py - Hyperliquid exchange integration.

Covers: orderbook (L2 book with precision cascade), fees (growth-mode aware),
and max leverage – all fetched dynamically from public APIs (no auth required).
"""
from __future__ import annotations

import math
import time
from typing import Dict, Optional, Tuple

import requests

from models import (
    HYPERLIQUID_GROWTH_MODE_SCALE,
    HYPERLIQUID_NO_GROWTH_MODE_SCALE,
    StandardizedOrderbook,
    ExecutionCalculator,
)


class HyperliquidAPI:
    def __init__(self):
        self.base_url = "https://api.hyperliquid.xyz/info"
        self.headers  = {'Content-Type': 'application/json'}

        self.max_leverages_cache: Dict = {}
        self.growth_mode_cache:   Dict = {}   # asset -> bool
        self.fee_cache:           Dict = {}   # asset -> (taker_bps, maker_bps)
        self.funding_cache:       Dict = {}   # asset -> funding_1h (fraction)

        self.deployer_fee_scale: Optional[float] = None
        self.base_taker_rate:    Optional[float] = None
        self.base_maker_rate:    Optional[float] = None

        self.last_metadata_fetch = 0
        self.last_fee_fetch      = 0
        self.metadata_cache_ttl  = 300   # 5 min
        self.fee_cache_ttl       = 300

        # xyz dex uses different names for some RWA assets
        self._symbol_aliases: Dict[str, str] = {
            "XAU": "GOLD", "XAG": "SILVER",
            "EURUSD": "EUR", "USDJPY": "JPY",
        }

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def _fetch_fee_config(self):
        """Fetch fee configuration from public APIs (no auth required)."""
        if time.time() - self.last_fee_fetch < self.fee_cache_ttl and self.deployer_fee_scale is not None:
            return

        try:
            # 1. deployerFeeScale from perpDexs API
            payload  = {"type": "perpDexs"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                for dex in response.json():
                    if dex and dex.get("name") == "xyz":
                        self.deployer_fee_scale = float(dex.get("deployerFeeScale", 1.0))
                        break

            # 2. Base fee rates from userFees API (public zero-address)
            payload  = {"type": "userFees", "user": "0x0000000000000000000000000000000000000001", "dex": "xyz"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                fees = response.json()
                self.base_taker_rate = float(fees.get("userCrossRate", 0.00045))
                self.base_maker_rate = float(fees.get("userAddRate",   0.00015))

            self.last_fee_fetch = time.time()
        except Exception as e:
            print(f"Error fetching HL fee config: {e}")

    def _fetch_metadata(self):
        """Fetch metadata to get max leverage and growth mode info."""
        if time.time() - self.last_metadata_fetch < self.metadata_cache_ttl and self.max_leverages_cache:
            return

        try:
            payload  = {"type": "metaAndAssetCtxs", "dex": "xyz"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data     = response.json()
                universe = []
                asset_ctxs = []
                if isinstance(data, list) and len(data) >= 1:
                    universe   = data[0].get("universe", [])
                    asset_ctxs = data[1] if len(data) > 1 else []
                elif isinstance(data, dict):
                    universe = data.get("universe", [])

                self.max_leverages_cache = {}
                self.growth_mode_cache   = {}
                self.funding_cache       = {}

                for i, item in enumerate(universe):
                    name        = item.get("name")
                    max_lev     = item.get("maxLeverage")
                    growth_mode = item.get("growthMode")
                    funding_1h  = float(asset_ctxs[i].get("funding", 0)) if i < len(asset_ctxs) else 0.0

                    if name:
                        stripped = name.replace("xyz:", "") if name.startswith("xyz:") else name
                        prefixed = f"xyz:{name}" if not name.startswith("xyz:") else name
                        for key in (name, stripped, prefixed):
                            self.max_leverages_cache[key] = max_lev
                            self.growth_mode_cache[key]   = growth_mode == "enabled"
                            self.funding_cache[key]       = funding_1h

                self.last_metadata_fetch = time.time()
        except Exception as e:
            print(f"Error fetching HL metadata: {e}")

    def _calculate_fees_for_asset(self, symbol: str) -> Tuple[float, float]:
        """
        Calculate taker and maker fees using the official Hyperliquid formula.
        All values from API – no hardcoding except protocol constants.

        Returns: (taker_fee_bps, maker_fee_bps)
        """
        self._fetch_fee_config()
        self._fetch_metadata()

        search_symbol = symbol if symbol.startswith("xyz:") else f"xyz:{symbol}"
        plain_symbol  = symbol.replace("xyz:", "") if symbol.startswith("xyz:") else symbol

        if search_symbol in self.fee_cache:
            return self.fee_cache[search_symbol]

        growth_enabled   = self.growth_mode_cache.get(search_symbol,
                           self.growth_mode_cache.get(plain_symbol, True))

        deployer_fee_scale = self.deployer_fee_scale if self.deployer_fee_scale is not None else 1.0
        base_taker         = self.base_taker_rate    if self.base_taker_rate    is not None else 0.00045
        base_maker         = self.base_maker_rate    if self.base_maker_rate    is not None else 0.00015

        scale_if_hip3 = (deployer_fee_scale + 1) if deployer_fee_scale < 1 else (deployer_fee_scale * 2)
        growth_scale  = HYPERLIQUID_GROWTH_MODE_SCALE if growth_enabled else HYPERLIQUID_NO_GROWTH_MODE_SCALE

        taker_fee_bps = base_taker * 100 * scale_if_hip3 * growth_scale * 100
        maker_fee_bps = base_maker * 100 * scale_if_hip3 * growth_scale * 100

        self.fee_cache[search_symbol] = (taker_fee_bps, maker_fee_bps)
        self.fee_cache[plain_symbol]  = (taker_fee_bps, maker_fee_bps)

        return (taker_fee_bps, maker_fee_bps)

    def get_fees(self, symbol: str) -> Tuple[float, float]:
        """
        Get taker and maker fees for a symbol (public API, no auth required).
        Returns: (taker_fee_bps, maker_fee_bps)
        """
        return self._calculate_fees_for_asset(symbol)

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def get_max_leverage(self, symbol: str) -> Optional[float]:
        self._fetch_metadata()
        return self.max_leverages_cache.get(f"xyz:{symbol}")

    # ------------------------------------------------------------------
    # Funding fee
    # ------------------------------------------------------------------
    def _resolve_xyz_name(self, symbol: str) -> str:
        """Map common RWA ticker to xyz dex internal name."""
        upper = symbol.upper().replace("XYZ:", "")
        return self._symbol_aliases.get(upper, upper)

    def get_funding_fee(self, symbol: str) -> Dict:
        """
        Return 1H and 24H funding fee in bps for *symbol*.
        `funding` from the API is a 1H rate as a plain fraction (e.g. 0.0000142).
        bps = fraction * 100.  24H = 1H * 24, floored to 2 dp.
        """
        self._fetch_metadata()
        xyz_name = self._resolve_xyz_name(symbol)
        funding_1h_raw = (
            self.funding_cache.get(f"xyz:{xyz_name}")
            or self.funding_cache.get(xyz_name)
            or 0.0
        )
        funding_1h_pct  = funding_1h_raw * 100
        funding_24h_pct = math.floor(funding_1h_pct * 24 * 1_000_000) / 1_000_000
        return {
            'funding_fee_1h_pct':  round(funding_1h_pct, 6),
            'funding_fee_24h_pct': funding_24h_pct,
        }

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def normalize_symbol(self, symbol: str) -> str:
        s = symbol.upper()
        if s == "NDX":
            return "kPW"
        return s

    def _fetch_coin(self, coin: str, n_sig_figs: Optional[int]) -> Optional[Dict]:
        payload = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            payload["nSigFigs"] = n_sig_figs

        try:
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code != 200:
                return None
            data = response.json()
            if not data:
                return None

            levels = data.get('levels', [])
            if not isinstance(levels, list) or len(levels) < 2:
                return None

            bids = levels[0] if isinstance(levels[0], list) else []
            asks = levels[1] if isinstance(levels[1], list) else []
            if not bids or not asks:
                return None

            def normalize_level(lvl):
                if isinstance(lvl, dict):
                    return lvl
                if isinstance(lvl, list) and len(lvl) >= 2:
                    return {'px': str(lvl[0]), 'sz': str(lvl[1])}
                return None

            formatted_bids = [x for x in (normalize_level(b) for b in bids) if x]
            formatted_asks = [x for x in (normalize_level(a) for a in asks) if x]

            return {'levels': [formatted_bids, formatted_asks]}
        except Exception:
            return None

    def get_orderbook(self, symbol: str, n_sig_figs: Optional[int] = None) -> Optional[Dict]:
        raw_symbol = self.normalize_symbol(symbol)
        coin       = raw_symbol if raw_symbol.startswith("xyz:") else f"xyz:{raw_symbol}"
        
        # Try requested or default precision first
        ob = self._fetch_coin(coin, n_sig_figs)
        if not ob or n_sig_figs is not None:
            return ob
            
        # Check if default precision has enough liquidity for a standard large order ($1M)
        # If not, fall back to nSigFigs=4 to get aggregated deep liquidity
        try:
            levels = ob.get('levels', [[], []])
            bids = levels[0]
            asks = levels[1]
            
            bids_usd = sum(float(b['px']) * float(b['sz']) for b in bids)
            asks_usd = sum(float(a['px']) * float(a['sz']) for a in asks)
            
            if bids_usd < 1_000_000 or asks_usd < 1_000_000:
                ob_agg = self._fetch_coin(coin, n_sig_figs=4)
                if ob_agg:
                    ob_agg['true_best_bid'] = float(bids[0]['px']) if bids else None
                    ob_agg['true_best_ask'] = float(asks[0]['px']) if asks else None
                    return ob_agg
        except Exception:
            pass
            
        return ob

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Hyperliquid orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None

        levels = orderbook.get('levels', [[], []])
        bids   = levels[0] if len(levels) > 0 else []
        asks   = levels[1] if len(levels) > 1 else []

        if not asks or not bids:
            return None

        try:
            best_bid = orderbook.get('true_best_bid') or float(bids[0].get('px', 0))
            best_ask = orderbook.get('true_best_ask') or float(asks[0].get('px', 0))
        except (ValueError, AttributeError, IndexError):
            return None

        if best_bid <= 0 or best_ask <= 0:
            return None

        def parse(entries, px_key='px', sz_key='sz'):
            result = []
            for e in entries:
                try:
                    result.append({'price': float(e.get(px_key, 0)), 'qty': float(e.get(sz_key, 0))})
                except (ValueError, AttributeError):
                    continue
            return result

        std_bids  = parse(bids)
        std_asks  = parse(asks)
        mid_price = (best_bid + best_ask) / 2

        return StandardizedOrderbook(
            bids=std_bids, asks=std_asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self,
        orderbook: Dict,
        order_size_usd: float,
        symbol: Optional[str] = None,
        anchor_mid_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator with dynamic fees."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook:
            return None

        if anchor_mid_price:
            std_orderbook = StandardizedOrderbook(
                bids=std_orderbook.bids, asks=std_orderbook.asks,
                best_bid=std_orderbook.best_bid, best_ask=std_orderbook.best_ask,
                mid_price=anchor_mid_price, timestamp=std_orderbook.timestamp,
            )

        if not symbol:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=taker_fee_bps,
            close_fee_bps=0.0,
        )

        if result:
            result['fee_bps']        = taker_fee_bps
            result['maker_fee_bps']  = maker_fee_bps
            max_levels_hit = (
                result['buy']['levels_used']  >= len(std_orderbook.asks) or
                result['sell']['levels_used'] >= len(std_orderbook.bids)
            )
            result['max_levels_hit'] = max_levels_hit

        return result

    def get_optimal_execution(self, symbol: str, order_size_usd: float) -> Optional[Dict]:
        """
        Cascade through orderbook precisions to find the best fill.

        Flow:
          1. Max precision (None) – best price accuracy; stop if fully filled.
          2. 4 significant figures – deeper book; use if not fully filled above.
        """
        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)

        precisions_to_try = [None, 4]
        final_result = None

        for n_sig in precisions_to_try:
            raw_book = self.get_orderbook(symbol, n_sig_figs=n_sig)
            if not raw_book:
                continue

            std_book = self.normalize_orderbook(raw_book)
            if not std_book:
                continue

            result = ExecutionCalculator.calculate_execution_cost(
                std_book, order_size_usd, open_fee_bps=taker_fee_bps
            )

            if result:
                final_result = result
                final_result['fee_bps']       = taker_fee_bps
                final_result['maker_fee_bps'] = maker_fee_bps
                final_result['sig_figs']      = "Maximum" if n_sig is None else n_sig

                if result['filled']:
                    break

        if final_result:
            final_result['is_xyz']       = True
            display_symbol               = symbol if "xyz" in str(symbol) else f"xyz:{symbol}"
            final_result['symbol']       = display_symbol
            final_result['max_leverage'] = self.get_max_leverage(symbol)
            funding                      = self.get_funding_fee(symbol)
            final_result.update(funding)

        return final_result
