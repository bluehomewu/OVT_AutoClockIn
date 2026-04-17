# --- 假日與請假管理工具 (Holiday Manager) ---
import os
from datetime import datetime

# 以此腳本所在目錄為基準，確保無論從哪裡執行都能找到檔案
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLIDAY_FILE = os.path.join(BASE_DIR, 'holidays.txt')

def read_holidays():
    """
    讀取並解析 holidays.txt 檔案。
    返回一個已排序的日期和原始行內容的列表。
    """
    if not os.path.exists(HOLIDAY_FILE):
        return []
    
    holidays = []
    with open(HOLIDAY_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # 分割日期和註解
            date_str = line.split('#')[0].strip()
            
            try:
                # 驗證日期格式
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                holidays.append((date_obj, line))
            except ValueError:
                # 如果是註解行或格式錯誤，也保留它
                if line.startswith('#'):
                    holidays.append((None, line))
                else:
                    print(f"⚠️ 警告：發現無效的日期格式 '{date_str}'，將原樣保留該行。")
                    holidays.append((None, line)) # 保留格式錯誤的行

    # 排序，將註解和錯誤行放在最後
    holidays.sort(key=lambda x: x[0] or datetime.max.date())
    return holidays

def write_holidays(holidays):
    """將假期列表寫回檔案，保持排序和註解"""
    with open(HOLIDAY_FILE, 'w', encoding='utf-8') as f:
        for _, line in holidays:
            f.write(line + '\n')

def list_dates():
    """顯示目前所有已設定的日期"""
    print("\n" + "="*40)
    print("📅 目前已設定的假日/請假日期：")
    print("="*40)
    
    holidays = read_holidays()
    
    if not holidays:
        print("沒有設定任何日期。")
    else:
        for _, line in holidays:
            print(line)
            
    print("="*40)
    input("\n請按 Enter 鍵返回主選單...")

def add_date():
    """新增一個新的日期和註解"""
    print("\n--- 新增日期 ---")
    date_str = input("請輸入日期 (格式 YYYY-MM-DD): ")
    
    try:
        new_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        print(f"❌ 錯誤：日期格式無效！請使用 'YYYY-MM-DD'。")
        return

    comment = input("請輸入註解 (可選，例如：國慶日): ")
    
    holidays = read_holidays()
    
    # 檢查日期是否已存在
    for date_obj, _ in holidays:
        if date_obj == new_date:
            print(f"❌ 錯誤：日期 {date_str} 已經存在於檔案中。")
            return
            
    # 建立新的一行
    new_line = date_str
    if comment:
        new_line += f" # {comment}"
        
    holidays.append((new_date, new_line))
    
    write_holidays(holidays)
    print(f"✅ 成功新增日期：{new_line}")

def remove_date():
    """從列表中移除一個日期"""
    print("\n--- 移除日期 ---")
    holidays = read_holidays()
    
    # 過濾出可以被移除的有效日期
    valid_dates = [(i, date_obj, line) for i, (date_obj, line) in enumerate(holidays) if date_obj is not None]
    
    if not valid_dates:
        print("沒有可以移除的有效日期。")
        return

    for idx, (_, date_obj, line) in enumerate(valid_dates):
        print(f"  {idx + 1}. {line}")
    
    print("\n請輸入您想移除的日期編號 (輸入 0 取消):")
    
    try:
        choice = int(input("> "))
        if choice == 0:
            return
        
        if 1 <= choice <= len(valid_dates):
            # 找到原始 holidays 列表中的索引
            original_index = valid_dates[choice - 1][0]
            removed_line = holidays.pop(original_index)[1]
            write_holidays(holidays)
            print(f"✅ 成功移除：{removed_line}")
        else:
            print("❌ 錯誤：無效的編號。")
            
    except ValueError:
        print("❌ 錯誤：請輸入數字。")

def main():
    """主程式迴圈，顯示選單"""
    while True:
        print("\n" + "="*40)
        print(" === 假日/請假管理工具 ===")
        print("="*40)
        print("  1. 顯示所有已設定日期")
        print("  2. 新增一個日期")
        print("  3. 移除一個日期")
        print("  4. 離開")
        print("="*40)
        
        choice = input("請輸入您的選擇 [1-4]: ")
        
        if choice == '1':
            list_dates()
        elif choice == '2':
            add_date()
        elif choice == '3':
            remove_date()
        elif choice == '4':
            print("👋 程式結束。")
            break
        else:
            print("❌ 無效的選擇，請重新輸入。")

if __name__ == "__main__":
    main()
