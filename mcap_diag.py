# mcap_diag.py -- READ-ONLY. Kuch save/badalta nahi. Sirf dekhta hai ki BSE in 20 scrips ka market cap kya deta hai.
# Server pe /root/bse_bot mein rakho, phir:  python3 mcap_diag.py
import json
import time
import price_backfill as pb
import price_tracker as pt

SCRIPS = ['514348', '530929', '531260', '532854', '533163', '537840', '542771', '543410', '543622', '543941',
          '544164', '544370', '544495', '544505', '544561', '544565', '544686', '544741', '544797', '544862']

cache = pb._load_mcap()
print("mcap_now.json mein in scrips ki value:")
for s in SCRIPS:
    print("  ", s, "->", repr(cache.get(s, "FILE MEIN NAHI")))

print("\nBSE listSecurities se live jawab:")
with pt._open_client() as client:
    first = True
    for s in SCRIPS:
        try:
            data = client.listSecurities(scripcode=s, group="")
        except Exception as e:  # noqa: BLE001
            print(s, "ERROR:", type(e).__name__, str(e)[:80])
            continue
        entry = data[0] if isinstance(data, list) and data else data
        if not isinstance(entry, dict):
            print(s, "-> khaali/alag jawab:", repr(entry)[:80])
            continue
        mk = {k: entry[k] for k in entry if 'cap' in k.lower() or 'mkt' in k.lower()}
        grp = entry.get('GROUP') or entry.get('Group') or entry.get('group')
        sec = entry.get('Status') or entry.get('STATUS')
        print(s, "| naam:", str(entry.get('Scrip_Name') or entry.get('scrip_name') or entry.get('Issuer_Name') or '')[:28],
              "| group:", grp, "| status:", sec, "| mcap fields:", mk)
        if first:
            print("   (pehle scrip ke saare fields):", json.dumps(entry)[:600])
            first = False
        time.sleep(1)
