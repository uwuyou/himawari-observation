#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zeabur 数据处理 Worker
======================
常驻服务。通过 APScheduler 定时轮询 NOAA Himawari-9 数据，生成云图、
云分类图、夜间微物理图和火烧云预测，并把结果 git 直推回 GitHub 仓库
（仓库再通过 GitHub Pages 自动构建展示网页）。

架构：
  Zeabur Worker（本服务，常驻，每 SCHED_MINUTES 分钟跑一次）
      │
      ├─ clone / pull 你的 himawari-observation 仓库（获取最新脚本）
      ├─ 运行 scripts/himawari_s3_cloud_map.py 探测最新时次 + 出红外图
      ├─ 运行 scripts/himawari_cloud_type.py 出云分类图
      ├─ 运行 scripts/himawari_s3_cloud_map.py --composite night_microphysics
      ├─ 运行 scripts/fire_cloud_predict.py 出火烧云预测 + 云数据
      ├─ 更新 index.html 里的观测时间戳
      └─ git commit + push 回仓库  →  触发 GitHub Pages 部署

运行（本地调试）：
    export GITHUB_TOKEN=你的token
    export GITHUB_REPO=uwuyou/himawari-observation
    python app.py

环境变量（均可选，有默认值）：
  GITHUB_TOKEN      必填。用于访问和推送仓库的 PAT。
  GITHUB_REPO       仓库，默认 uwuyou/himawari-observation
  WORK_DIR          仓库本地检查目录，默认 /tmp/himawari_repo
  SCHED_MINUTES     定时周期（分钟），默认 30
  MAX_BACK_HOURS    向历史回溯探测最新时次的最大小时数，默认 12
  CITY_NAME / CITY_LAT / CITY_LON  火烧云预测城市，默认 上海 31.23 121.47
  PORT              健康检查端口，默认 8080
"""

import logging
import os
import re
import shutil
import subprocess
import datetime as dt

from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("himawari-worker")

# ---------------- 配置 ----------------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "uwuyou/himawari-observation").strip()
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/himawari_repo").strip()
SCHED_MINUTES = int(os.environ.get("SCHED_MINUTES", "30"))
MAX_BACK_HOURS = int(os.environ.get("MAX_BACK_HOURS", "12"))
CITY_NAME = os.environ.get("CITY_NAME", "上海")
CITY_LAT = os.environ.get("CITY_LAT", "31.23")
CITY_LON = os.environ.get("CITY_LON", "121.47")
PORT = int(os.environ.get("PORT", "8080"))

CLONE_URL = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

app = Flask(__name__)


# ---------------- 仓库同步 ----------------
def _sh(cmd, cwd=None, check=False):
    """执行 shell 命令并返回 (rc, out)。"""
    r = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, shell=True
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败 rc={r.returncode}: {cmd}\n{r.stderr[-800:]}")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def ensure_repo():
    """确保本地有仓库副本；没有就 clone，有就 pull 更新。"""
    if not GITHUB_TOKEN:
        raise RuntimeError("缺少 GITHUB_TOKEN")
    if os.path.isdir(os.path.join(WORK_DIR, ".git")):
        _sh(f"git -C {WORK_DIR} reset --hard && git -C {WORK_DIR} clean -fd && git -C {WORK_DIR} pull --ff-only {CLONE_URL} main", check=True)
        log.info("仓库已更新")
    else:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        os.makedirs(os.path.dirname(WORK_DIR), exist_ok=True)
        _sh(f"git clone --depth 1 --branch main {CLONE_URL} {WORK_DIR}", check=True)
        log.info("仓库已克隆")


# ---------------- 数据处理 ----------------
def detect_obs_time():
    """探测最新可用时次，返回 YYYYMMDDHHMM 或 None。"""
    rc, out = _sh(
        f"python himawari_s3_cloud_map.py --latest "
        f"--composite B13 "
        f"--out ../assets/ir_latest.png "
        f"--max-back-hours {MAX_BACK_HOURS}",
        cwd=os.path.join(WORK_DIR, "scripts"),
    )
    m = re.search(r"\d{12}", out)
    if not m:
        log.warning("未探测到时次，IR 输出:\n%s", out[-800:])
        return None
    return m.group(0)


def run_generation(t_utc):
    """基于探测到的时次批量生成各类图与预测。"""
    scripts_dir = os.path.join(WORK_DIR, "scripts")

    # 1) 云分类图
    if t_utc:
        _sh(f"python himawari_cloud_type.py --time {t_utc} --out ../assets/cloudtype_latest.png",
            cwd=scripts_dir, check=True)
    else:
        _sh(f"python himawari_cloud_type.py --latest --max-back-hours {MAX_BACK_HOURS} "
            f"--out ../assets/cloudtype_latest.png", cwd=scripts_dir, check=True)

    # 2) 夜间微物理合成图
    if t_utc:
        _sh(f"python himawari_s3_cloud_map.py --time {t_utc} --composite night_microphysics "
            f"--out ../assets/night_microphysics_latest.png", cwd=scripts_dir, check=True)
    else:
        _sh(f"python himawari_s3_cloud_map.py --latest --max-back-hours {MAX_BACK_HOURS} "
            f"--composite night_microphysics --out ../assets/night_microphysics_latest.png",
            cwd=scripts_dir, check=True)

    # 3) 火烧云预测 + 云数据导出
    base = (f"python fire_cloud_predict.py --lat {CITY_LAT} --lon {CITY_LON} "
            f"--name {CITY_NAME} "
            f"--json ../assets/fire_prediction.json "
            f"--export-cloud-json ../assets/cloud_data.json")
    if t_utc:
        _sh(f"{base} --time {t_utc}", cwd=scripts_dir, check=True)
    else:
        _sh(f"{base} --latest --max-back-hours {MAX_BACK_HOURS}", cwd=scripts_dir, check=True)


def update_timestamp(t_utc):
    """更新 index.html 中的观测时间戳。"""
    idx = os.path.join(WORK_DIR, "index.html")
    if not os.path.exists(idx):
        log.warning("index.html 不存在，跳过时间戳更新")
        return
    with open(idx, "r", encoding="utf-8") as f:
        html = f.read()
    new_html = re.sub(r'const utc = "\d{12}";', f'const utc = "{t_utc}";', html)
    if new_html != html:
        with open(idx, "w", encoding="utf-8") as f:
            f.write(new_html)


def commit_push():
    """提交并推送所有变更到 main。"""
    git_config = [
        "git -C %s config user.name 'himawari-bot'" % WORK_DIR,
        "git -C %s config user.email 'bot@users.noreply.github.com'" % WORK_DIR,
    ]
    for c in git_config:
        _sh(c, check=True)
    _sh(f"git -C {WORK_DIR} add -A", check=True)
    rc, out = _sh(f"git -C {WORK_DIR} diff --cached --quiet")
    if rc == 0:
        log.info("无内容变更，跳过推送")
        return False
    stamp = dt.datetime.utcnow().strftime("%Y%m%d%H%M")
    _sh(f"git -C {WORK_DIR} commit -m 'auto update Himawari {stamp}'", check=True)
    _sh(f"git -C {WORK_DIR} pull --rebase {CLONE_URL} main", check=False)
    rc, out = _sh(f"git -C {WORK_DIR} push {CLONE_URL} main")
    if rc != 0:
        _sh(f"git -C {WORK_DIR} push --force-with-lease {CLONE_URL} main", check=True)
    log.info("已推送最新云图")
    return True


# ---------------- 全局状态 ----------------
last_run = None
_next_run = None


# ---------------- 定时任务 ----------------
def run_update():
    log.info("开始本轮数据更新 ...")
    try:
        ensure_repo()
        t_utc = detect_obs_time()
        log.info("探测到观测时次: %s", t_utc)
        run_generation(t_utc)
        update_timestamp(t_utc)
        commit_push()
        log.info("本轮更新完成 ✔")
    except Exception as e:
        log.exception("本轮更新失败: %s", e)
    finally:
        global last_run
        last_run = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    trigger = IntervalTrigger(minutes=SCHED_MINUTES)
    scheduler.add_job(run_update, trigger, next_run_time=dt.datetime.now() + dt.timedelta(seconds=3))
    scheduler.start()
    global _next_run
    _next_run = str(scheduler.get_jobs()[0].next_run_time)
    log.info("调度器已启动，每 %d 分钟运行一次，首次运行即将触发", SCHED_MINUTES)
    return scheduler


scheduler = create_scheduler()


@app.route("/")
def status():
    return jsonify(status="ok", repo=GITHUB_REPO, schedule_minutes=SCHED_MINUTES,
                   last_run=last_run, next_run=_next_run)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)