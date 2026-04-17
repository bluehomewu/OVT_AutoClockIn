# --- 假日與請假管理工具 (Holiday Manager) ---
import os
from datetime import datetime

# 以此腳本所在目錄為基準，確保無論從哪裡執行都能找到檔案
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLIDAY_FILE = os.path.join(BASE_DIR, 'holidays.txt')
EXCEPTION_FILE = os.path.join(BASE_DIR, 'exceptions.txt')


def _read_datefile(filepath):
    """通用：讀取並解析日期檔案，回傳 [(date_obj, 原始行), ...]。"""
    if not os.path.exists(filepath):
        return []
    entries = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            date_str = line.split('#')[0].strip()
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                entries.append((date_obj, line))
            except ValueError:
                if line.startswith('#'):
                    entries.append((None, line))
                else:
                    print(f"⚠️ 警告：發現無效的日期格式 '{date_str}'，將原樣保留該行。")
                    entries.append((None, line))
    entries.sort(key=lambda x: x[0] or datetime.max.date())
    return entries


def _write_datefile(filepath, entries):
    """通用：將日期列表寫回檔案。"""
    with open(filepath, 'w', encoding='utf-8') as f:
        for _, line in entries:
            f.write(line + '\n')


def _list_dates(filepath, title):
    print("\n" + "="*40)
    print(f"📅 {title}：")
    print("="*40)
    entries = _read_datefile(filepath)
    if not entries:
        print("沒有設定任何日期。")
    else:
        for _, line in entries:
            print(line)
    print("="*40)
    input("\n請按 Enter 鍵返回主選單...")


def _add_date(filepath, title):
    print(f"\n--- 新增{title} ---")
    date_str = input("請輸入日期 (格式 YYYY-MM-DD): ")
    try:
        new_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        print("❌ 錯誤：日期格式無效！請使用 'YYYY-MM-DD'。")
        return
    comment = input("請輸入註解 (可選，例如：補班日): ")
    entries = _read_datefile(filepath)
    for date_obj, _ in entries:
        if date_obj == new_date:
            print(f"❌ 錯誤：日期 {date_str} 已經存在於檔案中。")
            return
    new_line = date_str
    if comment:
        new_line += f" # {comment}"
    entries.append((new_date, new_line))
    _write_datefile(filepath, entries)
    print(f"✅ 成功新增日期：{new_line}")


def _remove_date(filepath, title):
    print(f"\n--- 移除{title} ---")
    entries = _read_datefile(filepath)
    valid = [(i, d, line) for i, (d, line) in enumerate(entries) if d is not None]
    if not valid:
        print("沒有可以移除的有效日期。")
        return
    for idx, (_, d, line) in enumerate(valid):
        print(f"  {idx + 1}. {line}")
    print("\n請輸入您想移除的日期編號 (輸入 0 取消):")
    try:
        choice = int(input("> "))
        if choice == 0:
            return
        if 1 <= choice <= len(valid):
            original_index = valid[choice - 1][0]
            removed_line = entries.pop(original_index)[1]
            _write_datefile(filepath, entries)
            print(f"✅ 成功移除：{removed_line}")
        else:
            print("❌ 錯誤：無效的編號。")
    except ValueError:
        print("❌ 錯誤：請輸入數字。")


# --- 舊版相容介面（保留原有函式名稱）---
def read_holidays():
    return _read_datefile(HOLIDAY_FILE)

def write_holidays(holidays):
    _write_datefile(HOLIDAY_FILE, holidays)

def list_dates():
    _list_dates(HOLIDAY_FILE, "目前已設定的假日/請假日期")

def add_date():
    _add_date(HOLIDAY_FILE, "假日/請假日")

def remove_date():
    _remove_date(HOLIDAY_FILE, "假日/請假日")


def main():
    """主程式迴圈，顯示選單"""
    while True:
        print("\n" + "="*44)
        print(" === OVT 打卡機器人 — 日期管理工具 ===")
        print("="*44)
        print("  【假日 / 請假日】（跳過自動打卡）")
        print("  1. 顯示所有假日/請假日")
        print("  2. 新增假日/請假日")
        print("  3. 移除假日/請假日")
        print()
        print("  【例外打卡日】（強制自動打卡，覆蓋週三/五/週末規則）")
        print("  4. 顯示所有例外打卡日")
        print("  5. 新增例外打卡日")
        print("  6. 移除例外打卡日")
        print()
        print("  0. 離開")
        print("="*44)

        choice = input("請輸入您的選擇 [0-6]: ")

        if choice == '1':
            _list_dates(HOLIDAY_FILE, "目前已設定的假日/請假日期")
        elif choice == '2':
            _add_date(HOLIDAY_FILE, "假日/請假日")
        elif choice == '3':
            _remove_date(HOLIDAY_FILE, "假日/請假日")
        elif choice == '4':
            _list_dates(EXCEPTION_FILE, "目前已設定的例外打卡日")
        elif choice == '5':
            _add_date(EXCEPTION_FILE, "例外打卡日")
            print("ℹ️  機器人將在最多 1 小時內自動偵測到此變更，無需重啟服務。")
        elif choice == '6':
            _remove_date(EXCEPTION_FILE, "例外打卡日")
        elif choice == '0':
            print("👋 程式結束。")
            break
        else:
            print("❌ 無效的選擇，請重新輸入。")


if __name__ == "__main__":
    main()
