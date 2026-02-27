#!/usr/bin/env python3
"""
app.py - Flask application, REST routes, and WebSocket handlers.

Entry point for the Multi-Exchange Slippage Comparison API.
Run with:  python app.py
"""
from __future__ import annotations

import eventlet
import sys
if sys.platform == 'darwin':
    import eventlet.hubs
    eventlet.hubs.use_hub('poll')  # kqueue is broken on macOS Python 3.9+
eventlet.monkey_patch()

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit

from models import ASSETS
from comparator import FeeComparator

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app      = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Initialize comparator (loads all exchange caches on startup)
comparator = FeeComparator()


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/assets', methods=['GET'])
def get_assets():
    """Return list of available assets."""
    assets_list = []
    for key, config in ASSETS.items():
        assets_list.append({
            'key':    key,
            'name':   config.name,
            'symbol': config.symbol_key,
            'exchanges': {
                'hyperliquid': config.hyperliquid_symbol is not None,
                'lighter':     config.lighter_market_id  is not None,
                'aster':       config.aster_symbol       is not None,
                'avantis':     True,
                'ostium':      config.ostium_symbol      is not None,
            },
        })
    return jsonify({'assets': assets_list})


@app.route('/api/compare', methods=['POST'])
def compare():
    """Compare slippage across exchanges for given asset and order size."""
    data       = request.json
    asset      = data.get('asset', '').upper()
    order_size = float(data.get('order_size', 1_000_000))
    order_type = data.get('order_type', 'taker').lower()
    direction  = data.get('direction',  'long').lower()

    if asset not in ASSETS:
        return jsonify({'error': f'Asset {asset} not found'}), 400

    result = comparator.compare_asset(asset, order_size, order_type=order_type, direction=direction)
    if not result:
        return jsonify({'error': 'Failed to compare asset'}), 500

    result = comparator.calculate_totals_and_winner(result, asset, order_type, direction)
    return jsonify(result)


@app.route('/api/compare/<asset>', methods=['GET'])
def compare_get(asset: str):
    """
    GET endpoint for comparing slippage across exchanges.

    URL: /api/compare/<asset>?size=1000000&order_type=taker&direction=long

    Parameters:
        asset (path):       Asset symbol (e.g. XAU, XAG, AAPL, NVDA)
        size (query):       Order size in USD (default: 1000000)
        order_type (query): 'taker' or 'maker'  (default: taker)
        direction (query):  'long'  or 'short'  (default: long)

    Examples:
        GET /api/compare/XAU?size=50000
        GET /api/compare/NVDA?size=1000000&order_type=maker
    """
    asset      = asset.upper()
    order_size = float(request.args.get('size',       1_000_000))
    order_type = request.args.get('order_type', 'taker').lower()
    direction  = request.args.get('direction',  'long').lower()

    if asset not in ASSETS:
        return jsonify({'error': f'Asset {asset} not found', 'available_assets': list(ASSETS.keys())}), 400

    result = comparator.compare_asset(asset, order_size, order_type=order_type, direction=direction)
    if not result:
        return jsonify({'error': 'Failed to compare asset'}), 500

    result = comparator.calculate_totals_and_winner(result, asset, order_type, direction)
    return jsonify(result)


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------
@socketio.on('compare')
def handle_compare(data):
    """Handle WebSocket compare request."""
    try:
        asset      = data.get('asset')
        order_size = data.get('order_size', 1_000_000)
        order_type = data.get('order_type', 'taker').lower()
        direction  = data.get('direction',  'long').lower()

        if not asset or asset not in ASSETS:
            emit('compare_error', {'error': f'Unknown asset: {asset}'})
            return

        result = comparator.compare_asset(asset, order_size, order_type=order_type, direction=direction)
        if not result:
            emit('compare_error', {'error': 'Failed to compare asset'})
            return

        result = comparator.calculate_totals_and_winner(result, asset, order_type, direction)
        emit('compare_result', result)
    except Exception as e:
        emit('compare_error', {'error': str(e)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    port  = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None   # debug only locally

    print("\n" + "=" * 60)
    print("🚀 FIXED FEE & AVERAGE SLIPPAGE COMPARISON API SERVER")
    print("=" * 60)
    print(f"Running on port {port} (debug={debug})")
    print("WebSocket support enabled")
    print("=" * 60 + "\n")

    socketio.run(app, host="0.0.0.0", debug=debug, port=port, use_reloader=False)
