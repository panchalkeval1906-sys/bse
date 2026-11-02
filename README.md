# BSE Announcements → Telegram Bot

Ye bot har 3 minute mein BSE India ke naye corporate announcements/filings check karta hai
aur naye milne par Telegram par bhej deta hai.

## ⚠️ Pehle ye karo — token revoke

Tumhara pichla bot token is conversation mein already share ho chuka hai, isliye wo ab
**exposed** maana jayega. Telegram mein `@BotFather` ko message karo:

```
/revoke
```

aur apna bot select karke naya token generate karo. Purana token kaam karna band ho jayega,
naya kaam karega. Yahi naya token neeche `.env` mein daalna.

## Setup (ek baar)

1. Python 3.10+ install hona chahiye.
2. Terminal mein isi folder ke andar:
   ```
   pip install bse python-dotenv requests
   ```
3. `.env.example` ko copy karke naam `.env` rakho, aur usme apna:
   - naya `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (jo tumhare paas already hai: 6071666296)

   daal do.

4. Chat ID confirm karne ke liye (agar doubt ho): apne bot ko Telegram par ek message bhejo,
   fir browser mein ye URL kholo (apna token daal ke):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Usme `"chat":{"id": ...}` dikhega — wahi tumhara CHAT_ID hai.

## Run

```
python bse_telegram_bot.py
```

Chalte hi bot Telegram par ek "started" confirmation message bhejega. Uske baad jab bhi
koi naya BSE filing aayega, wo turant Telegram par aa jayega. Terminal band mat karo —
jab tak ye process chal raha hai, tabhi tak checks hote rahenge.

Duplicate messages nahi aayenge — bot `bse_bot_data/seen_announcements.json` mein record
rakhta hai ki kaunsa announcement already bhej chuka hai. Agar script crash ho ke restart
ho, tab bhi purane wale dobara nahi bhejega.

Logs `bse_bot_data/bot.log` mein bhi save hote hain — agar kabhi doubt ho ki bot chal raha
tha ya nahi, ye file check karo.

## Continuous run — laptop band na karna pade uske liye options

Tumne bola "laptop open chhode toh" — ye zaroori hai kyunki script ek plain Python process
hai jo laptop ke saath hi chalti hai. Agar chaaho toh ye options hain jisse laptop band hone
par bhi bot chalta rahe:

1. **Windows Task Scheduler** — laptop restart hone par bhi auto-start ho jaye, isके liye
   ek scheduled task bana sakte ho jo boot par ye script run kare.
2. **Cloud VM (free tier)** — Oracle Cloud / AWS free tier / Railway.app jaisi jagah is
   script ko deploy kar do; wahan 24/7 chalega, laptop ki zaroorat hi nahi padegi. Agar
   chaho toh isme bhi help kar sakta hoon.
3. **Abhi ke liye**: laptop khula rakhna sabse simple hai — bas terminal band mat karna,
   aur laptop sleep mode mein na jaaye iska khayal rakhna (Windows Settings → Power → sleep
   "Never" jab plugged in ho).

## Common issues

- **"TELEGRAM_BOT_TOKEN missing"** → `.env` file sahi jagah nahi hai ya naam galat hai
  (`.env.example` nahi, sirf `.env` hona chahiye).
- **BSE se baar baar Timeout/ConnectionError** → BSE ka server temporarily slow/down ho
  sakta hai, ya tumhara internet drop hua ho. Bot khud retry karega, koi action nahi chahiye.
- **Telegram "Bad Request: can't parse entities"** → agar kabhi aisa aaye, batana — kisi
  company ke naam mein Markdown special character ho sakta hai jo escape karna reh gaya ho.
