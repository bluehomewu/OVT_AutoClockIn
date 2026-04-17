# OVT 自動打卡機器人 — Linux 版

> **版本**：v5.5 Linux 背景服務版  
> **執行環境**：Linux + Python 3.8+ + systemd  
> **背景執行方式**：systemd service（不依賴 Docker、tmux 或 `&`）

---

## 目錄

1. [專案概述](#1-專案概述)
2. [檔案結構](#2-檔案結構)
3. [運作流程](#3-運作流程)
4. [API 節點說明](#4-api-節點說明)
5. [設定檔說明](#5-設定檔說明)
6. [部署方式](#6-部署方式)
7. [服務管理指令](#7-服務管理指令)
8. [日誌查看](#8-日誌查看)
9. [假日管理工具](#9-假日管理工具)
10. [常見問題](#10-常見問題)

---

## 1. 專案概述

此程式會在每個工作日自動於指定時間範圍內，透過 HTTP API 向 OVT 考勤系統完成上班打卡與下班打卡，模擬人工登入行為並加入隨機時間偏移，避免規律打卡被系統偵測。

**重要限制**（此版本 skip 的日期）：

| 條件 | 行為 |
|------|------|
| 星期三 / 星期五 | 跳過（需手動打卡） |
| 週六 / 週日 | 跳過 |
| `holidays.txt` 中的日期 | 跳過 |

---

## 2. 檔案結構

```
Linux/
├── api_test.py          # 主程式（背景打卡 Bot）
├── holiday_manager.py   # 假日/請假日期管理工具（互動式 CLI）
├── account.config       # 帳號密碼設定檔 ⚠️ 請勿上傳至版控
├── holidays.txt         # 自訂假日與請假日期清單
├── autoclock.service    # systemd 服務定義範本
├── install.sh           # 一鍵安裝腳本
├── uninstall.sh         # 移除腳本
├── bot_status.json      # 執行時期狀態檔（自動產生）
├── autoclock_bot.log    # 日誌檔（自動產生）
└── README.md            # 本文件
```

---

## 3. 運作流程

### 3.1 啟動流程

```
程式啟動
    │
    ├─ 讀取 account.config（帳號密碼）
    │
    ├─ 檢查內網連線（ping 10.3.10.2）
    │     ├─ 成功 → 繼續
    │     └─ 失敗 → 記錄警告，等待最多 5 分鐘後退出（需手動處理 VPN）
    │
    ├─ 讀取 / 建立 bot_status.json
    │     ├─ 同日重啟 → 載入上次狀態（不重複打卡）
    │     └─ 新的一天 → 呼叫 generate_random_schedule_for_today()
    │
    └─ 進入主迴圈 main_loop()
```

### 3.2 每日排程邏輯

```
今天是工作日？
    ├─ 否 → 每小時 sleep，等待跨日
    └─ 是 → 產生隨機打卡時間
              │
              ├─ 上班卡時間窗口：07:45 ~ 08:05（預設）
              │       隨機時間 = 窗口內任意秒數
              │
              └─ 下班卡時間窗口：17:01 ~ 17:10（預設）
                      隨機時間 = 窗口內任意秒數
```

時間設定參數（在 `api_test.py` 頂部）：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `CLOCK_IN_TARGET_HOUR` | `8` | 上班打卡基準小時 |
| `CLOCK_IN_MIN_OFFSET` | `-15` | 相對基準的最早偏移（分鐘） |
| `CLOCK_IN_MAX_OFFSET` | `5` | 相對基準的最晚偏移（分鐘） |
| `CLOCK_OUT_TARGET_HOUR` | `17` | 下班打卡基準小時 |
| `CLOCK_OUT_MIN_OFFSET` | `1` | 相對基準的最早偏移（分鐘） |
| `CLOCK_OUT_MAX_OFFSET` | `10` | 相對基準的最晚偏移（分鐘） |

### 3.3 打卡觸發邏輯

```
到達打卡時間前 5 分鐘
    └─ VPN 連線預檢查（ping 10.3.10.2）
          ├─ 成功 → 等待打卡時間
          └─ 失敗 → 記錄警告（請手動重連 VPN）

到達隨機打卡時間
    └─ 呼叫 perform_clock_action_api("in" 或 "out")
          ├─ 成功 → 標記完成，寫入 bot_status.json
          └─ 失敗 → 最多重試 3 次（每次間隔約 1 分鐘）
                    超過 2 小時寬限期後放棄，記錄錯誤日誌
```

### 3.4 補打卡機制

若程式因意外重啟或 VPN 問題導致打卡錯過窗口：

- 窗口結束後 **2 小時內**，仍會自動嘗試補打卡（最多 3 次）
- 超過 2 小時則放棄，記錄 `❌` 錯誤至日誌，需手動補登

---

## 4. API 節點說明

目標系統：`https://tw-eip.ovt.com`

### 4.1 取得登入頁面（GET）

```
GET https://tw-eip.ovt.com/login/
```

- 目的：取得登入表單中隱藏的 `csrfmiddlewaretoken`
- 解析方式：使用 BeautifulSoup 解析 HTML `<input name="csrfmiddlewaretoken">`

### 4.2 登入（POST）

```
POST https://tw-eip.ovt.com/login/
Content-Type: application/x-www-form-urlencoded
Referer: https://tw-eip.ovt.com/login/
```

**Request Body：**

```
username=<帳號>
password=<密碼>
csrfmiddlewaretoken=<從 HTML 取得的 token>
```

**成功判斷：** 回應 URL 不包含 `login`，且頁面不含 `Please login with your OVT account`  
**登入後：** 從 Session Cookie 中取得 `csrftoken`，用於後續打卡請求

### 4.3 上班打卡（POST）

```
POST https://tw-eip.ovt.com/attendance/clockin-add/
Content-Type: application/json
X-CSRFToken: <cookie 中的 csrftoken>
Referer: https://tw-eip.ovt.com/
```

**Request Body：**

```json
{}
```

**成功回應（HTTP 200）：**

```json
{ "message": "..." }
```

### 4.4 下班打卡（POST）

```
POST https://tw-eip.ovt.com/attendance/clockout-add/
Content-Type: application/x-www-form-urlencoded
X-CSRFToken: <cookie 中的 csrftoken>
Referer: https://tw-eip.ovt.com/
```

**Request Body：**

```
reason=Personal matters
is_overnine=False
csrfmiddlewaretoken=<cookie 中的 csrftoken>
```

> **注意**：所有 API 請求均停用 SSL 驗證（`verify=False`），因目標伺服器使用自簽憑證。

---

## 5. 設定檔說明

### `account.config`

```ini
[credentials]
username = your_username
password = your_password
```

> ⚠️ **安全提示**：此檔案包含明文密碼，請確保：
> - 不要提交至 Git（加入 `.gitignore`）
> - 設定適當的檔案權限：`chmod 600 account.config`

### `holidays.txt`

每行一個日期，格式為 `YYYY-MM-DD`，支援 `#` 行內註解：

```
2026-02-16 # 過年
2026-04-04 # 兒童節
2026-06-19 # 請假
```

空行與 `#` 開頭的純註解行會被忽略。

---

## 6. 部署方式

### 系統需求

- Linux（支援 systemd，如 Ubuntu 20.04+、Debian 11+、CentOS 8+）
- Python 3.8+
- pip3
- 網路可連通 VPN / 內網（`10.3.10.2` 可 ping）

### 步驟

#### Step 1：複製檔案至 Linux 主機

```bash
# 方式一：scp 複製
scp -r ./Linux/ user@your-linux-host:~/ovt-autoclock/

# 方式二：git clone 後進入 Linux 子目錄
```

#### Step 2：進入目錄並設定帳密

```bash
cd ~/ovt-autoclock
vim account.config
```

填入正確的帳號密碼後儲存：

```ini
[credentials]
username = your.username
password = your_password
```

#### Step 3：設定檔案權限

```bash
chmod 600 account.config
chmod +x install.sh uninstall.sh
```

#### Step 4：執行安裝腳本

```bash
sudo bash install.sh
```

安裝腳本會自動：
1. 確認 Python 3 與 pip3 已安裝
2. 安裝 `requests` 與 `beautifulsoup4` 套件
3. 將 `autoclock.service` 範本填入正確路徑與使用者名稱
4. 複製至 `/etc/systemd/system/autoclock.service`
5. 執行 `systemctl daemon-reload && systemctl enable && systemctl start`

#### Step 5：確認服務狀態

```bash
sudo systemctl status autoclock
```

看到 `Active: active (running)` 即表示安裝成功。

---

## 7. 服務管理指令

```bash
# 查看目前狀態
sudo systemctl status autoclock

# 啟動服務
sudo systemctl start autoclock

# 停止服務
sudo systemctl stop autoclock

# 重新啟動（例如修改設定後）
sudo systemctl restart autoclock

# 設定開機自動啟動
sudo systemctl enable autoclock

# 取消開機自動啟動
sudo systemctl disable autoclock

# 完全移除服務
sudo bash uninstall.sh
```

---

## 8. 日誌查看

程式同時輸出至 `autoclock_bot.log` 檔案與 systemd journal。

```bash
# 即時查看日誌檔（推薦）
tail -f ~/ovt-autoclock/autoclock_bot.log

# 透過 systemd journal 查看（含系統層級資訊）
journalctl -u autoclock -f

# 查看最近 100 行
journalctl -u autoclock -n 100 --no-pager

# 查看特定日期的日誌
journalctl -u autoclock --since "2026-04-17" --until "2026-04-18"
```

**日誌符號說明：**

| 符號 | 意義 |
|------|------|
| `✔️` | 操作成功 |
| `❌` | 操作失敗 |
| `⏰` | 到達打卡時間 |
| `⚠️` | 警告（補打卡、重試等） |
| `💾` | 狀態儲存 |
| `🌅` | 偵測到跨日 |
| `💤` | 今日跳過（週末/假日/手動打卡日） |
| `⏳` | 進入睡眠等待下一事件 |

---

## 9. 假日管理工具

使用 `holiday_manager.py` 管理 `holidays.txt` 中的假日/請假日期：

```bash
python3 holiday_manager.py
```

提供互動式選單：

```
========================================
 === 假日/請假管理工具 ===
========================================
  1. 顯示所有已設定日期
  2. 新增一個日期
  3. 移除一個日期
  4. 離開
========================================
```

修改後**不需要重啟服務**，程式在跨日時會自動重新讀取 `holidays.txt`。

---

## 10. 常見問題

### Q：服務啟動後馬上停止？

查看日誌確認原因：

```bash
journalctl -u autoclock -n 30 --no-pager
```

常見原因：
- `account.config` 格式錯誤或帳密錯誤
- VPN 未連線，且等待 5 分鐘後超時退出 → 服務會自動在 60 秒後重啟

### Q：如何確認今天打卡是否已完成？

```bash
cat ~/ovt-autoclock/bot_status.json
```

查看 `clock_in_done` 與 `clock_out_done` 是否為 `true`。

### Q：VPN 斷線怎麼辦？

Linux 版本不支援自動 GUI 重連，請手動重連 VPN。重連後服務會在下次輪詢時自動偵測到並繼續打卡。若打卡時間已過但在 2 小時寬限期內，會自動補打卡。

### Q：如何修改打卡時間範圍？

編輯 `api_test.py` 頂部的常數，然後重啟服務：

```bash
vim ~/ovt-autoclock/api_test.py
# 修改 CLOCK_IN_TARGET_HOUR 等變數
sudo systemctl restart autoclock
```

### Q：登入失敗怎麼辦？

程式會將失敗的 HTML 回應儲存至 `login_failure_response.html`，可用來分析網站是否有結構變更：

```bash
cat ~/ovt-autoclock/login_failure_response.html
```
