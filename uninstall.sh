#!/usr/bin/env bash
# uninstall.sh — 移除 OVT 自動打卡機器人的 systemd 服務

set -e

SERVICE_NAME="autoclock"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "================================================"
echo " OVT 自動打卡機器人 — 解除安裝程式"
echo "================================================"

if systemctl is-active --quiet "${SERVICE_NAME}"; then
    echo "🛑 停止服務..."
    systemctl stop "${SERVICE_NAME}"
fi

if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    echo "🔕 取消開機自動啟動..."
    systemctl disable "${SERVICE_NAME}"
fi

if [ -f "${SERVICE_FILE}" ]; then
    echo "🗑️  移除 service 檔案..."
    rm -f "${SERVICE_FILE}"
fi

systemctl daemon-reload

echo ""
echo "================================================"
echo " 選用清理項目（可保留供下次安裝使用）"
echo "================================================"

INSTALL_DIR="$(dirname "$(realpath "$0")")"

ask_remove() {
    local desc="$1"
    local path="$2"
    if [ -e "$path" ]; then
        read -rp "🗑️  是否刪除 ${desc} (${path})？[y/N] " ans
        case "$ans" in
            [Yy]*) rm -f "$path" && echo "   已刪除。" ;;
            *)     echo "   保留。" ;;
        esac
    fi
}

ask_remove "打卡狀態檔 bot_status.json"  "${INSTALL_DIR}/bot_status.json"
ask_remove "機器人日誌 autoclock_bot.log" "${INSTALL_DIR}/autoclock_bot.log"
ask_remove "例外打卡日清單 exceptions.txt" "${INSTALL_DIR}/exceptions.txt"

echo ""
echo "✅ 服務已完全移除。"
