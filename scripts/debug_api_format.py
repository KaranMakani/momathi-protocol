from config.settings import PARADEX_L1_ADDRESS, PARADEX_PRIVATE_KEY
from paradex_py import Paradex

if __name__ == '__main__':
    client = Paradex(
        env="prod",
        l1_address=PARADEX_L1_ADDRESS,
        l2_private_key=PARADEX_PRIVATE_KEY
    )
    
    # 1. Check AccountSummary
    try:
        summary = client.api_client.fetch_account_summary()
        print("Summary type:", type(summary))
        print("Summary fields:", dir(summary))
        print("Summary:", summary)
    except Exception as e:
        print("Balance err:", e)

    # 2. Check klines
    import time
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (10 * 60 * 60 * 1000)
    try:
        klines = client.api_client.fetch_klines('BTC-USD-PERP', '60', start_ms, end_ms)
        print("Klines type:", type(klines))
        if isinstance(klines, dict) and 'results' in klines:
            print("First item in results:", klines['results'][0] if klines['results'] else 'empty')
        else:
            print("Klines format:", klines)
    except Exception as e:
        print("Klines err:", e)
