#!/usr/bin/env bash
# =====================================================================
#  Himawari 云图站点自动更新脚本
#  - 用 --latest 增量下载最新时次（NOAA 公共 S3，缓存持久化，只补新分段）
#  - 绘制: 红外云图 B13  +  云类型分类图
#  - 回写 index.html 中的观测时次 (UTC)
#  用法: bash /workspace/himawari-site/update_site.sh
#  可选部署: DEPLOY=1 IGAAK=... IGASK=... bash .../update_site.sh
# =====================================================================
set -uo pipefail

ROOT=/workspace
SITE="$ROOT/himawari-site"
ASSETS="$SITE/assets"
mkdir -p "$ASSETS"

echo "[1/3] 下载最新红外数据并绘图 ..."
IR_LOG=$(PYTHONUNBUFFERED=1 python3 "$ROOT/himawari_s3_cloud_map.py" \
  --latest --composite B13 --out "$ASSETS/ir_latest.png" 2>&1)
echo "$IR_LOG" | grep -E "自动选定时次|已保存|错误|Traceback" | tail -4
TIME_UTC=$(echo "$IR_LOG" | grep -oE '[0-9]{12}' | head -1)

if [ -n "$TIME_UTC" ]; then
  echo "[2/3] 云分类图 (时次 $TIME_UTC UTC) ..."
  PYTHONUNBUFFERED=1 python3 "$ROOT/himawari_cloud_type.py" \
    --time "$TIME_UTC" --out "$ASSETS/cloudtype_latest.png" 2>&1 \
    | grep -E "已保存|晴空|低云|中云|错误|Traceback" | tail -8
else
  echo "[警告] 未探测到 IR 时次，跳过云分类。"
fi

# 回写观测时次到页面
if [ -n "$TIME_UTC" ]; then
  sed -i -E "s/const utc = \"[0-9]{12}\";/const utc = \"$TIME_UTC\";/" "$SITE/index.html"
  echo "[meta] 页面时次已更新为 $TIME_UTC UTC"
fi

# 可选部署
if [ "${DEPLOY:-0}" = "1" ]; then
  echo "[3/3] 部署到 IGA Pages ..."
  cd "$SITE"
  if [ -n "${IGAAK:-}" ] && [ -n "${IGASK:-}" ]; then
    iga login --accessKey "$IGAAK" --secretKey "$IGASK" >/dev/null 2>&1 || true
  fi
  iga pages deploy --name himawari-observation 2>&1 | tail -6 || true
fi
echo "[done]"