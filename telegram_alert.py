"""
GBT Morning Telegram Alert
ดึงข้อมูล IV + DTE + SD Levels จาก pageth/Vol2VolData
ตรวจจับราคาเปิด CME จาก commit ที่ volume น้อย (session เพิ่งเปิด)
"""

import os, math, re, requests
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT   = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")   # auto-provided by GitHub Actions
DATA_REPO       = "pageth/Vol2VolData"
INTRADAY_FILE   = "IntradayData.txt"
OI_FILE         = "OIData.txt"
BKK             = timezone(timedelta(hours=7))

# ── ถ้า total Call+Put ต่ำกว่านี้ = data ใหม่ของวัน (session เพิ่งเปิด) ──
FRESH_THRESHOLD = 1000

# ── GitHub API helpers ──────────────────────────────────
def gh_headers():
    h = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h

def fetch_commits_since(since_dt):
    """ดึง commit list ของ IntradayData.txt ตั้งแต่ since_dt (newest first)"""
    url = f"https://api.github.com/repos/{DATA_REPO}/commits"
    params = {
        "path"     : INTRADAY_FILE,
        "since"    : since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_page" : 100,
    }
    r = requests.get(url, params=params, headers=gh_headers(), timeout=15)
    r.raise_for_status()
    return r.json()   # list, newest first

def fetch_raw_at_sha(sha, filename):
    url = f"https://raw.githubusercontent.com/{DATA_REPO}/{sha}/{filename}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text

def fetch_raw_latest(filename):
    url = f"https://raw.githubusercontent.com/{DATA_REPO}/main/{filename}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.text

# ── Session start = 23:00 UTC (= 06:00 BKK ของวันถัดไป) ──
def session_start_utc():
    now = datetime.now(timezone.utc)
    # ถ้าตอนนี้ก่อน 23:00 UTC → session เริ่มเมื่อคืน (เมื่อวาน 23:00 UTC)
    if now.hour < 23:
        base = now - timedelta(days=1)
    else:
        base = now
    return base.replace(hour=23, minute=0, second=0, microsecond=0)

# ── หาราคาเปิดของ session จาก commit ที่ volume น้อยที่สุด ──────
def find_session_open():
    """
    สแกน commit history ตั้งแต่ CME เปิด (23:00 UTC)
    หา commit ที่ Call+Put รวมน้อย = ข้อมูลสดของวัน (ตลาดพึ่งเปิด)
    คืนค่า (open_price, how_found, commit_time_bkk)
    """
    since = session_start_utc()
    print(f"🔍 Scanning commits since {since.strftime('%H:%M UTC')}…")

    try:
        commits = fetch_commits_since(since)
    except Exception as e:
        print(f"⚠ Cannot fetch commits: {e}")
        return None, "fallback (API error)", "—"

    if not commits:
        print("⚠ No commits found for this session")
        return None, "no commits", "—"

    print(f"  Found {len(commits)} commits")

    # commits เรียงล่าสุดก่อน → reverse เพื่อให้เก่าสุดก่อน
    commits_asc = list(reversed(commits))

    best_price = None
    best_time  = None
    method     = "oldest commit"

    for commit in commits_asc:
        sha  = commit["sha"]
        ts   = commit["commit"]["committer"]["date"]   # ISO 8601 UTC
        ct   = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        ct_bkk = ct.astimezone(BKK).strftime("%H:%M BKK")

        try:
            raw   = fetch_raw_at_sha(sha, INTRADAY_FILE)
            h     = parse_header(raw)
            total = h["total_call"] + h["total_put"]
            print(f"  [{ct_bkk}] sha={sha[:7]}  price=${h['fut_price']}  total={total:,}")

            if total < FRESH_THRESHOLD:
                # ✅ volume น้อย = data สดของวัน → นี่คือ open จริง
                best_price = h["fut_price"]
                best_time  = ct_bkk
                method     = f"fresh session (total={total:,})"
                # ไม่ break → หา commit เก่าสุดที่ยัง fresh อยู่
            else:
                # volume เพิ่มขึ้นแล้ว → commit ก่อนหน้าคือ open จริง
                if best_price is not None:
                    print(f"  ↑ volume ramped up here → using previous commit as open")
                    break
                else:
                    # ไม่เคยเจอ fresh commit เลย → ใช้ oldest commit นี้แทน
                    best_price = h["fut_price"]
                    best_time  = ct_bkk
                    method     = f"oldest available (total={total:,})"
                    break

        except Exception as e:
            print(f"  Skip {sha[:7]}: {e}")
            continue

    if best_price is None:
        # Fallback: อ่านจาก main branch แล้วคำนวณ open จาก futChg
        print("⚠ Using fallback: futPrice − futChg")
        raw = fetch_raw_latest(INTRADAY_FILE)
        h   = parse_header(raw)
        best_price = h["open_price"]
        best_time  = "—"
        method     = "fallback (futPrice−futChg)"

    print(f"✅ Session open = ${best_price:.1f}  [{method}]")
    return best_price, method, best_time

# ── Parse header lines ──────────────────────────────────
def parse_header(raw):
    lines = raw.strip().splitlines()
    h1, h2 = lines[0], lines[1]

    dte_m   = re.search(r'\(([\d.]+)\s*DTE\)', h1)
    dte     = float(dte_m.group(1)) if dte_m else 1.0

    price_m   = re.search(r'vs\s*([\d.]+)\s*\(([-+]?[\d.]+)\)', h1)
    fut_price = float(price_m.group(1)) if price_m else None
    fut_chg   = float(price_m.group(2)) if price_m else 0.0
    open_price = round(fut_price - fut_chg, 2) if fut_price else None

    iv_m    = re.search(r'Vol:\s*([\d.]+)', h2)
    atm_iv  = float(iv_m.group(1)) if iv_m else None

    ivchg_m = re.search(r'Vol Chg:\s*([-+]?[\d.]+)', h2)
    iv_chg  = float(ivchg_m.group(1)) if ivchg_m else 0.0

    call_m = re.search(r'Call:\s*([\d,]+)', h2)
    put_m  = re.search(r'Put:\s*([\d,]+)',  h2)
    total_call = int(call_m.group(1).replace(',','')) if call_m else 0
    total_put  = int(put_m.group(1).replace(',',''))  if put_m  else 0

    return dict(
        dte=dte, fut_price=fut_price, fut_chg=fut_chg,
        open_price=open_price, atm_iv=atm_iv, iv_chg=iv_chg,
        total_call=total_call, total_put=total_put
    )

# ── คำนวณ SD levels ─────────────────────────────────────
def calc_sd(open_price, atm_iv, dte):
    sig    = atm_iv / 100.0
    sd_exp = open_price * sig * math.sqrt(dte / 365.0)
    sd_1d  = open_price * sig * math.sqrt(1  / 365.0)
    return sd_exp, sd_1d

def fmt_chg(v):
    return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"

def fmt_iv_chg(v):
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"

def iv_regime(iv):
    if iv < 10: return "🟢 ต่ำมาก"
    if iv < 15: return "🟢 ต่ำ"
    if iv < 22: return "🟡 ปกติ"
    if iv < 30: return "🟠 สูง"
    return              "🔴 สูงมาก"

# ── สร้าง message ────────────────────────────────────────
def build_message(h_intraday, h_oi, open_price, open_method, open_time):
    now_bkk  = datetime.now(BKK)
    date_str = now_bkk.strftime("%d/%m/%Y  %H:%M BKK")
    hour     = now_bkk.hour
    if hour < 9:   session = "🌅 เปิดตลาด"
    elif hour < 12: session = "☀️ กลางเช้า"
    else:           session = "🌤 รอบบ่าย"

    iv  = h_intraday["atm_iv"]
    dte = h_intraday["dte"]
    F   = h_intraday["fut_price"]
    chg = h_intraday["fut_chg"]
    iv_c = h_intraday["iv_chg"]
    O   = open_price

    sd_exp, sd_1d = calc_sd(O, iv, dte)

    up1, dn1 = O + sd_exp,   O - sd_exp
    up2, dn2 = O + 2*sd_exp, O - 2*sd_exp

    oi_call   = h_oi["total_call"]
    oi_put    = h_oi["total_put"]
    put_call  = oi_put / oi_call if oi_call else 0
    pct_used  = abs(F - O) / sd_exp * 100 if sd_exp > 0 else 0
    dir_arrow = "↑" if F > O else "↓"

    regime    = iv_regime(iv)
    iv_arrow  = "📈" if iv_c > 0 else "📉" if iv_c < 0 else "➡️"

    # Open source label
    if "fresh" in open_method:
        open_src = f"✅ จาก session open ({open_time})"
    elif "oldest" in open_method:
        open_src = f"⚠️ CME ยังไม่อัพ → ใช้ oldest commit ({open_time})"
    else:
        open_src = f"⚠️ {open_method}"

    msg = f"""📡 *GBT Alert — {session}*
{date_str}
━━━━━━━━━━━━━━━━━━━━━

📊 *Gold Futures*
  Price : `${F:,.1f}`  ({fmt_chg(chg)})
  Open  : `${O:,.1f}`  {open_src}
  วิ่งแล้ว : `{dir_arrow} {pct_used:.1f}%` of expected move

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
  1σ/1Day : `±${sd_1d:.1f}` (reference)

📋 *Open Interest*
  Call OI : `{oi_call:,}`
  Put  OI : `{oi_put:,}`
  P/C     : `{put_call:.2f}` {'🐻 Put Heavy' if put_call>1.2 else '🐂 Call Heavy' if put_call<0.8 else '⚖️ Balanced'}

━━━━━━━━━━━━━━━━━━━━━
_GBT Auto-Alert · pageth/Vol2VolData_"""

    return msg

# ── ส่ง Telegram ─────────────────────────────────────────
def send_telegram(text):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    print(f"✅ Sent! status={r.status_code}")

# ── Main ─────────────────────────────────────────────────
def main():
    print("🔄 Fetching current Vol2Vol data…")
    raw_intraday = fetch_raw_latest(INTRADAY_FILE)
    raw_oi       = fetch_raw_latest(OI_FILE)
    h_intraday   = parse_header(raw_intraday)
    h_oi         = parse_header(raw_oi)
    print(f"  IV={h_intraday['atm_iv']}%  DTE={h_intraday['dte']}  F=${h_intraday['fut_price']}")

    # หาราคาเปิดจาก commit history (smart detection)
    open_price, open_method, open_time = find_session_open()
    if open_price is None:
        open_price = h_intraday["open_price"]
        open_method = "fallback"
        open_time = "—"

    msg = build_message(h_intraday, h_oi, open_price, open_method, open_time)
    print("📤 Sending to Telegram…")
    send_telegram(msg)

if __name__ == "__main__":
    main()
