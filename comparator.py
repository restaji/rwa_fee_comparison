#!/usr/bin/env python3
"""
comparator.py - FeeComparator orchestrator.

Calls all exchange APIs in parallel conceptually and returns a unified
comparison result with total costs and winner determination.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

log = logging.getLogger(__name__)

from models import ASSETS
from exchanges.hyperliquid import HyperliquidAPI
from exchanges.lighter      import LighterAPI
from exchanges.aster        import AsterAPI
from exchanges.avantis      import AvantisAPI
from exchanges.ostium       import OstiumAPI
from exchanges.extended     import ExtendedAPI
from exchanges.edgex        import EdgeXAPI
from exchanges.grvt         import GRVTAPI


class FeeComparator:
    def __init__(self):
        self.hyperliquid = HyperliquidAPI()
        self.lighter     = LighterAPI()
        self.aster       = AsterAPI()
        self.avantis     = AvantisAPI()
        self.ostium      = OstiumAPI()
        self.extended    = ExtendedAPI()
        self.edgex       = EdgeXAPI()
        self.grvt        = GRVTAPI()

    def compare_asset(
        self,
        asset_key: str,
        order_size_usd: float,
        order_type: str = 'taker',
        direction: str  = 'long',
    ) -> Optional[Dict]:
        """
        Compare execution cost across all exchanges for a given asset.

        Args:
            asset_key:      Asset symbol (e.g. 'XAU', 'NVDA')
            order_size_usd: Order size in USD
            order_type:     'taker' or 'maker'
            direction:      'long' or 'short'
        """
        if asset_key not in ASSETS:
            return None

        config = ASSETS[asset_key]
        result = {
            'asset':         config.name,
            'symbol_key':    config.symbol_key,
            'order_size_usd': order_size_usd,
            'order_type':    order_type,
            'direction':     direction,
            'hyperliquid':   None,
            'lighter':       None,
            'aster':         None,
            'avantis':       None,
            'ostium':        None,
            'extended':      None,
            'edgex':         None,
            'grvt':          None,
            'symbols': {
                'hyperliquid': config.hyperliquid_symbol,
                'lighter':     config.symbol_key if config.lighter_market_id else None,
                'aster':       config.aster_symbol,
                'avantis':     config.symbol_key,
                'ostium':      config.ostium_symbol,
                'extended':    config.extended_symbol,
                'edgex':       (config.edgex_symbol or config.symbol_key) if config.edgex_contract_id else None,
                'grvt':        config.symbol_key if config.grvt_instrument else None,
            },
        }

        is_long = (direction.lower() == 'long')

        def fetch_hyperliquid():
            if not config.hyperliquid_symbol:
                return 'hyperliquid', None, None
            try:
                r = self.hyperliquid.get_optimal_execution(config.hyperliquid_symbol, order_size_usd)
                sym = (r.get('symbol') or config.hyperliquid_symbol) if r else None
                return 'hyperliquid', r, sym
            except Exception as e:
                print(f"Hyperliquid error: {e}")
                return 'hyperliquid', None, None

        def fetch_lighter():
            if not config.lighter_market_id:
                return 'lighter', None
            ob = self.lighter.get_orderbook(config.lighter_market_id)
            r  = self.lighter.calculate_execution_cost(ob, order_size_usd, market_id=config.lighter_market_id)
            if r:
                r['symbol'] = config.symbol_key
            return 'lighter', r

        def fetch_aster():
            if not config.aster_symbol:
                return 'aster', None
            ob = self.aster.get_orderbook(config.aster_symbol)
            r  = self.aster.calculate_execution_cost(ob, order_size_usd, symbol=config.aster_symbol)
            if r:
                r['symbol'] = config.aster_symbol
            return 'aster', r

        def fetch_avantis():
            r = self.avantis.calculate_cost(asset_key, order_size_usd, is_long=is_long)
            if r:
                r['symbol'] = r.get('symbol') or config.symbol_key
            return 'avantis', r

        def fetch_ostium():
            if not config.ostium_symbol:
                return 'ostium', None
            r = self.ostium.calculate_execution_cost(config.ostium_symbol, order_size_usd)
            if r:
                r['symbol'] = config.ostium_symbol
            return 'ostium', r

        def fetch_extended():
            if not config.extended_symbol:
                return 'extended', None
            ob = self.extended.get_orderbook(config.extended_symbol)
            r  = self.extended.calculate_execution_cost(ob, order_size_usd, market=config.extended_symbol)
            if r:
                r['symbol'] = config.extended_symbol
            return 'extended', r

        def fetch_edgex():
            if not config.edgex_contract_id:
                return 'edgex', None
            sym = config.edgex_symbol or config.symbol_key
            r   = self.edgex.calculate_execution_cost(config.edgex_contract_id, order_size_usd, direction=direction, symbol=sym)
            return 'edgex', r

        def fetch_grvt():
            if not config.grvt_instrument:
                return 'grvt', None
            r = self.grvt.calculate_execution_cost(config.grvt_instrument, order_size_usd, direction=direction, symbol=config.symbol_key)
            return 'grvt', r

        tasks = [fetch_hyperliquid, fetch_lighter, fetch_aster, fetch_avantis,
                 fetch_ostium, fetch_extended, fetch_edgex, fetch_grvt]

        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {executor.submit(fn): fn for fn in tasks}
            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res[0] == 'hyperliquid':
                        _, r, sym = res
                        result['hyperliquid'] = r
                        if sym:
                            result['symbols']['hyperliquid'] = sym
                    else:
                        name, r = res
                        result[name] = r
                except Exception as e:
                    print(f"Exchange fetch error: {e}")

        # Maker order override: zero slippage for orderbook-based exchanges
        if order_type == 'maker':
            for name in ['hyperliquid', 'lighter', 'aster', 'extended', 'edgex', 'grvt']:
                ex_data = result.get(name)
                if ex_data:
                    ex_data['slippage_bps']      = 0.0
                    ex_data['buy_slippage_bps']  = 0.0
                    ex_data['sell_slippage_bps'] = 0.0
                    if 'buy'  in ex_data: ex_data['buy']['slippage_bps']  = 0.0
                    if 'sell' in ex_data: ex_data['sell']['slippage_bps'] = 0.0

        return result

    def calculate_totals_and_winner(
        self, result: Dict, asset_key: str, order_type: str = 'taker', direction: str = 'long'
    ) -> Dict:
        """
        Calculate total costs and determine winner for a comparison result.

        Args:
            result:     Raw comparison result from compare_asset()
            asset_key:  Asset symbol
            order_type: 'taker' or 'maker'
            direction:  'long' or 'short'

        Returns:
            Updated result dict with total_cost_bps and winner fields.
        """
        if not result or asset_key not in ASSETS:
            return result

        config    = ASSETS[asset_key]
        exchanges = []

        # Fetch fees dynamically
        lighter_taker,  lighter_maker  = self.lighter.get_fees(config.lighter_market_id)     if config.lighter_market_id   else (None, None)
        aster_taker,    aster_maker    = self.aster.get_fees(config.aster_symbol)             if config.aster_symbol        else (None, None)
        extended_taker, extended_maker = self.extended.get_fees(config.extended_symbol)       if config.extended_symbol     else (None, None)
        hl_taker,       hl_maker       = self.hyperliquid.get_fees(config.hyperliquid_symbol) if config.hyperliquid_symbol  else (None, None)
        edgex_taker,    edgex_maker    = self.edgex.get_fees(config.edgex_contract_id)        if config.edgex_contract_id   else (None, None)
        grvt_taker,     grvt_maker     = self.grvt.get_fees(config.grvt_instrument)           if config.grvt_instrument      else (None, None)

        if order_type == 'maker':
            fee_structure = {
                'hyperliquid': {'open': hl_maker,       'close': hl_maker},
                'lighter':     {'open': lighter_maker,  'close': lighter_maker},
                'aster':       {'open': aster_maker,    'close': aster_maker},
                'extended':    {'open': extended_maker, 'close': extended_maker},
                'edgex':       {'open': edgex_maker,    'close': edgex_maker},
                'grvt':        {'open': grvt_maker,     'close': grvt_maker},
            }
        else:
            fee_structure = {
                'hyperliquid': {'open': hl_taker,       'close': hl_taker},
                'lighter':     {'open': lighter_taker,  'close': lighter_taker},
                'aster':       {'open': aster_taker,    'close': aster_taker},
                'extended':    {'open': extended_taker, 'close': extended_taker},
                'edgex':       {'open': edgex_taker,    'close': edgex_taker},
                'grvt':        {'open': grvt_taker,     'close': grvt_taker},
            }

        os_data = result.get('ostium')
        if os_data:
            fee_structure['ostium'] = {'open': os_data.get('fee_bps'), 'close': 0.0}

        av = result.get('avantis')
        if av:
            fee_structure['avantis'] = {'open': av.get('open_fee_bps', 0), 'close': av.get('close_fee_bps', 0)}

        # Standardize opening/closing slippage by direction
        is_long = (direction == 'long')
        for name in ['hyperliquid', 'lighter', 'aster', 'ostium', 'extended', 'edgex', 'grvt']:
            ex_data = result.get(name)
            if ex_data:
                buy_slip  = ex_data.get('buy_slippage_bps',  0.0)
                sell_slip = ex_data.get('sell_slippage_bps', 0.0)
                ex_data['opening_slippage_bps'] = buy_slip  if is_long else sell_slip
                ex_data['closing_slippage_bps'] = sell_slip if is_long else buy_slip
                ex_data['slippage_type']         = 'opening_closing'

        # Calculate totals and collect for winner ranking
        for name in ['hyperliquid', 'lighter', 'aster', 'avantis', 'ostium', 'extended', 'edgex', 'grvt']:
            ex_data = result.get(name)
            if ex_data:
                fees           = fee_structure.get(name, {'open': 0, 'close': 0})
                slippage       = ex_data.get('slippage_bps', 0)
                effective_spread = slippage if name == 'avantis' else 2 * slippage
                f_open         = fees['open']
                f_close        = fees['close']
                total_cost     = effective_spread + (f_open or 0.0) + (f_close or 0.0)

                ex_data['effective_spread_bps'] = effective_spread
                ex_data['open_fee_bps']          = f_open
                ex_data['close_fee_bps']         = f_close
                ex_data['total_cost_bps']        = total_cost
                ex_data['exchange']              = name

                if total_cost is not None and ex_data.get('executed') != 'PARTIAL':
                    exchanges.append({'name': name, 'total_cost': total_cost, 'filled': ex_data.get('filled', True)})

        if exchanges:
            winner = min(exchanges, key=lambda x: x['total_cost'])
            result['winner']           = winner['name']
            result['winner_cost_bps']  = winner['total_cost']

        return result
