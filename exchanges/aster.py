#!/usr/bin/env python3
"""
exchanges/aster.py - Aster DEX exchange integration.

Covers: orderbook (depth), fees (authenticated /commissionRate endpoint),
and max leverage (from leverageOiRemainingMap API).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

from models import StandardizedOrderbook, ExecutionCalculator

import os


class AsterAPI:
    BASE_URL     = "https://fapi.asterdex.com/fapi/v1"
    LEVERAGE_API = "https://www.asterdex.com/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining"
    SYMBOLS_API  = "https://www.asterdex.com/bapi/futures/v1/public/future/simple/symbols"
    FUNDING_API  = "https://www.asterdex.com/bapi/futures/v1/public/future/common/real-time-funding-rate"

    # Map short tickers -> Aster symbol names
    SYMBOL_MAP: Dict[str, str] = {
        "XAG":    "XAGUSDT",
        "XAU":    "XAUUSDT",
        "EURUSD": "EURUSDUSDT",
        "USDJPY": "USDTJPYUSDT",
        "GBPUSD": "GBPUSDUSDT",
        "NVDA":   "NVDAUSDT",
        "TSLA":   "TSLAUSDT",
        "AAPL":   "AAPLUSDT",
        "MSFT":   "MSFTUSDT",
        "COIN":   "COINUSDT",
        "AMZN":   "AMZNUSDT",
        "GOOG":   "GOOGUSDT",
        "META":   "METAUSDT",
        "HOOD":   "HOODUSDT",
        "SPY":    "SPYUSDT",
        "QQQ":    "QQQUSDT",
    }

    def __init__(self):
        self.headers = {'Content-Type': 'application/json'}
        self.leverage_cache: Dict        = {}   # symbol -> max_leverage
        self.leverage_cache_loaded: Dict = {}   # symbol -> bool
        self.fee_cache: Dict             = {}   # symbol -> {taker_fee_bps, maker_fee_bps}

        self.api_key    = os.getenv("ASTER_API_KEY",    "")
        self.secret_key = os.getenv("ASTER_SECRET_KEY", "")
        self.session    = requests.Session()
        if self.api_key:
            self.session.headers.update({'X-MBX-APIKEY': self.api_key})

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _sign(self, params: Dict) -> str:
        """Generate HMAC SHA256 signature for request parameters."""
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _signed_request(self, method: str, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make a signed API request."""
        params                = params or {}
        params['timestamp']   = int(time.time() * 1000)
        params['recvWindow']  = 5000
        params['signature']   = self._sign(params)
        url = f"{self.BASE_URL}{endpoint}"
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, timeout=30)
            else:
                response = self.session.post(url, data=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Aster API request failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def get_fees(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Get taker and maker fees for a symbol (authenticated /commissionRate).
        Returns (taker_bps, maker_bps) or (None, None).
        """
        if symbol in self.fee_cache:
            fees = self.fee_cache[symbol]
            return (fees.get('taker_fee_bps'), fees.get('maker_fee_bps'))

        if not self.api_key or not self.secret_key:
            print("Aster API credentials not configured in .env")
            return (None, None)

        try:
            response = self._signed_request('GET', '/commissionRate', {'symbol': symbol})
            if not response:
                return (None, None)

            maker_rate = float(response.get('makerCommissionRate', 0))
            taker_rate = float(response.get('takerCommissionRate', 0))

            # Doubles applied: taker fill on Aster matches Hyperliquid/Extended convention
            taker_bps = taker_rate * 10000 * 2
            maker_bps = maker_rate * 10000 * 2

            self.fee_cache[symbol] = {'taker_fee_bps': taker_bps, 'maker_fee_bps': maker_bps}
            return (taker_bps, maker_bps)
        except Exception as e:
            print(f"Error fetching Aster fees for {symbol}: {e}")
            return (None, None)

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def _fetch_max_leverage(self, symbol: str) -> Optional[int]:
        """
        Fetch max leverage from leverageOiRemainingMap.
        Highest key in the map = max available leverage.
        """
        if self.leverage_cache_loaded.get(symbol):
            return self.leverage_cache.get(symbol)

        try:
            url      = f"{self.LEVERAGE_API}?symbol={symbol}"
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('data'):
                    leverage_map = data['data'].get('leverageOiRemainingMap', {})
                    if leverage_map:
                        max_lev = max(int(k) for k in leverage_map.keys())
                        self.leverage_cache[symbol]        = max_lev
                        self.leverage_cache_loaded[symbol] = True
                        return max_lev
        except Exception as e:
            print(f"Error fetching Aster max leverage for {symbol}: {e}")

        self.leverage_cache_loaded[symbol] = True
        self.leverage_cache[symbol]        = None
        return None

    def get_max_leverage(self, symbol: str) -> Optional[int]:
        """Get max leverage for a symbol from API."""
        return self._fetch_max_leverage(symbol)

    # ------------------------------------------------------------------
    # Funding fee
    # ------------------------------------------------------------------
    def _resolve_symbol(self, symbol: str) -> str:
        """Map short ticker (e.g. 'XAG') to Aster full symbol (e.g. 'XAGUSDT')."""
        return self.SYMBOL_MAP.get(symbol.upper(), symbol)

    def get_funding_fee(self, symbol: str) -> Dict:
        """
        Fetch current funding fee for *symbol* from the real-time-funding-rate API.
        `lastFundingRate` is the 4H rate as a plain fraction (e.g. -0.00027586).
          1H bps  = rate / 4 * 100
          24H bps = rate * 6 * 100  (floored to 2 dp)
        """
        try:
            resp = requests.get(
                self.FUNDING_API,
                params={'symbol': self._resolve_symbol(symbol)},
                headers=self.headers,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                entry = (data.get('data') or [{}])[0]
                rate_4h_raw     = float(entry.get('lastFundingRate', 0) or 0)
                funding_4h_pct  = rate_4h_raw * 100
                funding_1h_pct  = funding_4h_pct / 4
                funding_24h_pct = math.floor(funding_4h_pct * 6 * 1_000_000) / 1_000_000
                return {
                    'funding_fee_1h_pct':  round(funding_1h_pct, 6),
                    'funding_fee_24h_pct': funding_24h_pct,
                }
        except Exception as e:
            print(f"Aster funding fee error for {symbol}: {e}")
        return {'funding_fee_1h_pct': 0.0, 'funding_fee_24h_pct': 0.0}

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        url    = f"{self.BASE_URL}/depth"
        params = {'symbol': symbol, 'limit': 1000}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            if response.status_code != 200:
                return None
            data = response.json()
            if not data.get('bids') or not data.get('asks'):
                return None
            bids = [{'price': float(l[0]), 'qty': float(l[1])} for l in data['bids']]
            asks = [{'price': float(l[0]), 'qty': float(l[1])} for l in data['asks']]
            return {'bids': bids, 'asks': asks}
        except Exception:
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Aster orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if not bids or not asks:
            return None

        best_bid = bids[0]['price']
        best_ask = asks[0]['price']
        if best_bid <= 0 or best_ask <= 0:
            return None

        mid_price = (best_bid + best_ask) / 2

        return StandardizedOrderbook(
            bids=bids, asks=asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self, orderbook: Dict, order_size_usd: float, symbol: str = None
    ) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(symbol) if symbol else (None, None)
        calc_fee = taker_fee_bps if taker_fee_bps is not None else 0.0

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=calc_fee,
            close_fee_bps=0.0,
        )

        if result:
            result['fee_bps']       = taker_fee_bps
            result['maker_fee_bps'] = maker_fee_bps
            if symbol:
                result['max_leverage'] = self.get_max_leverage(symbol)
                funding = self.get_funding_fee(symbol)
                result.update(funding)

        return result
