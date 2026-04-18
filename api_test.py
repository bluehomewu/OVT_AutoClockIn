# --- OVT 自動打卡機器人 - v5.9 Linux 背景服務版 ---
import os
import time
import sys
import ssl
import json
import argparse
import configparser
import random
import logging
import logging.handlers
import platform
import threading
import requests
import subprocess
import urllib.request
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta

# --- 路徑設定：以此腳本所在目錄為基準 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_FILE = os.path.join(BASE_DIR, "bot_status.json")
HOLIDAY_FILE = os.path.join(BASE_DIR, "holidays.txt")
EXCEPTION_FILE = os.path.join(BASE_DIR, "exceptions.txt")
LOG_FILE = os.path.join(BASE_DIR, "autoclock_bot.log")
ACCOUNT_CONFIG = os.path.join(BASE_DIR, "account.config")

PRE_CHECK_MINUTES = 5
MAX_API_RETRIES = 3
LATE_ATTEMPT_GRACE_HOURS = 2
STARTUP_VPN_MAX_WAIT = 300
STARTUP_VPN_RETRY_INTERVAL = 30


def setup_logging():
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 輪替日誌：最大 5 MB，保留 3 份備份，避免無限增長
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)


# --- 設定區 & 讀取設定檔 ---
CLOCK_IN_TARGET_HOUR, CLOCK_IN_MIN_OFFSET, CLOCK_IN_MAX_OFFSET = 8, -15, 5
CLOCK_OUT_TARGET_HOUR, CLOCK_OUT_MIN_OFFSET, CLOCK_OUT_MAX_OFFSET = 17, 1, 10

try:
    config = configparser.ConfigParser()
    config.read(ACCOUNT_CONFIG)
    USERNAME = config.get("credentials", "username")
    PASSWORD = config.get("credentials", "password")
except Exception:
    logging.error("❌ 讀取 account.config 失敗！", exc_info=True)
    sys.exit(1)

# --- Telegram 通知設定（從 account.config 讀取，未設定時靜默停用）---
TELEGRAM_TOKEN: str = config.get("telegram", "token", fallback="")
TELEGRAM_CHAT_ID: str = config.get("telegram", "chat_id", fallback="")

# 每日帳號密碼驗證：記錄上次已檢查的日期，避免同日重複執行
_last_credential_check_date: str = ""


def send_telegram(message: str):
    """發送 Telegram 通知；若未設定 Token 或網路錯誤，靜默略過不影響打卡流程。"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx):
            pass
    except Exception:
        logging.warning("⚠️ Telegram 通知發送失敗（不影響打卡流程）")


def _telegram_reply(chat_id: str, message: str):
    """向指定 chat_id 回覆訊息（用於指令回應）。"""
    if not TELEGRAM_TOKEN:
        return
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx):
            pass
    except Exception:
        logging.warning("⚠️ Telegram 指令回覆發送失敗")


def _telegram_delete_message(chat_id: str, message_id: int):
    """刪除指定訊息（例如含明文密碼的指令訊息）。"""
    if not TELEGRAM_TOKEN:
        return
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "message_id": message_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx):
            pass
    except Exception:
        logging.warning("⚠️ Telegram 訊息刪除失敗（訊息可能已過期或無權限）")


def _ping_host(host: str) -> str:
    """Ping 一台主機，回傳單行結果字串（含延遲或失敗訊息）。"""
    param = ["-n", "1"] if platform.system().lower() == "windows" else ["-c", "1", "-W", "3"]
    try:
        result = subprocess.run(
            ["ping"] + param + [host],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # 從輸出中提取 avg 延遲
            for line in result.stdout.splitlines():
                if "time=" in line:
                    time_part = [p for p in line.split() if p.startswith("time=")]
                    if time_part:
                        return f"✅ {time_part[0]}"
                # Linux rtt 統計行
                if "rtt" in line or "round-trip" in line:
                    avg = line.split("/")[4] if "/" in line else "?"
                    return f"✅ avg {avg} ms"
            return "✅ 可達"
        else:
            return "❌ 無法連線"
    except subprocess.TimeoutExpired:
        return "❌ 逾時"
    except Exception as e:
        return f"❌ 錯誤: {e}"


def _handle_ping_command(chat_id: str):
    """處理 /ping 指令：回報機器人狀態與兩站連線結果。"""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # 讀取今日打卡狀態
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            status = json.load(f)
        today_str = status.get("date", "?")
        if status.get("skipped"):
            clock_status = f"💤 今日跳過（{status.get('reason', '')}）"
        else:
            ci = "✅" if status.get("clock_in_done") else "⏳"
            co = "✅" if status.get("clock_out_done") else "⏳"
            clock_status = f"上班打卡: {ci}  下班打卡: {co}"
    except Exception:
        today_str = date.today().isoformat()
        clock_status = "⚠️ 無法讀取狀態檔"

    # Ping 兩站
    prod_ip = "10.3.10.162"
    test_ip = "10.3.10.163"
    prod_result = _ping_host(prod_ip)
    test_result = _ping_host(test_ip)

    reply = (
        f"🤖 <b>OVT 打卡機器人 /ping</b>\n"
        f"⏰ {now_str}\n"
        f"📅 {today_str}\n"
        f"{'─' * 28}\n"
        f"<b>今日打卡狀態</b>\n{clock_status}\n"
        f"{'─' * 28}\n"
        f"<b>網路連線</b>\n"
        f"🏭 正式站 ({prod_ip})：{prod_result}\n"
        f"🧪 測試站 ({test_ip})：{test_result}"
    )
    _telegram_reply(chat_id, reply)


_HELP_TEXT = (
    "🤖 <b>OVT 打卡機器人 — 可用指令</b>\n"
    "\n"
    "/ping — 檢查機器人是否在線，並回報今日打卡狀態\n"
    "        及主機到正式站 / 測試站的 ping 延遲\n"
    "\n"
    "/list — 列出今日排程及實際打卡時間（正式站）\n"
    "\n"
    "/list_testsite — 列出測試站今日上下班打卡時間\n"
    "\n"
    "/clockin — 手動立即執行上班打卡，完成後自動顯示打卡記錄\n"
    "\n"
    "/clockout — 手動立即執行下班打卡，完成後自動顯示打卡記錄\n"
    "\n"
    "/clockin_test — 手動立即對測試站執行上班打卡，完成後自動顯示測試站打卡記錄\n"
    "\n"
    "/clockout_test — 手動立即對測試站執行下班打卡，完成後自動顯示測試站打卡記錄\n"
    "\n"
    "/setpassword &lt;新密碼&gt; — 更新 EIP 登入密碼（同步至設定檔與記憶體，並自動驗證）\n"
    "\n"
    "/help — 顯示此說明訊息"
)


def fetch_attendance_from_eip(base_url: str = None):
    """從 EIP 首頁取得今日打卡記錄（clockInTime / clockOutTime span）。
    回傳 (clock_in, clock_out)，未打卡時對應值為 None，失敗時回傳 (None, None)。
    時間格式為 HH:MM:SS，例如 '10:44:10' / '14:20:00'。
    base_url 預設使用全域 BASE_URL（正式站或已設定的站點）。
    """
    target = base_url or BASE_URL
    login_url = f"{target}/login/"
    try:
        s = requests.Session()
        login_page = s.get(login_url, verify=False, timeout=REQUEST_TIMEOUT)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")
        token_el = soup.find("input", {"name": "csrfmiddlewaretoken"})
        if not token_el:
            return None, None
        login_resp = s.post(
            login_url,
            data={"username": USERNAME, "password": PASSWORD,
                  "csrfmiddlewaretoken": token_el["value"]},
            headers={"Referer": login_url},
            verify=False, timeout=REQUEST_TIMEOUT,
        )
        if "login" in login_resp.url.lower():
            return None, None

        # 今日打卡時間直接顯示在首頁 #clockInTime / #clockOutTime
        home_resp = s.get(f"{target}/", verify=False, timeout=REQUEST_TIMEOUT)
        home_resp.raise_for_status()
        home_soup = BeautifulSoup(home_resp.text, "html.parser")

        def _parse_time(span_id):
            el = home_soup.find("span", {"id": span_id})
            if not el:
                return None
            txt = el.get_text(strip=True)
            return txt if txt else None

        clock_in  = _parse_time("clockInTime")
        clock_out = _parse_time("clockOutTime")
        return clock_in, clock_out

    except Exception:
        return None, None


def _handle_list_command(chat_id: str):
    """處理 /list 指令：從 EIP 取得今日實際打卡時間，並附上本機排程資料。"""
    weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    today = date.today()
    today_str_full = f"{today.isoformat()} {weekday_map[today.weekday()]}"

    # --- 讀本機狀態（排程時間 & debug 備份）---
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        _telegram_reply(chat_id, "⚠️ 無法讀取狀態檔，請確認機器人是否正常運行。")
        return

    if status.get("skipped"):
        reason = status.get("reason", "")
        # 今日跳過，但仍嘗試從 EIP 取得資料（可能是手動打卡或例外打卡日）
        eip_in, eip_out = fetch_attendance_from_eip()
        if eip_in or eip_out:
            _telegram_reply(
                chat_id,
                f"📋 <b>今日打卡記錄</b>\n"
                f"📅 {today_str_full}\n"
                f"{'─' * 28}\n"
                f"💤 自動打卡跳過（{reason}）\n"
                f"{'─' * 28}\n"
                f"<b>EIP 站點記錄</b>\n"
                f"  上班打卡：{eip_in or '—'}\n"
                f"  下班打卡：{eip_out or '—'}"
            )
        else:
            _telegram_reply(
                chat_id,
                f"📋 <b>今日打卡記錄</b>\n"
                f"📅 {today_str_full}\n"
                f"{'─' * 28}\n"
                f"💤 今日跳過（{reason}）\n"
                f"無自動打卡排程，EIP 亦無記錄。"
            )
        return

    def fmt_sched(key):
        val = status.get(key, "")
        if not val:
            return "—"
        return val[11:19] if len(val) > 8 else val

    sched_in  = fmt_sched("scheduled_in")
    sched_out = fmt_sched("scheduled_out")
    local_in  = status.get("clock_in_time") or "—"
    local_out = status.get("clock_out_time") or "—"

    # --- 從 EIP 取得最新資料 ---
    eip_in, eip_out = fetch_attendance_from_eip()
    eip_in_str  = eip_in  or "—"
    eip_out_str = eip_out or "—"

    ci_icon = "✅" if (eip_in or status.get("clock_in_done"))  else "⏳"
    co_icon = "✅" if (eip_out or status.get("clock_out_done")) else "⏳"

    reply = (
        f"📋 <b>今日打卡記錄</b>\n"
        f"📅 {today_str_full}\n"
        f"{'─' * 28}\n"
        f"<b>上班打卡</b> {ci_icon}\n"
        f"  排程時間：{sched_in}\n"
        f"  EIP 記錄：{eip_in_str}\n"
        f"  本機備份：{local_in}\n"
        f"{'─' * 28}\n"
        f"<b>下班打卡</b> {co_icon}\n"
        f"  排程時間：{sched_out}\n"
        f"  EIP 記錄：{eip_out_str}\n"
        f"  本機備份：{local_out}"
    )
    _telegram_reply(chat_id, reply)


def _handle_list_testsite_command(chat_id: str):
    """處理 /list_testsite 指令：從測試站 EIP 取得今日打卡時間，非工作日顯示跳過狀態。"""
    weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    today = date.today()
    today_str_full = f"{today.isoformat()} {weekday_map[today.weekday()]}"

    is_work, reason = is_workday(today)
    if not is_work:
        # 非工作日仍嘗試查詢 EIP（可能有手動打卡）
        eip_in, eip_out = fetch_attendance_from_eip(base_url=TEST_BASE_URL)
        if eip_in or eip_out:
            _telegram_reply(
                chat_id,
                f"📋 <b>今日打卡記錄（測試站）</b>\n"
                f"📅 {today_str_full}\n"
                f"{'─' * 28}\n"
                f"💤 自動打卡跳過（{reason}）\n"
                f"{'─' * 28}\n"
                f"<b>EIP 站點記錄</b>\n"
                f"  上班打卡：{eip_in or '—'}\n"
                f"  下班打卡：{eip_out or '—'}"
            )
        else:
            _telegram_reply(
                chat_id,
                f"📋 <b>今日打卡記錄（測試站）</b>\n"
                f"📅 {today_str_full}\n"
                f"{'─' * 28}\n"
                f"💤 今日跳過（{reason}）\n"
                f"無自動打卡排程，EIP 亦無記錄。"
            )
        return

    eip_in, eip_out = fetch_attendance_from_eip(base_url=TEST_BASE_URL)
    ci_icon = "✅" if eip_in  else "⏳"
    co_icon = "✅" if eip_out else "⏳"
    reply = (
        f"📋 <b>今日打卡記錄（測試站）</b>\n"
        f"📅 {today_str_full}\n"
        f"{'─' * 28}\n"
        f"🟢 上班打卡 {ci_icon}：{eip_in  or '—'}\n"
        f"🔴 下班打卡 {co_icon}：{eip_out or '—'}"
    )
    _telegram_reply(chat_id, reply)


def _handle_clockin_command(chat_id: str):
    """處理 /clockin 指令：立即執行上班打卡，完成後自動顯示今日打卡記錄。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _telegram_reply(chat_id, f"⏳ 正在執行手動上班打卡...\n⏰ {now_str}")
    success = perform_clock_action_api("in")
    if success:
        _telegram_reply(chat_id, f"✅ <b>手動上班打卡成功</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        _telegram_reply(chat_id, f"❌ <b>手動上班打卡失敗</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _handle_list_command(chat_id)


def _handle_clockout_command(chat_id: str):
    """處理 /clockout 指令：立即執行下班打卡，完成後自動顯示今日打卡記錄。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _telegram_reply(chat_id, f"⏳ 正在執行手動下班打卡...\n⏰ {now_str}")
    success = perform_clock_action_api("out")
    if success:
        _telegram_reply(chat_id, f"✅ <b>手動下班打卡成功</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        _telegram_reply(chat_id, f"❌ <b>手動下班打卡失敗</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _handle_list_command(chat_id)


def _handle_clockin_test_command(chat_id: str):
    """處理 /clockin_test 指令：立即對測試站執行上班打卡，完成後顯示測試站打卡記錄。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _telegram_reply(chat_id, f"⏳ 正在執行手動上班打卡（🧪 測試站）...\n⏰ {now_str}")
    success = perform_clock_action_api("in", base_url=TEST_BASE_URL)
    if success:
        _telegram_reply(chat_id, f"✅ <b>手動上班打卡成功（🧪 測試站）</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        _telegram_reply(chat_id, f"❌ <b>手動上班打卡失敗（🧪 測試站）</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _handle_list_testsite_command(chat_id)


def _handle_clockout_test_command(chat_id: str):
    """處理 /clockout_test 指令：立即對測試站執行下班打卡，完成後顯示測試站打卡記錄。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _telegram_reply(chat_id, f"⏳ 正在執行手動下班打卡（🧪 測試站）...\n⏰ {now_str}")
    success = perform_clock_action_api("out", base_url=TEST_BASE_URL)
    if success:
        _telegram_reply(chat_id, f"✅ <b>手動下班打卡成功（🧪 測試站）</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        _telegram_reply(chat_id, f"❌ <b>手動下班打卡失敗（🧪 測試站）</b>\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _handle_list_testsite_command(chat_id)


def verify_login_credentials(base_url: str = None) -> bool:
    """嘗試登入以驗證帳號密碼是否有效，不執行任何打卡動作。回傳 True/False。"""
    target = base_url or PROD_BASE_URL
    login_url = f"{target}/login/"
    try:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning
        )
        s = requests.Session()
        login_page = s.get(login_url, verify=False, timeout=REQUEST_TIMEOUT)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")
        token_el = soup.find("input", {"name": "csrfmiddlewaretoken"})
        if not token_el:
            return False
        resp = s.post(
            login_url,
            data={"username": USERNAME, "password": PASSWORD,
                  "csrfmiddlewaretoken": token_el["value"]},
            headers={"Referer": login_url},
            verify=False, timeout=REQUEST_TIMEOUT,
        )
        return "login" not in resp.url.lower() and "Please login" not in resp.text
    except Exception:
        return False


def _daily_credential_check():
    """每日登入驗證（於跨日或啟動時觸發）：失敗時透過 Telegram 發出密碼過期警告。
    同一天內只執行一次，重複呼叫安全無副作用。
    """
    global _last_credential_check_date
    today = date.today().isoformat()
    if _last_credential_check_date == today:
        return
    _last_credential_check_date = today
    logging.info("🔑 執行每日帳號密碼驗證...")
    if verify_login_credentials():
        logging.info("✅ 每日帳號密碼驗證成功。")
    else:
        logging.warning("⚠️ 每日帳號密碼驗證失敗！密碼可能已過期，請更新。")
        send_telegram(
            f"🔑 <b>帳號密碼驗證失敗</b>\n"
            f"嘗試登入 EIP 失敗，密碼可能已過期或已被更改。\n"
            f"請使用 /setpassword &lt;新密碼&gt; 指令更新密碼。\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )


def _handle_setpassword_command(chat_id: str, new_password: str, message_id: int = 0):
    """處理 /setpassword 指令：刪除含明文密碼的原始訊息，更新密碼後回報驗證結果。"""
    global PASSWORD

    # 立即刪除含明文密碼的使用者訊息，避免密碼留存於聊天記錄
    if message_id:
        _telegram_delete_message(chat_id, message_id)

    # 安全性：僅允許授權的 chat_id 操作
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        _telegram_reply(chat_id, "❌ 無授權操作。")
        return

    if not new_password:
        _telegram_reply(chat_id, "❌ 請提供新密碼，格式：/setpassword &lt;新密碼&gt;")
        return

    try:
        cfg = configparser.ConfigParser()
        cfg.read(ACCOUNT_CONFIG)
        cfg.set("credentials", "password", new_password)
        with open(ACCOUNT_CONFIG, "w", encoding="utf-8") as f:
            cfg.write(f)
        PASSWORD = new_password
        logging.info("🔑 密碼已透過 Telegram 指令更新（密碼不記錄於日誌）。")
    except Exception as e:
        logging.error(f"❌ 更新密碼失敗: {e}")
        _telegram_reply(chat_id, f"❌ 密碼更新失敗：{e}")
        return

    if verify_login_credentials():
        _telegram_reply(chat_id, "✅ <b>新密碼驗證成功！</b>可正常登入 EIP。")
    else:
        _telegram_reply(chat_id, "⚠️ <b>警告：新密碼驗證失敗</b>\n請確認密碼是否輸入正確。")


def telegram_polling_loop():
    """背景執行緒：long-poll Telegram getUpdates，處理指令。
    不影響打卡主流程，發生任何錯誤均自動重試。
    """
    if not TELEGRAM_TOKEN:
        return

    ctx = ssl.create_default_context()
    offset = 0
    logging.info("🤖 Telegram 指令監聽執行緒已啟動（支援 /ping、/help）")

    while True:
        try:
            params = json.dumps({
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message"],
            }).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                data=params,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
                data = json.loads(resp.read())

            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if not chat_id or not text:
                    continue

                if text.startswith("/ping"):
                    logging.info(f"📨 收到 /ping 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_ping_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/help"):
                    logging.info(f"📨 收到 /help 指令（chat_id={chat_id}）")
                    _telegram_reply(chat_id, _HELP_TEXT)

                elif text.startswith("/clockin_test"):
                    logging.info(f"📨 收到 /clockin_test 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_clockin_test_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/clockout_test"):
                    logging.info(f"📨 收到 /clockout_test 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_clockout_test_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/clockin"):
                    logging.info(f"📨 收到 /clockin 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_clockin_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/clockout"):
                    logging.info(f"📨 收到 /clockout 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_clockout_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/setpassword"):
                    parts = text.split(maxsplit=1)
                    new_pw = parts[1].strip() if len(parts) > 1 else ""
                    msg_id = msg.get("message_id", 0)
                    logging.info(f"📨 收到 /setpassword 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_setpassword_command,
                        args=(chat_id, new_pw, msg_id),
                        daemon=True,
                    ).start()

                elif text.startswith("/list_testsite"):
                    logging.info(f"📨 收到 /list_testsite 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_list_testsite_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

                elif text.startswith("/list"):
                    logging.info(f"📨 收到 /list 指令（chat_id={chat_id}）")
                    threading.Thread(
                        target=_handle_list_command,
                        args=(chat_id,),
                        daemon=True,
                    ).start()

        except Exception:
            time.sleep(10)

# --- API Interaction ---
PROD_BASE_URL = "https://tw-eip.ovt.com"
TEST_BASE_URL = "https://tw-eip-preprd.ovt.com"

# 這些 URL 由 __main__ 根據 --test 參數設定後才可使用；
# 函式內部透過全域查找取得，Python 在呼叫時解析 global，
# 因此 __main__ 的賦值會正確反映到所有後續呼叫。
BASE_URL: str = ""
LOGIN_URL: str = ""
CLOCK_IN_URL: str = ""
CLOCK_OUT_URL: str = ""

REQUEST_TIMEOUT = 15  # 所有 HTTP 請求的逾時秒數


def perform_clock_action_api(action_type: str, base_url: str = None):
    """執行打卡 API。base_url 預設使用全域 BASE_URL（正式站），
    傳入 TEST_BASE_URL 可對測試站打卡。"""
    target = base_url or BASE_URL
    target_login_url    = f"{target}/login/"
    target_clockin_url  = f"{target}/attendance/clockin-add/"
    target_clockout_url = f"{target}/attendance/clockout-add/"

    logging.info(f"🚀 === 準備執行 API 打卡任務: {action_type.upper()} ({target}) ===")
    session = requests.Session()

    try:
        logging.info("正在獲取登入頁面以取得 CSRF token (已停用 SSL 驗證)...")
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning
        )

        login_page = session.get(target_login_url, verify=False, timeout=REQUEST_TIMEOUT)
        login_page.raise_for_status()

        soup = BeautifulSoup(login_page.text, "html.parser")
        token_element = soup.find("input", {"name": "csrfmiddlewaretoken"})

        if not token_element:
            logging.error(
                "❌ 無法在登入頁面的 HTML 中找到 'csrfmiddlewaretoken'。網站結構可能已變更。"
            )
            failure_html = os.path.join(BASE_DIR, "login_page_no_token.html")
            with open(failure_html, "w", encoding="utf-8") as f:
                f.write(login_page.text)
            logging.info(f"已將 HTML 內容儲存至 '{failure_html}' 供分析。")
            return False

        csrf_token_from_html = token_element["value"]
        logging.info("✔️ 成功從 HTML 中解析出 CSRF token。")

        login_payload = {
            "username": USERNAME,
            "password": PASSWORD,
            "csrfmiddlewaretoken": csrf_token_from_html,
        }
        headers = {"Referer": target_login_url}

        logging.info("正在執行登入...")
        login_response = session.post(
            target_login_url, data=login_payload, headers=headers, verify=False,
            timeout=REQUEST_TIMEOUT,
        )
        login_response.raise_for_status()

        if (
            "login" in login_response.url.lower()
            or "Please login with your OVT account" in login_response.text
        ):
            logging.error(
                f"❌ 登入失敗。狀態碼: {login_response.status_code}。請檢查帳號密碼或網站變更。"
            )
            failure_html = os.path.join(BASE_DIR, "login_failure_response.html")
            with open(failure_html, "w", encoding="utf-8") as f:
                f.write(login_response.text)
            logging.info(f"✔️ 已將失敗的回應儲存至 '{failure_html}'。")
            return False

        logging.info("✔️ 登入成功！")

        csrf_token_for_actions = session.cookies.get("csrftoken")
        if not csrf_token_for_actions:
            logging.error("❌ 登入後無法找到操作所需的 'csrftoken' cookie。")
            return False

        action_headers = {
            "Referer": target + "/",
            "X-CSRFToken": csrf_token_for_actions,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36",
        }

        if action_type == "in":
            logging.info("正在發送 Clock In API 請求...")
            action_response = session.post(
                target_clockin_url, headers=action_headers, json={}, verify=False,
                timeout=REQUEST_TIMEOUT,
            )
        else:  # 'out'
            logging.info("正在發送 Clock Out API 請求...")
            clock_out_payload = {
                "reason": "Personal matters",
                "is_overnine": False,
                "csrfmiddlewaretoken": csrf_token_for_actions,
            }
            action_response = session.post(
                target_clockout_url,
                headers=action_headers,
                data=clock_out_payload,
                verify=False,
                timeout=REQUEST_TIMEOUT,
            )

        logging.info(f"伺服器回應狀態碼: {action_response.status_code}")

        if action_response.status_code == 200:
            response_json = action_response.json()
            logging.info(f"✔️ 伺服器回應: {response_json.get('message', 'No message')}")
            logging.info("🎉 任務圓滿完成。")
            return True
        else:
            logging.error(f"❌ 打卡 API 請求失敗。")
            try:
                logging.error(f"錯誤詳情: {action_response.json()}")
            except json.JSONDecodeError:
                logging.error(f"錯誤詳情 (非 JSON): {action_response.text[:200]}")
            return False
    except requests.exceptions.RequestException:
        logging.error(f"❌ 網路請求過程中發生錯誤。", exc_info=True)
        return False
    finally:
        logging.info("✅ === 打卡任務結束 ===")


def launch_vpn_monitor():
    """
    Linux 版本：此程式作為 systemd 背景服務執行，無法開啟互動式視窗。
    偵測到 VPN 斷線時僅記錄警告，由管理員手動重連。
    """
    logging.warning(
        "⚠️  VPN 連線失敗！請手動確認 VPN 狀態後重連。"
        " (Linux 服務模式不支援自動 GUI 重連)"
    )


def check_intranet_connection():
    hostname = "10.3.10.2"
    param = "-n 1" if platform.system().lower() == "windows" else "-c 1"
    redirect = "> nul" if platform.system().lower() == "windows" else "> /dev/null 2>&1"
    return os.system(f"ping {param} {hostname} {redirect}") == 0


def get_leave_dates():
    if not os.path.exists(HOLIDAY_FILE):
        return set()
    leave_dates = set()
    with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            date_str = line.split("#")[0].strip()
            try:
                leave_dates.add(datetime.strptime(date_str, "%Y-%m-%d").date())
            except ValueError:
                logging.warning(
                    f"⚠️ 在 {HOLIDAY_FILE} 中發現無效的日期格式: '{date_str}'，將忽略此行。"
                )
    return leave_dates


def get_exception_dates():
    """讀取 exceptions.txt，回傳需強制自動打卡的例外日期集合。"""
    if not os.path.exists(EXCEPTION_FILE):
        return set()
    exception_dates = set()
    with open(EXCEPTION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            date_str = line.split("#")[0].strip()
            try:
                exception_dates.add(datetime.strptime(date_str, "%Y-%m-%d").date())
            except ValueError:
                logging.warning(
                    f"⚠️ 在 {EXCEPTION_FILE} 中發現無效的日期格式: '{date_str}'，將忽略此行。"
                )
    return exception_dates


def is_workday(check_date):
    # 例外打卡日優先：即使是週三/五/週末，仍強制自動打卡
    if check_date in get_exception_dates():
        logging.info(f"⚡ 今天（{check_date}）是例外打卡日，將執行自動打卡。")
        return True, "Exception Day"
    if check_date.weekday() in (2, 4):
        logging.info(f"💤 今天是手動打卡日 (星期三/星期五)，將跳過自動化。")
        return False, "Manual Day (Wednesday/Friday)"
    if check_date.weekday() >= 5:
        logging.info(f"💤 今天是週末，將跳過自動化。")
        return False, "Weekend"
    if check_date in get_leave_dates():
        logging.info(f"🌴 今天是自訂的假日/請假日，將跳過自動化。")
        return False, "Holiday/Leave"
    return True, "Workday"


def save_status(status_data):
    status_to_save = {
        k: v.isoformat() if isinstance(v, (datetime, date)) else v
        for k, v in status_data.items()
    }
    # 原子寫入：先寫臨時檔再 rename，避免寫到一半崩潰導致狀態損壞
    temp_file = STATUS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(status_to_save, f, indent=4)
    os.replace(temp_file, STATUS_FILE)
    if not status_to_save.get("skipped"):
        logging.info(
            f"💾 狀態已儲存: Clock In Done: {status_to_save.get('clock_in_done', False)}, Clock Out Done: {status_to_save.get('clock_out_done', False)}"
        )


def load_status():
    today, today_str = date.today(), date.today().isoformat()
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                status = json.load(f)
            required_keys = ["clock_in_window_start", "clock_out_window_start"]
            if status.get("date") == today_str:
                if all(key in status for key in required_keys):
                    logging.info("🔄 同日重啟，正在載入先前的狀態...")
                    if not status.get("skipped"):
                        for key in status:
                            if "scheduled" in key or "window" in key:
                                status[key] = datetime.fromisoformat(status[key])
                    return status
                else:
                    logging.warning(
                        "⚠️ 舊版本的狀態檔案被偵測到。將為今天強制重設排程。"
                    )
    except (json.JSONDecodeError, KeyError):
        logging.warning("⚠️ 狀態檔案毀損或格式錯誤，將建立新的狀態。")
    today_is_workday, reason = is_workday(today)
    if not today_is_workday:
        status_to_return = {"date": today_str, "skipped": True, "reason": reason}
    else:
        logging.info(f"🏢 今天是工作日，正在建立新的隨機打卡排程...")
        sched_in, sched_out, in_win_start, in_win_end, out_win_start, out_win_end = (
            generate_random_schedule_for_today()
        )
        status_to_return = {
            "date": today_str,
            "skipped": False,
            "clock_in_done": False,
            "clock_out_done": False,
            "pre_check_in_done": False,
            "pre_check_out_done": False,
            "clock_in_retries": 0,
            "clock_out_retries": 0,
            "scheduled_in": sched_in,
            "scheduled_out": sched_out,
            "clock_in_window_start": in_win_start,
            "clock_in_window_end": in_win_end,
            "clock_out_window_start": out_win_start,
            "clock_out_window_end": out_win_end,
        }
    save_status(status_to_return)
    return status_to_return


def generate_random_schedule_for_today():
    today = date.today()
    in_win_start = datetime.combine(today, datetime.min.time()).replace(
        hour=CLOCK_IN_TARGET_HOUR
    ) + timedelta(minutes=CLOCK_IN_MIN_OFFSET)
    in_win_end = datetime.combine(today, datetime.min.time()).replace(
        hour=CLOCK_IN_TARGET_HOUR
    ) + timedelta(minutes=CLOCK_IN_MAX_OFFSET)
    out_win_start = datetime.combine(today, datetime.min.time()).replace(
        hour=CLOCK_OUT_TARGET_HOUR
    ) + timedelta(minutes=CLOCK_OUT_MIN_OFFSET)
    out_win_end = datetime.combine(today, datetime.min.time()).replace(
        hour=CLOCK_OUT_TARGET_HOUR
    ) + timedelta(minutes=CLOCK_OUT_MAX_OFFSET)
    in_ts = random.randint(int(in_win_start.timestamp()), int(in_win_end.timestamp()))
    scheduled_in = datetime.fromtimestamp(in_ts)
    if scheduled_in.second == 0:
        scheduled_in += timedelta(seconds=random.randint(1, 59))
    out_ts = random.randint(
        int(out_win_start.timestamp()), int(out_win_end.timestamp())
    )
    scheduled_out = datetime.fromtimestamp(out_ts)
    if scheduled_out.second == 0:
        scheduled_out += timedelta(seconds=random.randint(1, 59))
    return (
        scheduled_in,
        scheduled_out,
        in_win_start,
        in_win_end,
        out_win_start,
        out_win_end,
    )


def calculate_sleep_duration(status):
    now = datetime.now()
    candidates = []

    if not status.get("skipped"):
        if not status.get("clock_in_done"):
            if not status.get("pre_check_in_done"):
                candidates.append(
                    status["scheduled_in"] - timedelta(minutes=PRE_CHECK_MINUTES)
                )
            candidates.append(status["scheduled_in"])
            candidates.append(status["clock_in_window_end"])
            if status.get("clock_in_retries", 0) > 0:
                candidates.append(now + timedelta(seconds=60))

        if not status.get("clock_out_done"):
            if not status.get("pre_check_out_done"):
                candidates.append(
                    status["scheduled_out"] - timedelta(minutes=PRE_CHECK_MINUTES)
                )
            candidates.append(status["scheduled_out"])
            candidates.append(status["clock_out_window_end"])
            if status.get("clock_out_retries", 0) > 0:
                candidates.append(now + timedelta(seconds=60))

    future_events = [t for t in candidates if t > now]

    if not future_events:
        # 所有事件已完成或今天跳過，直接睡到明天午夜，不每小時無謂喚醒
        tomorrow_midnight = datetime.combine(
            date.today() + timedelta(days=1), datetime.min.time()
        ) + timedelta(seconds=5)
        secs = (tomorrow_midnight - now).total_seconds()
        return max(60, secs)

    next_event = min(future_events)
    secs_until = (next_event - now).total_seconds()
    return max(1, secs_until - 1)


def _handle_clock_action(status: dict, action_type: str, label: str):
    """上班/下班打卡的通用觸發邏輯（含補打卡與重試）。

    action_type: "in" 或 "out"
    label:       "上班" 或 "下班"（用於日誌訊息）
    """
    now = datetime.now()
    done_key = f"clock_{action_type}_done"
    retries_key = f"clock_{action_type}_retries"
    window_start = status[f"clock_{action_type}_window_start"]
    window_end = status[f"clock_{action_type}_window_end"]
    scheduled = status[f"scheduled_{action_type}"]

    if status.get(done_key):
        return

    if now > window_end:
        hours_late = (now - window_end).total_seconds() / 3600
        if hours_late > LATE_ATTEMPT_GRACE_HOURS:
            logging.error(
                f"❌ {label}打卡窗口已關閉超過 {LATE_ATTEMPT_GRACE_HOURS} 小時，"
                "不再嘗試補打卡。請手動補登！"
            )
            send_telegram(
                f"❌ <b>{label}打卡放棄</b>\n"
                f"窗口已關閉超過 {LATE_ATTEMPT_GRACE_HOURS} 小時，請手動補登！\n"
                f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            status[done_key] = True
            save_status(status)
            return

        retries = status.get(retries_key, 0)
        if retries >= MAX_API_RETRIES:
            status[done_key] = True
            logging.error("❌ 偵測到先前重試耗盡但狀態未儲存，標記為已處理。請手動補登！")
            save_status(status)
            return

        mins_late = hours_late * 60
        logging.warning(
            f"⏰ {label}打卡窗口已關閉 {mins_late:.0f} 分鐘，嘗試補打卡 "
            f"(第 {retries + 1}/{MAX_API_RETRIES} 次)..."
        )
        success = perform_clock_action_api(action_type)
        if success:
            status[done_key] = True
            status[f"clock_{action_type}_time"] = now.strftime("%H:%M:%S")
            logging.warning("⚠️ 補打卡成功，但打卡時間已延遲，請留意考勤記錄。")
            send_telegram(
                f"⚠️ <b>{label}補打卡成功</b>（延遲 {mins_late:.0f} 分鐘）\n"
                f"打卡時間已延遲，請留意考勤記錄。\n"
                f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            status[retries_key] = retries + 1
            remaining = MAX_API_RETRIES - status[retries_key]
            if remaining > 0:
                logging.warning(f"⚠️ 補打卡失敗，將稍後重試 (剩餘次數: {remaining})")
            else:
                status[done_key] = True
                logging.error("❌ 補打卡已達最大重試次數，放棄。請手動補登！")
                send_telegram(
                    f"❌ <b>{label}補打卡失敗</b>\n"
                    f"已達最大重試次數（{MAX_API_RETRIES} 次），請手動補登！\n"
                    f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
        save_status(status)

    elif window_start <= now >= scheduled:
        retries = status.get(retries_key, 0)
        if retries >= MAX_API_RETRIES:
            status[done_key] = True
            save_status(status)
            return

        logging.info(f"⏰ 到達{label}打卡時間，準備執行 (第 {retries + 1}/{MAX_API_RETRIES} 次)...")
        success = perform_clock_action_api(action_type)
        if success:
            status[done_key] = True
            status[f"clock_{action_type}_time"] = datetime.now().strftime("%H:%M:%S")
            logging.info(f"✔️ {label}打卡成功。")
            send_telegram(
                f"✅ <b>{label}打卡成功</b>\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            status[retries_key] = retries + 1
            remaining = MAX_API_RETRIES - status[retries_key]
            if remaining > 0:
                logging.warning(f"⚠️ {label}打卡失敗，將在下次循環重試 (剩餘次數: {remaining})")
            else:
                status[done_key] = True
                logging.error(f"❌ {label}打卡已達最大重試次數，放棄。請手動處理！")
                send_telegram(
                    f"❌ <b>{label}打卡失敗</b>\n"
                    f"已達最大重試次數（{MAX_API_RETRIES} 次），請手動處理！\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
        save_status(status)


def main_loop():
    status = load_status()
    if not status.get("skipped"):
        logging.info(
            f"🕒 上班卡有效時間窗口: {status['clock_in_window_start'].strftime('%H:%M:%S')} - {status['clock_in_window_end'].strftime('%H:%M:%S')}"
        )
        logging.info(
            f"   -> 今日隨機執行時間: {status['scheduled_in'].strftime('%H:%M:%S')}"
        )
        logging.info(
            f"🕒 下班卡有效時間窗口: {status['clock_out_window_start'].strftime('%H:%M:%S')} - {status['clock_out_window_end'].strftime('%H:%M:%S')}"
        )
        logging.info(
            f"   -> 今日隨機執行時間: {status['scheduled_out'].strftime('%H:%M:%S')}"
        )
    logging.info("=" * 50)

    while True:
        if date.today().isoformat() != status["date"]:
            logging.info("=" * 50)
            today = date.today()
            weekday_map = [
                "星期一", "星期二", "星期三", "星期四",
                "星期五", "星期六", "星期日",
            ]
            weekday_str = weekday_map[today.weekday()]
            logging.info(
                f"🌅 偵測到跨日，進入新的一天: {today.isoformat()} ({weekday_str})"
            )
            status = load_status()
            if not status.get("skipped"):
                logging.info(
                    f"🕒 新上班卡有效時間窗口: {status['clock_in_window_start'].strftime('%H:%M:%S')} - {status['clock_in_window_end'].strftime('%H:%M:%S')}"
                )
                logging.info(
                    f"   -> 新隨機執行時間: {status['scheduled_in'].strftime('%H:%M:%S')}"
                )
                logging.info(
                    f"🕒 新下班卡有效時間窗口: {status['clock_out_window_start'].strftime('%H:%M:%S')} - {status['clock_out_window_end'].strftime('%H:%M:%S')}"
                )
                logging.info(
                    f"   -> 新隨機執行時間: {status['scheduled_out'].strftime('%H:%M:%S')}"
                )
            logging.info("=" * 50)
            threading.Thread(target=_daily_credential_check, daemon=True).start()

        if status.get("skipped"):
            # 每小時醒來時重新檢查 exceptions.txt，無需重啟服務即可生效
            if date.today() in get_exception_dates():
                logging.info("⚡ 偵測到今天已加入例外打卡日，重新載入排程...")
                status = load_status()
                continue
            time.sleep(3600)
            continue

        now = datetime.now()

        # --- VPN 連線預檢查（上班 / 下班各一次）---
        for action_type, label in (("in", "上班"), ("out", "下班")):
            done_key = f"clock_{action_type}_done"
            pre_key = f"pre_check_{action_type}_done"
            sched_key = f"scheduled_{action_type}"
            if not status.get(done_key) and not status.get(pre_key):
                pre_check_time = status[sched_key] - timedelta(minutes=PRE_CHECK_MINUTES)
                if now >= pre_check_time:
                    logging.info(
                        f"ℹ️  到達{label}打卡前 {PRE_CHECK_MINUTES} 分鐘，執行 VPN 連線預檢查..."
                    )
                    if not check_intranet_connection():
                        launch_vpn_monitor()
                    else:
                        logging.info("✔️  VPN 連線預檢查成功。")
                    status[pre_key] = True
                    save_status(status)

        # --- 打卡觸發（上班 / 下班，含補打卡與重試）---
        for action_type, label in (("in", "上班"), ("out", "下班")):
            _handle_clock_action(status, action_type, label)

        sleep_secs = calculate_sleep_duration(status)
        if sleep_secs > 120:
            logging.info(f"⏳ 所有近期事件已完成，下次喚醒在 {sleep_secs:.0f} 秒後。")
        time.sleep(sleep_secs)


def wait_for_vpn_at_startup():
    """
    啟動時若 VPN 未連線，在 Linux 服務模式下等待並輪詢，
    不嘗試開啟任何 GUI 視窗。
    """
    logging.error("❌ 內網連線失敗！無法 ping 到 10.3.10.2。")
    logging.warning(
        "🔄 請手動確認 VPN 連線狀態。機器人將等待最多 "
        f"{STARTUP_VPN_MAX_WAIT // 60} 分鐘..."
    )
    send_telegram(
        f"⚠️ <b>VPN 連線失敗</b>\n"
        f"打卡機器人偵測到內網無法連通（10.3.10.2）。\n"
        f"正在等待，最多 {STARTUP_VPN_MAX_WAIT // 60} 分鐘。\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    launch_vpn_monitor()

    elapsed = 0
    while elapsed < STARTUP_VPN_MAX_WAIT:
        time.sleep(STARTUP_VPN_RETRY_INTERVAL)
        elapsed += STARTUP_VPN_RETRY_INTERVAL
        remaining = STARTUP_VPN_MAX_WAIT - elapsed
        logging.info(
            f"⏳ 等待 VPN 連線中... "
            f"(已等待 {elapsed}s / 最多 {STARTUP_VPN_MAX_WAIT}s，剩餘 {remaining}s)"
        )
        if check_intranet_connection():
            logging.info("✔️ VPN 連線已恢復！繼續啟動機器人...")
            send_telegram("✅ <b>VPN 連線已恢復</b>，打卡機器人繼續啟動。")
            return True

    logging.error(
        f"❌ 已等待 {STARTUP_VPN_MAX_WAIT // 60} 分鐘，VPN 仍未連線。程式將退出。"
    )
    logging.warning("🤔 請確認 VPN 狀態後手動重啟機器人。")
    send_telegram(
        f"❌ <b>VPN 等待逾時</b>\n"
        f"等待 {STARTUP_VPN_MAX_WAIT // 60} 分鐘仍無法連線，打卡機器人即將退出。\n"
        f"請手動確認 VPN 並重啟服務。"
    )
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OVT 自動打卡機器人")
    parser.add_argument(
        "--test",
        action="store_true",
        help="使用測試環境 (tw-eip-preprd.ovt.com)",
    )
    parser.add_argument(
        "--now",
        choices=["in", "out"],
        metavar="in|out",
        help="立即執行單次打卡並退出（用於手動測試），搭配 --test 可對測試站打卡",
    )
    args = parser.parse_args()

    # 根據參數切換環境 URL
    if args.test:
        BASE_URL = TEST_BASE_URL
    else:
        BASE_URL = PROD_BASE_URL
    LOGIN_URL = f"{BASE_URL}/login/"
    CLOCK_IN_URL = f"{BASE_URL}/attendance/clockin-add/"
    CLOCK_OUT_URL = f"{BASE_URL}/attendance/clockout-add/"

    setup_logging()

    env_label = "🧪 【測試站】" if args.test else "🏭 【正式站】"
    logging.info("=" * 50)
    logging.info(f"{env_label} 目標伺服器: {BASE_URL}")
    logging.info("🔌 正在檢查內網連線狀態...")

    if not check_intranet_connection():
        if not wait_for_vpn_at_startup():
            sys.exit(1)
    else:
        logging.info("✔️  內網連線成功！")

    today = date.today()
    weekday_map = [
        "星期一", "星期二", "星期三", "星期四",
        "星期五", "星期六", "星期日",
    ]
    weekday_str = weekday_map[today.weekday()]
    logging.info(f"☀️  今天是 {today.isoformat()} ({weekday_str})")
    logging.info("=" * 50)
    logging.info("🕰️  自動打卡機器人已啟動 (API 模式)，正在載入狀態...")

    # --now 模式：立即打卡後退出（測試用）
    if args.now:
        label = "上班" if args.now == "in" else "下班"
        logging.info(f"⚡ 手動觸發模式：立即執行 {label} 打卡")
        success = perform_clock_action_api(args.now)
        if success:
            send_telegram(
                f"✅ <b>手動{label}打卡成功</b> {env_label}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            send_telegram(
                f"❌ <b>手動{label}打卡失敗</b> {env_label}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        sys.exit(0 if success else 1)

    # 排程模式：啟動 Telegram 指令監聽執行緒 & 發送啟動通知
    if TELEGRAM_TOKEN:
        t = threading.Thread(target=telegram_polling_loop, daemon=True, name="tg-polling")
        t.start()

    # 啟動時執行一次帳號密碼驗證
    threading.Thread(target=_daily_credential_check, daemon=True).start()

    send_telegram(
        f"🤖 <b>OVT 打卡機器人已啟動</b>\n"
        f"📅 {today.isoformat()} ({weekday_str})\n"
        f"🌐 {env_label} {BASE_URL}"
    )

    main_loop()
