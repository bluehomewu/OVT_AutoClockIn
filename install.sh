#!/usr/bin/env bash
# install.sh — 安裝 OVT 自動打卡機器人為 systemd 背景服務
# 使用方式：chmod +x install.sh && sudo ./install.sh

set -e

SERVICE_NAME="autoclock"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="${SUDO_USER:-$(whoami)}"

echo "================================================"
echo " OVT 自動打卡機器人 — systemd 安裝程式"
echo "================================================"
echo " 安裝目錄 : ${SCRIPT_DIR}"
echo " 執行使用者: ${CURRENT_USER}"
echo "================================================"

# 確認 python3 存在
if ! command -v python3 &>/dev/null; then
    echo "❌ 找不到 python3，請先安裝 Python 3.8+"
    exit 1
fi

# 確認 account.config 已設定
if grep -q "YOUR_USERNAME" "${SCRIPT_DIR}/account.config" 2>/dev/null; then
    echo "❌ 請先編輯 ${SCRIPT_DIR}/account.config，填入真實的帳號密碼！"
    exit 1
fi

# 保護帳號密碼設定檔
chmod 600 "${SCRIPT_DIR}/account.config"
echo "✔️  已設定 account.config 權限為 600（僅擁有者可讀寫）"

# 安裝 Python 依賴
echo "📦 安裝 Python 套件..."
pip3 install --quiet requests beautifulsoup4 || {
    echo "⚠️  pip3 失敗，嘗試使用 pip3 --user 安裝..."
    pip3 install --user --quiet requests beautifulsoup4
}

# 從範本產生 service 檔
echo "⚙️  產生 systemd service 檔案..."
sed \
    -e "s|%USER_PLACEHOLDER%|${CURRENT_USER}|g" \
    -e "s|%WORKDIR_PLACEHOLDER%|${SCRIPT_DIR}|g" \
    "${SCRIPT_DIR}/autoclock.service" > "${SERVICE_FILE}"

echo "✔️  已寫入 ${SERVICE_FILE}"

# 重新載入 systemd 並啟用服務
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl start  "${SERVICE_NAME}"

echo ""
echo "✅ 安裝完成！服務已啟動並設定為開機自動執行。"
echo ""
echo "常用管理指令："
echo "  sudo systemctl status  ${SERVICE_NAME}   # 查看狀態"
echo "  sudo systemctl stop    ${SERVICE_NAME}   # 停止服務"
echo "  sudo systemctl restart ${SERVICE_NAME}   # 重新啟動"
echo "  journalctl -u ${SERVICE_NAME} -f         # 即時查看日誌"
echo "  tail -f ${SCRIPT_DIR}/autoclock_bot.log  # 查看日誌檔"
