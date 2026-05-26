"""
GBT Morning Telegram Alert
ดึงข้อมูล IV + DTE + SD Levels จาก pageth/Vol2VolData
แล้วส่งไปยัง Telegram ทุกเช้า 06:05 BKK
"""

import os, math, re, requests
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT   = os.environ["TELEGRAM_CHAT_ID"]
DATA_REPO       = "pageth/Vol2VolData"
INTRADAY_FILE   = "IntradayData.txt"
OI_FILE         = "OIData.txt"
BKK             = timezone(timedelta(hours=7))

# ── Fetch latest file from GitHub (no token needed — public repo) ──
def fetch_raw(filename):
    url = f"https://raw.githubusercontent.com/{DATA_REPO}/main/{filename}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text

# ── Parse header lines ─────────────────────────────────
def parse_header(raw):
    lines = raw.strip().splitlines()
    h1, h2 = lines[0], lines[1]

    # DTE — format: (0.46 DTE)
    dte_m = re.search(r'\(([\d.]+)\s*DTE\)', h1)
    dte   = float(dte_m.group(1)) if dte_m else 1.0

    # Futures price & change — format: vs 4540.5 (-21.4)
    price_m = re.search(r'vs\s*([\d.]+)\s*\(([-+]?[\d.]+)\)', h1)
    fut_price = float(price_m.group(1)) if price_m else None
    fut_chg   = float(price_m.group(2)) if price_m else 0.0

    # Open price = current − change
    open_price = round(fut_price - fut_chg, 2) if fut_price else None

    # ATM IV — format: Vol: 17.83
    iv_m  = re.search(r'Vol:\s*([\d.]+)', h2)
    atm_iv = float(iv_m.group(1)) if iv_m else None

    # IV Change — format: Vol Chg: -1.24
    ivchg_m = re.search(r'Vol Chg:\s*([-+]?[\d.]+)', h2)
    iv_chg  = float(ivchg_m.group(1)) if ivchg_m else 0.0

    # ATM strike
    atm_m = re.search(r'ATM:\s*([\d.]+)', h2)
    atm_k = float(atm_m.group(1)) if atm_m else None

    # Total Call / Put (OI or volume)
    call_m = re.search(r'Call:\s*([\d,]+)', h2)
    put_m  = re.search(r'Put:\s*([\d,]+)',  h2)
    total_call = int(call_m.group(1).replace(',','')) if call_m else 0
    total_put  = int(put_m.group(1).replace(',',''))  if put_m  else 0

    return dict(
        dte=dte, fut_price=fut_price, fut_chg=fut_chg,
        open_price=open_price, atm_iv=atm_iv, iv_chg=iv_chg,
        atm_k=atm_k, total_call=total_call, total_put=total_put
    )

# ── Calculate SD levels ─────────────────────────────────
def calc_sd(open_price, atm_iv, dte):
    sig   = atm_iv / 100.0
    sd1d  = open_price * sig * math.sqrt(dte / 365.0)   # expected move for this DTE
    sd1day = open_price * sig * math.sqrt(1 / 365.0)    # 1-day sigma (reference)
    return sd1d, sd1day

# ── Format number with +/- sign ─────────────────────────
def fmt_chg(v):
    return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"

def fmt_iv_chg(v):
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"

# ── IV Regime label ──────────────────────────────────────
def iv_regime(iv):
    if iv < 10:  return "🟢 ต่ำมาก"
    if iv < 15:  return "🟢 ต่ำ"
    if iv < 22:  return "🟡 ปกติ"
    if iv < 30:  return "🟠 สูง"
    return           "🔴 สูงมาก"

# ── Build message ────────────────────────────────────────
def build_message(h_intraday, h_oi):
    now_bkk = datetime.now(BKK)
    date_str = now_bkk.strftime("%d/%m/%Y  %H:%M BKK")
    hour = now_bkk.hour
    if hour < 9:
        session = "🌅 เปิดตลาด"
    elif hour < 12:
        session = "☀️ กลางเช้า"
    else:
        session = "🌤 รอบบ่าย"

    iv   = h_intraday["atm_iv"]
    dte  = h_intraday["dte"]
    F    = h_intraday["fut_price"]
    O    = h_intraday["open_price"]
    chg  = h_intraday["fut_chg"]
    iv_c = h_intraday["iv_chg"]

    sd_exp, sd_1day = calc_sd(O, iv, dte)

    up1 = O + sd_exp
    dn1 = O - sd_exp
    up2 = O + 2*sd_exp
    dn2 = O - 2*sd_exp

    # OI totals from OI file
    oi_call = h_oi["total_call"]
    oi_put  = h_oi["total_put"]
    put_call = oi_put / oi_call if oi_call else 0

    pct_used = abs(F - O) / sd_exp * 100 if sd_exp > 0 else 0

    regime = iv_regime(iv)
    iv_arrow = "📈" if iv_c > 0 else "📉" if iv_c < 0 else "➡️"

    msg = f"""📡 *GBT Alert — {session}*
{date_str}
━━━━━━━━━━━━━━━━━━━━━

📊 *Gold Futures*
  Price : `${F:,.1f}`  ({fmt_chg(chg)})
  Open  : `${O:,.1f}`
  วิ่งแล้ว: `{pct_used:.1f}% of expected move`

🎯 *Implied Volatility*
  ATM IV : `{iv:.2f}%`  {iv_arrow} ({fmt_iv_chg(iv_c)})
  DTE    : `{dte:.3f}` days
  Regime : {regime}

📐 *SD Levels (จาก Open `${O:,.1f}`)*
```
+2σ  ${up2:>8,.1f}   (+${2*sd_exp:.1f})
+1σ  ${up1:>8,.1f}   (+${sd_exp:.1f})
 ▶︎   ${O:>8,.1f}   Open
-1σ  ${dn1:>8,.1f}   (-${sd_exp:.1f})
-2σ  ${dn2:>8,.1f}   (-${2*sd_exp:.1f})
```
  1σ/1Day : `±${sd_1day:.1f}` (reference)

📋 *Open Interest Summary*
  Call OI : `{oi_call:,}`
  Put  OI : `{oi_put:,}`
  P/C Ratio: `{put_call:.2f}` {'🐻 Put Heavy' if put_call>1.2 else '🐂 Call Heavy' if put_call<0.8 else '⚖️ Balanced'}

━━━━━━━━━━━━━━━━━━━━━
_GBT Auto-Alert · pageth/Vol2VolData_"""

    return msg

# ── Send to Telegram ────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id"    : TELEGRAM_CHAT,
        "text"       : text,
        "parse_mode" : "Markdown",
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    print(f"✅ Sent! status={r.status_code}")
    return r.json()

# ── Main ────────────────────────────────────────────────
def main():
    print("🔄 Fetching Vol2Vol data…")
    raw_intraday = fetch_raw(INTRADAY_FILE)
    raw_oi       = fetch_raw(OI_FILE)

    h_intraday = parse_header(raw_intraday)
    h_oi       = parse_header(raw_oi)

    print(f"  IV={h_intraday['atm_iv']}%  DTE={h_intraday['dte']}  F=${h_intraday['fut_price']}")

    msg = build_message(h_intraday, h_oi)
    print("📤 Sending to Telegram…")
    send_telegram(msg)

if __name__ == "__main__":
    main()
