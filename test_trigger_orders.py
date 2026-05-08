import os
import time
import config
from paradex_py import Paradex

if __name__ == '__main__':
    client = Paradex(
        env="prod",
        l1_address=config.PARADEX_L1_ADDRESS,
        l2_private_key=config.PARADEX_PRIVATE_KEY
    )
    
    # 1. Fetch Current Open Orders
    print("\n--- OPEN ORDERS ---")
    try:
        open_orders = client.api_client.fetch_orders()
        print([o for o in open_orders.get('results', [])])
    except Exception as e:
        print("Error:", e)

    # 2. Fetch Order History (last 10 minutes)
    print("\n--- ORDER HISTORY (Last 10m) ---")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (10 * 60 * 1000)
    
    try:
        history = client.api_client.fetch_orders_history({"start_at": start_ms, "end_at": end_ms})
        for o in history.get('results', []):
            print(f"ID: {o.get('id')} | Type: {o.get('type')} | Side: {o.get('side')} | Mkt: {o.get('market')} | Status: {o.get('status')} | Cancel Reason: {o.get('cancel_reason')}")
    except Exception as e:
        print("Error:", e)
