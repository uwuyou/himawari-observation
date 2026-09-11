#!/usr/bin/env bash
# =====================================================================
#  部署 himawari-site 到 GitHub Pages + 自动更新
#  用法（在已授权 GitHub 的新会话中）:
#     bash /workspace/himawari-site/deploy_github.sh            # 生成图 + 推送
#  首次会创建公开仓库 <your>/himawari-observation 并开启 Pages。
#  之后每次运行都会增量下载最新数据、重绘、提交并推送（网站自动更新）。
#  若要关闭「生成图」只看推送，设 SKIP_GEN=1。
# =====================================================================
set -uo pipefail

ROOT=/workspace
SITE="$ROOT/himawari-site"
ASSETS="$SITE/assets"
mkdir -p "$ASSETS"
cd "$SITE"

# 从私有凭据文件读取 token（须在仓库之外），无则用环境变量 GH_TOKEN
if [ -z "${GH_TOKEN:-}" ] && [ -f "$ROOT/.secrets/.github_token" ]; then
  export GH_TOKEN=$(cat "$ROOT/.secrets/.github_token")
fi
if [ -z "${GH_TOKEN:-}" ] && [ -f "$ROOT/himawari_site_token" ]; then
  export GH_TOKEN=$(cat "$ROOT/himawari_site_token")
fi
if [ -z "${GH_TOKEN:-}" ]; then
  echo "[错误] 未找到 GitHub token（.secrets 或 GH_TOKEN）"
  exit 1
fi
export GH_PROMPT_DISABLED=1
OWNER=$(gh api user --jq .login)
REPO="himawari-observation"
FULL="$OWNER/$REPO"

# 1) 生成最新图（可跳过）
if [ "${SKIP_GEN:-0}" != "1" ]; then
  echo "[1/4] 下载最新红外数据并绘图 ..."
  IR_LOG=$(PYTHONUNBUFFERED=1 python3 "$ROOT/himawari_s3_cloud_map.py" \
    --latest --composite B13 --out "$ASSETS/ir_latest.png" 2>&1)
  echo "$IR_LOG" | grep -E "自动选定时次|已保存|错误|Traceback" | tail -3
  TIME_UTC=$(echo "$IR_LOG" | grep -oE '[0-9]{12}' | head -1)
  if [ -n "$TIME_UTC" ]; then
    sed -i -E "s/const utc = \"[0-9]{12}\";/const utc = \"$TIME_UTC\";/" "$SITE/index.html"
    echo "[2/4] 云分类图 (UTC $TIME_UTC) ..."
    PYTHONUNBUFFERED=1 python3 "$ROOT/himawari_cloud_type.py" \
      --time "$TIME_UTC" --out "$ASSETS/cloudtype_latest.png" 2>&1 \
      | grep -E "已保存|晴空|低云|中云|错误" | tail -6
  else
    echo "[警告] 未探测到 IR 时次，仅推送已有图。"
  fi
fi

# 2) 初始化 git 仓库
[ -d .git ] || { git init -q; git branch -M main; }
git add -A
git -c user.name="himawari-bot" -c user.email="bot@users.noreply.github.com" \
  commit -q -m "update $(date -u +%Y%m%d%H%M)" 2>/dev/null || echo "[提示] 无内容变更"

# 3) 创建仓库（如不存在）并推送
if ! gh repo view "$FULL" >/dev/null 2>&1; then
  echo "[3/4] 创建公开仓库 $FULL ..."
  gh repo create "$REPO" --public --source=. --remote=origin --push >/dev/null
else
  echo "[3/4] 推送 $FULL ..."
  git -c user.name="himawari-bot" -c user.email="bot@users.noreply.github.com" \
    push -u origin main 2>/dev/null || \
  { git remote remove origin; git remote add origin "https://github.com/$FULL.git"; \
    git push -u origin main 2>&1 | tail -3; }
fi

# 4) 开启 GitHub Pages（从 main: /）
if ! gh api "repos/$FULL/pages" >/dev/null 2>&1; then
  echo "[4/4] 开启 GitHub Pages ..."
  gh api -X POST "repos/$FULL/pages" \
    -f "source[branch]=main" -f "source[path]=/" >/dev/null 2>&1 \
    || echo "[提示] Pages 开启需稍后生效"
fi
echo "=============================================="
echo "线上地址: https://$OWNER.github.io/$REPO/  (首次启用约需 1 分钟)"
echo "=============================================="