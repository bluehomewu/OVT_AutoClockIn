# --- OVT 自動打卡機器人 - v5.6 Linux 背景服務版 ---
import os
import time
import sys
import argparse
import configparser
import random
import logging
import json
import platform
import requests
import subprocess
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta

# --- 路徑設定：以此腳本所在目錄為基準 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_FILE = os.path.join(BASE_DIR, "bot_status.json")
HOLIDAY_FILE = os.path.join(BASE_DIR, "holidays.txt")
LOG_FILE = os.path.join(BASE_DIR, "autoclock_bot.log")
ACCOUNT_CONFIG = os.path.join(BASE_DIR, "account.config")

PRE_CHECK_MINUTES = 5
MAX_API_RETRIES = 3
LATE_ATTEMPT_GRACE_HOURS = 2
STARTUP_VPN_MAX_WAIT = 300
STARTUP_VPN_RETRY_INTERVAL = 30


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        filename=LOG_FILE,
        filemode="a",
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)


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

# --- API Interaction ---
PROD_BASE_URL = "https://tw-eip.ovt.com"
TEST_BASE_URL = "https://tw-eip-preprd.ovt.com"

# 預設使用正式站；若以 --test 參數啟動則改用測試站
BASE_URL = PROD_BASE_URL
LOGIN_URL = f"{BASE_URL}/login/"
CLOCK_IN_URL = f"{BASE_URL}/attendance/clockin-add/"
CLOCK_OUT_URL = f"{BASE_URL}/attendance/clockout-add/"


def perform_clock_action_api(action_type: str):
    logging.info(f"🚀 === 準備執行 API 打卡任務: {action_type.upper()} ===")
    session = requests.Session()

    try:
        logging.info("正在獲取登入頁面以取得 CSRF token (已停用 SSL 驗證)...")
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning
        )

        login_page = session.get(LOGIN_URL, verify=False)
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
        headers = {"Referer": LOGIN_URL}

        logging.info("正在執行登入...")
        login_response = session.post(
            LOGIN_URL, data=login_payload, headers=headers, verify=False
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
            "Referer": BASE_URL + "/",
            "X-CSRFToken": csrf_token_for_actions,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36",
        }

        if action_type == "in":
            logging.info("正在發送 Clock In API 請求...")
            action_response = session.post(
                CLOCK_IN_URL, headers=action_headers, json={}, verify=False
            )
        else:  # 'out'
            logging.info("正在發送 Clock Out API 請求...")
            clock_out_payload = {
                "reason": "Personal matters",
                "is_overnine": False,
                "csrfmiddlewaretoken": csrf_token_for_actions,
            }
            action_response = session.post(
                CLOCK_OUT_URL,
                headers=action_headers,
                data=clock_out_payload,
                verify=False,
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


def is_workday(check_date):
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
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_to_save, f, indent=4)
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
        tomorrow_midnight = datetime.combine(
            date.today() + timedelta(days=1), datetime.min.time()
        ) + timedelta(seconds=5)
        secs = (tomorrow_midnight - now).total_seconds()
        return max(60, min(3600, secs))

    next_event = min(future_events)
    secs_until = (next_event - now).total_seconds()
    return max(1, secs_until - 1)


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

        if status.get("skipped"):
            time.sleep(3600)
            continue

        now = datetime.now()

        # --- 上班前 VPN 連線預檢查 ---
        if (
            not status.get("skipped")
            and not status.get("clock_in_done")
            and not status.get("pre_check_in_done")
        ):
            pre_check_time = status["scheduled_in"] - timedelta(
                minutes=PRE_CHECK_MINUTES
            )
            if now >= pre_check_time:
                logging.info(
                    f"ℹ️  到達上班打卡前 {PRE_CHECK_MINUTES} 分鐘，執行 VPN 連線預檢查..."
                )
                if not check_intranet_connection():
                    launch_vpn_monitor()
                else:
                    logging.info("✔️  VPN 連線預檢查成功。")
                status["pre_check_in_done"] = True
                save_status(status)

        # --- 下班前 VPN 連線預檢查 ---
        if (
            not status.get("skipped")
            and not status.get("clock_out_done")
            and not status.get("pre_check_out_done")
        ):
            pre_check_time = status["scheduled_out"] - timedelta(
                minutes=PRE_CHECK_MINUTES
            )
            if now >= pre_check_time:
                logging.info(
                    f"ℹ️  到達下班打卡前 {PRE_CHECK_MINUTES} 分鐘，執行 VPN 連線預檢查..."
                )
                if not check_intranet_connection():
                    launch_vpn_monitor()
                else:
                    logging.info("✔️  VPN 連線預檢查成功。")
                status["pre_check_out_done"] = True
                save_status(status)

        # --- 上班打卡觸發（含補打卡與重試邏輯）---
        if not status["clock_in_done"]:
            if now > status["clock_in_window_end"]:
                hours_late = (now - status["clock_in_window_end"]).total_seconds() / 3600
                if hours_late <= LATE_ATTEMPT_GRACE_HOURS:
                    retries = status.get("clock_in_retries", 0)
                    if retries < MAX_API_RETRIES:
                        mins_late = hours_late * 60
                        logging.warning(
                            f"⏰ 上班打卡窗口已關閉 {mins_late:.0f} 分鐘，嘗試補打卡 "
                            f"(第 {retries + 1}/{MAX_API_RETRIES} 次)..."
                        )
                        success = perform_clock_action_api("in")
                        if success:
                            status["clock_in_done"] = True
                            logging.warning("⚠️ 補打卡成功，但打卡時間已延遲，請留意考勤記錄。")
                        else:
                            status["clock_in_retries"] = retries + 1
                            remaining = MAX_API_RETRIES - status["clock_in_retries"]
                            if remaining > 0:
                                logging.warning(
                                    f"⚠️ 補打卡失敗，將稍後重試 (剩餘次數: {remaining})"
                                )
                            else:
                                status["clock_in_done"] = True
                                logging.error(
                                    "❌ 補打卡已達最大重試次數，放棄。請手動補登！"
                                )
                        save_status(status)
                    else:
                        status["clock_in_done"] = True
                        logging.error(
                            "❌ 偵測到先前重試耗盡但狀態未儲存，標記為已處理。請手動補登！"
                        )
                        save_status(status)
                else:
                    logging.error(
                        f"❌ 上班打卡窗口已關閉超過 {LATE_ATTEMPT_GRACE_HOURS} 小時，"
                        "不再嘗試補打卡。請手動補登！"
                    )
                    status["clock_in_done"] = True
                    save_status(status)

            elif status["clock_in_window_start"] <= now and now >= status["scheduled_in"]:
                retries = status.get("clock_in_retries", 0)
                if retries < MAX_API_RETRIES:
                    logging.info(
                        f"⏰ 到達上班打卡時間，準備執行 (第 {retries + 1}/{MAX_API_RETRIES} 次)..."
                    )
                    success = perform_clock_action_api("in")
                    if success:
                        status["clock_in_done"] = True
                        logging.info("✔️ 上班打卡成功。")
                    else:
                        status["clock_in_retries"] = retries + 1
                        remaining = MAX_API_RETRIES - status["clock_in_retries"]
                        if remaining > 0:
                            logging.warning(
                                f"⚠️ 上班打卡失敗，將在下次循環重試 (剩餘次數: {remaining})"
                            )
                        else:
                            status["clock_in_done"] = True
                            logging.error(
                                "❌ 上班打卡已達最大重試次數，放棄。請手動處理！"
                            )
                    save_status(status)
                else:
                    status["clock_in_done"] = True
                    save_status(status)

        # --- 下班打卡觸發（含補打卡與重試邏輯）---
        if not status["clock_out_done"]:
            if now > status["clock_out_window_end"]:
                hours_late = (now - status["clock_out_window_end"]).total_seconds() / 3600
                if hours_late <= LATE_ATTEMPT_GRACE_HOURS:
                    retries = status.get("clock_out_retries", 0)
                    if retries < MAX_API_RETRIES:
                        mins_late = hours_late * 60
                        logging.warning(
                            f"⏰ 下班打卡窗口已關閉 {mins_late:.0f} 分鐘，嘗試補打卡 "
                            f"(第 {retries + 1}/{MAX_API_RETRIES} 次)..."
                        )
                        success = perform_clock_action_api("out")
                        if success:
                            status["clock_out_done"] = True
                            logging.warning("⚠️ 補打卡成功，但打卡時間已延遲，請留意考勤記錄。")
                        else:
                            status["clock_out_retries"] = retries + 1
                            remaining = MAX_API_RETRIES - status["clock_out_retries"]
                            if remaining > 0:
                                logging.warning(
                                    f"⚠️ 補打卡失敗，將稍後重試 (剩餘次數: {remaining})"
                                )
                            else:
                                status["clock_out_done"] = True
                                logging.error(
                                    "❌ 補打卡已達最大重試次數，放棄。請手動補登！"
                                )
                        save_status(status)
                    else:
                        status["clock_out_done"] = True
                        logging.error(
                            "❌ 偵測到先前重試耗盡但狀態未儲存，標記為已處理。請手動補登！"
                        )
                        save_status(status)
                else:
                    logging.error(
                        f"❌ 下班打卡窗口已關閉超過 {LATE_ATTEMPT_GRACE_HOURS} 小時，"
                        "不再嘗試補打卡。請手動補登！"
                    )
                    status["clock_out_done"] = True
                    save_status(status)

            elif (
                status["clock_out_window_start"] <= now
                and now >= status["scheduled_out"]
            ):
                retries = status.get("clock_out_retries", 0)
                if retries < MAX_API_RETRIES:
                    logging.info(
                        f"⏰ 到達下班打卡時間，準備執行 (第 {retries + 1}/{MAX_API_RETRIES} 次)..."
                    )
                    success = perform_clock_action_api("out")
                    if success:
                        status["clock_out_done"] = True
                        logging.info("✔️ 下班打卡成功。")
                    else:
                        status["clock_out_retries"] = retries + 1
                        remaining = MAX_API_RETRIES - status["clock_out_retries"]
                        if remaining > 0:
                            logging.warning(
                                f"⚠️ 下班打卡失敗，將在下次循環重試 (剩餘次數: {remaining})"
                            )
                        else:
                            status["clock_out_done"] = True
                            logging.error(
                                "❌ 下班打卡已達最大重試次數，放棄。請手動處理！"
                            )
                    save_status(status)
                else:
                    status["clock_out_done"] = True
                    save_status(status)

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
            return True

    logging.error(
        f"❌ 已等待 {STARTUP_VPN_MAX_WAIT // 60} 分鐘，VPN 仍未連線。程式將退出。"
    )
    logging.warning("🤔 請確認 VPN 狀態後手動重啟機器人。")
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
        logging.info(f"⚡ 手動觸發模式：立即執行 {'上班' if args.now == 'in' else '下班'} 打卡")
        success = perform_clock_action_api(args.now)
        sys.exit(0 if success else 1)

    main_loop()
