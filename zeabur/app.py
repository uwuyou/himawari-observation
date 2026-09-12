#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zeabur 一体化服务（纯 Zeabur 方案，不再依赖 GitHub / Vercel）
================================================================
一个常驻容器同时干两件事：
  1) 内置极简静态站点：把 Zeabur 网页服务直接当网站前端用，可从浏览器直接打开。
  2) 后台定时刷新：每 SCHED_MINUTES 分钟从 NOAA AWS S3 拉取 Himawari-9 数据，
     生成 红外云图 / 云分类图 / 夜间微物理图 / 火烧云预测，
     写入共享卷 {DATA_DIR}/assets/，并在末次写一个 obs_time.json 供前端轮询感知刷新。

架构：
  浏览器 ──>  Zeabur 容器(本服务)
                     ├─ Flask 静态站：/            -> index.html
                     │                  /assets/..  -> 共享卷 {DATA_DIR}/assets/..
                     └─ APScheduler 每 30 分钟：
                           先探测最新时次(出红外图) -> 再出云分类/夜间微物理/火烧云预测
                         -> 写 {DATA_DIR}/assets/*.png + *.json + obs_time.json

关键点：Zeabur 是「常驻容器」，没有 Vercel/GitHub 的定时与运行时长限制，
        所以数据处理(下载 GB 级卫星数据 + satpy 渲染)可以安全跑在这里。
        图片/JSON 必须放共享卷 {DATA_DIR} 里（挂载 NAS 存储），否则重建容器会丢。

运行（本地调试）：
    export DATA_DIR=/tmp/himawari_data
    python app.py

环境变量（均可选，有默认值）：
  DATA_DIR        共享数据目录（Zeabur 上挂载 NAS 卷到该路径），默认 /data
  SCHED_MINUTES   刷新周期（分钟），默认 30
  MAX_BACK_HOURS  探测最新时次回退范围（小时），默认 12
  CITY_NAME / CITY_LAT / CITY_LON  火烧云预测城市，默认 上海 31.23 121.47
  PORT            监听端口（Zeabur 会注入），默认 8080
"""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time

from flask import Flask, jsonify, send_file, request
from werkzeug.utils import safe_join
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("himawari-svc")

# ---------------- 配置 ----------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))          # /srv
WEB_DIR = os.path.join(APP_DIR, "web")                        # 静态站点
SCRIPTS_DIR = os.path.join(APP_DIR, "scripts")                # 生成脚本
DATA_DIR = os.environ.get("DATA_DIR", "/data").strip() or "/data"
ASSETS_DIR = os.path.join(DATA_DIR, "assets")

SCHED_MINUTES = int(os.environ.get("SCHED_MINUTES", "30"))
# 回退小时数：默认从 12 降到 3。卫星最新影像约滞后 15~40 分钟，
# 3 小时窗口足够覆盖；且帧数少 → 内存峰值显著下降，降低 2GB 实例被驱逐风险。
MAX_BACK_HOURS = float(os.environ.get("MAX_BACK_HOURS", "3"))
CITY_NAME = os.environ.get("CITY_NAME", "上海").strip() or "上海"
CITY_LAT = os.environ.get("CITY_LAT", "31.23")
CITY_LON = os.environ.get("CITY_LON", "121.47")
PORT = int(os.environ.get("PORT", "8080"))

# 上传发布：本地/常驻机器将渲染产物 POST 到 /api/upload，需带此 token。
# 只有设置了 UPLOAD_TOKEN，上传接口才生效；否则一律 403。
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()

# 是否在 Pod 内也自行渲染卫星图（重型，吃内存，默认关闭）。
# 关闭后容器只负责：托管静态站 + 接收 /api/upload 写入产物，2GB 绰绰有余。
RENDER_IN_POD = os.environ.get("RENDER_IN_POD", "0").strip().lower() in ("1", "true", "yes")

OBS_FILE = os.path.join(ASSETS_DIR, "obs_time.json")
PY = sys.executable

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# ---------------- 工具 ----------------
def _sh(args, cwd=None, timeout=1500):
    """执行命令，返回 (rc, out)。默认超时 25 分钟（首次下载量大）。"""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        log.warning("rc=%s 命令失败: %s\n%s", r.returncode, " ".join(args), out[-900:])
    return r.returncode, out


def _run_retry(name, args, cwd=None, tries=3, backoff=15, timeout=1500):
    """带重试的执行：吸收容器启动初期 / 网络瞬时抖动造成的 DNS、连接失败。"""
    last = (1, "")
    for i in range(tries):
        rc, out = _sh(args, cwd=cwd, timeout=timeout)
        if rc == 0:
            return 0, out
        last = (rc, out)
        if i < tries - 1:
            log.warning("%s 第 %s/%s 次失败，%ss 后重试", name, i + 1, tries, backoff)
            time.sleep(backoff)
    return last


def ensure_dirs():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(WEB_DIR, exist_ok=True)


# ---------------- 数据处理 ----------------
def detect_and_ir():
    """探测最新时次并出红外图。返回 (utc_YYYYMMDDHHMM 或 None, 是否成功)。"""
    out_png = safe_join(ASSETS_DIR, "ir_latest.png")
    ir_args = ["himawari_s3_cloud_map.py", "--latest",
               "--composite", "B13", "--max-back-hours", str(MAX_BACK_HOURS),
               "--out", out_png]
    rc, out = _run_retry("红外图", [PY] + ir_args, cwd=SCRIPTS_DIR)
    if rc != 0:
        return None, False
    # 脚本会打印「自动选定时次（UTC）：YYYYMMDDHHMM」或「自动找出最新时次: ...」
    for line in out.splitlines():
        m = None
        for tok in line.split():
            if tok.isdigit() and len(tok) == 12:
                m = tok
                break
        if m:
            return m, True
    log.warning("未从 IR 输出解析出时次:\n%s", out[-500:])
    return None, False


def run_generation(t_utc):
    """依次生成 云分类 / 夜间微物理 / 火烧云预测。单项失败不中断。"""
    ok = {"ir": False, "ct": False, "nm": False, "fc": False}

    # 1) 云分类图
    ct_png = safe_join(ASSETS_DIR, "cloudtype_latest.png")
    ct_args = [PY, "himawari_cloud_type.py", "--out", ct_png,
               "--max-back-hours", str(MAX_BACK_HOURS)]
    if t_utc:
        ct_args += ["--time", t_utc]
    else:
        ct_args += ["--latest"]
    ok["ct"] = (_run_retry("云分类图", [PY] + ct_args, cwd=SCRIPTS_DIR)[0] == 0)

    # 2) 夜间微物理合成图
    nm_png = safe_join(ASSETS_DIR, "night_microphysics_latest.png")
    nm_args = [PY, "himawari_s3_cloud_map.py", "--composite", "night_microphysics",
               "--max-back-hours", str(MAX_BACK_HOURS), "--out", nm_png]
    if t_utc:
        nm_args += ["--time", t_utc]
    else:
        nm_args += ["--latest"]
    ok["nm"] = (_run_retry("夜间微物理", nm_args, cwd=SCRIPTS_DIR)[0] == 0)

    # 3) 火烧云预测 + 云数据导出（供前端页面互动查询）
    fc_json = safe_join(ASSETS_DIR, "fire_prediction.json")
    cl_json = safe_join(ASSETS_DIR, "cloud_data.json")
    fc_args = [PY, "fire_cloud_predict.py",
               "--lat", CITY_LAT, "--lon", CITY_LON, "--name", CITY_NAME,
               "--json", fc_json, "--export-cloud-json", cl_json,
               "--max-back-hours", str(MAX_BACK_HOURS)]
    if t_utc:
        fc_args += ["--time", t_utc]
    else:
        fc_args += ["--latest"]
    ok["fc"] = (_run_retry("火烧云预测", fc_args, cwd=SCRIPTS_DIR)[0] == 0)

    return ok


def write_obs_time(t_utc):
    """写入前端轮询用的 obs_time.json（utc=观测时次，updated=版本号）。"""
    doc = {
        "utc": t_utc or "",
        "updated": int(time.time() * 1000),
        "city": CITY_NAME,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = OBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, OBS_FILE)
    log.info("已写 %s -> %s", OBS_FILE, t_utc or "(无时次)")


# ---------------- 定时任务 ----------------
job_lock = False
last_run = None
next_run = None


def run_update():
    global job_lock
    if job_lock:
        log.info("上一轮仍在运行，跳过")
        return
    job_lock = True
    t0 = time.time()
    log.info("—— 开始本轮数据刷新 ——")
    try:
        t_utc, ir_ok = detect_and_ir()
        log.info("探测时次: %s (IR成功=%s)", t_utc, ir_ok)
        run_generation(t_utc)
        write_obs_time(t_utc)
        log.info("—— 本轮刷新完成，耗时 %.0fs ——", time.time() - t0)
    except Exception as e:
        log.exception("本轮刷新异常: %s", e)
    finally:
        global last_run
        last_run = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job_lock = False


def create_scheduler():
    s = BackgroundScheduler(daemon=True)
    trigger = IntervalTrigger(minutes=SCHED_MINUTES)
    s.add_job(run_update, trigger, next_run_time=dt.datetime.now() + dt.timedelta(seconds=5))
    s.start()
    global next_run
    try:
        next_run = str(s.get_jobs()[0].next_run_time)
    except Exception:
        next_run = "unknown"
    log.info("调度器已启动，每 %d 分钟刷新一次，首次即将触发", SCHED_MINUTES)
    return s


# ---------------- HTTP 路由 ----------------
@app.route("/")
def index():
    return send_file(os.path.join(WEB_DIR, "index.html"))


@app.route("/assets/<path:name>")
def asset(name):
    p = safe_join(ASSETS_DIR, name)
    if not p or not os.path.isfile(p):
        return ("Not Found", 404)
    return send_file(p)


@app.route("/api/status")
def status():
    obs = {}
    try:
        with open(OBS_FILE, "r", encoding="utf-8") as f:
            obs = json.load(f)
    except Exception:
        obs = {}
    return jsonify(status="ok", schedule_minutes=SCHED_MINUTES,
                   last_run=last_run, next_run=next_run, obs=obs)


@app.route("/api/update", methods=["POST"])
def manual_update():
    """手动触发一轮刷新（仅调试用，且需 RENDER_IN_POD=1 才有意义）。"""
    if not RENDER_IN_POD:
        return jsonify(status="disabled", reason="RENDER_IN_POD=0，容器不渲染；请用常驻机器上传产物"), 403
    import threading
    threading.Thread(target=run_update, daemon=True).start()
    return jsonify(status="triggered")


@app.route("/api/upload", methods=["POST"])
def upload_asset():
    """常驻渲染机器把产物文件 POST 到此，写入 /data/assets。

    上传需带 `token`（查参或 X-Upload-Token 头），等于 Zeabur 环境变量 UPLOAD_TOKEN。
    文件名仅允许已知产物名，防止路径穿越。失败时 500，理论上读回最新文件。
    """
    if not UPLOAD_TOKEN:
        return jsonify(status="error", reason="未配置 UPLOAD_TOKEN，上传接口已禁用"), 403
    tok = request.values.get("token", "") or request.headers.get("X-Upload-Token", "")
    if tok != UPLOAD_TOKEN:
        return jsonify(status="error", reason="token 错误"), 403

    f = request.files.get("file")
    if f is None:
        return jsonify(status="error", reason="缺少 file 字段（multipart）"), 400
    name = os.path.basename(f.filename or "")
    if not name:
        return jsonify(status="error", reason="文件名非法"), 400
    # 只允许发布白名单产物，避免覆盖 index.html / app.py 等
    allowed = {"ir_latest.png", "cloudtype_latest.png", "night_microphysics_latest.png",
               "fire_prediction.json", "cloud_data.json", "obs_time.json"}
    if name not in allowed:
        return jsonify(status="error", reason="文件名不在白名单内"), 403

    dest = safe_join(ASSETS_DIR, name)
    try:
        with open(dest, "wb") as out:
            out.write(f.read())
    except Exception as e:
        log.exception("写入上传文件失败: %s", name)
        return jsonify(status="error", reason=str(e)), 500
    log.info("收到上传: %s (%s bytes)", name, os.path.getsize(dest))
    return jsonify(status="ok", name=name, size=os.path.getsize(dest))


if __name__ == "__main__":
    ensure_dirs()
    scheduler = None
    if RENDER_IN_POD:
        scheduler = create_scheduler()
    else:
        log.info("RENDER_IN_POD=0：容器仅托管站点 + 接收 /api/upload，不本地渲染（重型渲染交给常驻机器）")
    app.run(host="0.0.0.0", port=PORT, threaded=True)