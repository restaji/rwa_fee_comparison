#!/usr/bin/env python3
"""
comparator.py - FeeComparator orchestrator.

Calls all exchange APIs in parallel conceptually and returns a unified
comparison result with total costs and winner determination.
"""
from __future__ import annotations

from typing import Dict, Optional

from models import ASSETS
from exchanges.hyperliquid import HyperliquidAPI
from exchanges.lighter      import LighterAPI
from exchanges.aster        import AsterAPI
from exchanges.avantis      import AvantisAPI
from exchanges.ostium       import OstiumAPI
from exchanges.extended     import ExtendedAPI


class FeeComparator:
    def __init__(self):
        self.hyperliquid = HyperliquidAPI()
        self.lighter     = LighterAPI()
        self.aster       = AsterAPI()
        self.avantis     = AvantisAPI()
        self.ostium      = OstiumAPI()
        self.extended    = ExtendedAPI()

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
            'symbols': {
                'hyperliquid': config.hyperliquid_symbol,
                'lighter':     config.symbol_key if config.lighter_market_id else None,
                'aster':       config.aster_symbol,
                'avantis':     config.symbol_key,
                'ostium':      config.ostium_symbol,
                'extended':    config.extended_symbol,
            },
        }

        # --- Hyperliquid ---
        if config.hyperliquid_symbol:
            hl_result = self.hyperliquid.get_optimal_execution(config.hyperliquid_symbol, order_size_usd)
            if hl_result:
                result['hyperliquid'] = hl_result
                result['symbols']['hyperliquid'] = hl_result['symbol']

        # --- Lighter ---
        if config.lighter_market_id:
            ob     = self.lighter.get_orderbook(config.lighter_market_id)
            lr     = self.lighter.calculate_execution_cost(ob, order_size_usd, market_id=config.lighter_market_id)
            if lr:
                lr['symbol'] = config.symbol_key
            result['lighter'] = lr

        # --- Aster ---
        if config.aster_symbol:
            ob  = self.aster.get_orderbook(config.aster_symbol)
            ar  = self.aster.calculate_execution_cost(ob, order_size_usd, symbol=config.aster_symbol)
            if ar:
                ar['symbol'] = config.aster_symbol
            result['aster'] = ar

        # --- Avantis ---
        is_long = (direction.lower() == 'long')
        av      = self.avantis.calculate_cost(asset_key, order_size_usd, is_long=is_long)
        if av:
            av['symbol'] = config.symbol_key
        result['avantis'] = av

        # --- Ostium ---
        if config.ostium_symbol:
            os_r = self.ostium.calculate_execution_cost(config.ostium_symbol, order_size_usd)
            if os_r:
                os_r['symbol'] = config.ostium_symbol
            result['ostium'] = os_r

        # --- Extended ---
        if config.extended_symbol:
            ob  = self.extended.get_orderbook(config.extended_symbol)
            ex  = self.extended.calculate_execution_cost(ob, order_size_usd, market=config.extended_symbol)
            if ex:
                ex['symbol'] = config.extended_symbol
            result['extended'] = ex

        # Maker order override: zero slippage for orderbook-based exchanges
        if order_type == 'maker':
            for name in ['hyperliquid', 'lighter', 'aster', 'extended']:
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
        lighter_taker,  lighter_maker  = self.lighter.get_fees(config.lighter_market_id)  if config.lighter_market_id  else (None, None)
        aster_taker,    aster_maker    = self.aster.get_fees(config.aster_symbol)          if config.aster_symbol       else (None, None)
        extended_taker, extended_maker = self.extended.get_fees(config.extended_symbol)    if config.extended_symbol    else (None, None)
        hl_taker,       hl_maker       = self.hyperliquid.get_fees(config.hyperliquid_symbol) if config.hyperliquid_symbol else (None, None)

        if order_type == 'maker':
            fee_structure = {
                'hyperliquid': {'open': hl_maker,       'close': hl_maker},
                'lighter':     {'open': lighter_maker,  'close': lighter_maker},
                'aster':       {'open': aster_maker,    'close': aster_maker},
                'extended':    {'open': extended_maker, 'close': extended_maker},
            }
        else:
            fee_structure = {
                'hyperliquid': {'open': hl_taker,       'close': hl_taker},
                'lighter':     {'open': lighter_taker,  'close': lighter_taker},
                'aster':       {'open': aster_taker,    'close': aster_taker},
                'extended':    {'open': extended_taker, 'close': extended_taker},
            }

        os_data = result.get('ostium')
        if os_data:
            fee_structure['ostium'] = {'open': os_data.get('fee_bps'), 'close': 0.0}

        av = result.get('avantis')
        if av:
            fee_structure['avantis'] = {'open': av.get('open_fee_bps', 0), 'close': av.get('close_fee_bps', 0)}

        # Standardize opening/closing slippage by direction
        is_long = (direction == 'long')
        for name in ['hyperliquid', 'lighter', 'aster', 'ostium', 'extended']:
            ex_data = result.get(name)
            if ex_data:
                buy_slip  = ex_data.get('buy_slippage_bps',  0.0)
                sell_slip = ex_data.get('sell_slippage_bps', 0.0)
                ex_data['opening_slippage_bps'] = buy_slip  if is_long else sell_slip
                ex_data['closing_slippage_bps'] = sell_slip if is_long else buy_slip
                ex_data['slippage_type']         = 'opening_closing'

        # Calculate totals and collect for winner ranking
        for name in ['hyperliquid', 'lighter', 'aster', 'avantis', 'ostium', 'extended']:
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
