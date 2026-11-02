#!/bin/bash
# enrich_loop.sh -- enrich ko khud restart karta rehta hai jab tak poora na ho jaye.
#  * Har 2 ghante mein enrich ko restart karta hai (memory khaali ho jati hai)
#  * Beech mein enrich mar jaye (memory/crash) ya Gemini quota lage, toh 10 min baad khud dobara chalata hai
#  * "ENRICH POORA ... agla step: run" aate hi ruk jata hai
cd /root/bse_bot || exit 1
while true; do
  echo "=== $(date) enrich shuru ===" >> bf_enrich_loop.log
  timeout 7200 python3 price_backfill.py enrich >> bf_enrich_loop.log 2>&1
  if tail -n 8 bf_enrich_loop.log | grep -q "Ab agla step: python3 price_backfill.py run"; then
    echo "=== $(date) SAB ENRICH POORA ===" >> bf_enrich_loop.log
    break
  fi
  echo "=== $(date) enrich ruka, 10 min baad dobara ===" >> bf_enrich_loop.log
  sleep 600
done
