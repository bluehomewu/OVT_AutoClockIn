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

echo "✅ 服務已完全移除。"
