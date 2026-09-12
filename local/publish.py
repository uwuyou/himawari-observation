#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地/常驻机器发布脚本：渲染卫星图与火烧云预测 → 上传到 Zeabur 静态托管。

架构：重型渲染在本机/常驻机器跑（吃得住内存），Zeabur 只当轻量托管，
通过 /api/upload 接收产物写入 /data/assets，浏览器再从 /assets/* 读取轮询。

用法（推荐配合系统定时器，每 30 分钟跑一次）：
    python local/publish.py
    # 或指定：
    python local/publish.py --domain https://sunsetplan.zeabur.app \
                            --token <UPLOAD_TOKEN> --out /path/to/assets_out

依赖：本机已能跑通 scripts/ 下脚本（satpy/cartopy/matplotlib）+ requests。
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time

import requests

# 项目根 = 本文件上级的上级（local/..）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable

# 允许上传到 /api/upload 的白名单文件名（须与 zeabur/app.py 约定一致）
ALLOWED = ["ir_latest.png", "cloudtype_latest.png", "night_microphysics_latest.png",
           "fire_prediction.json", "cloud_data.json", "obs_time.json"]


def sh(args, cwd=SCRIPTS, timeout=1500):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        print(f"[WARN] rc={r.returncode} 命令失败: {' '.join(args)}\n{out[-800:]}", file=sys.stderr)
    return r.returncode, out


def sh_retry(name, args, tries=3, backoff=15):
    for i in range(tries):
        rc, out = sh(args)
        if rc == 0:
            return 0, out
        if i < tries - 1:
            print(f"[WARN] {name} 第 {i+1}/{tries} 次失败，{backoff}s 后重试", file=sys.stderr)
            time.sleep(backoff)
    return rc, out


def extract_utc(out):
    """从脚本输出里抓 YYYYMMDDHHMM 时次。"""
    for line in out.splitlines():
        for tok in line.split():
            if tok.isdigit() and len(tok) == 12:
                return tok
    return None


def render(args):
    """渲染全部产物。返回 (t_utc, ok_dict)。单项失败不中断、不覆盖旧产物。"""
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    kh = ["--max-back-hours", str(args.back_hours)]
    ok = {"ir": False, "ct": False, "nm": False, "fc": False}
    t_utc = None

    # 1) 红外图（同时探测最新时次）
    ir_png = os.path.join(out_dir, "ir_latest.png")
    rc, out = sh_retry("红外图", [PY, "himawari_s3_cloud_map.py", "--latest",
                                  "--composite", "B13", *kh, "--out", ir_png])
    ok["ir"] = (rc == 0)
    t_utc = extract_utc(out)
    print(f"[INFO] 时次: {t_utc or '(未探测到)'} IR成功={ok['ir']}")
    if t_utc is None:
        print("[WARN] 未探测到时次，本轮其它产物将沿用 --latest", file=sys.stderr)

    # 2) 云分类图
    ct_png = os.path.join(out_dir, "cloudtype_latest.png")
    ct_args = [PY, "himawari_cloud_type.py", *kh, "--out", ct_png]
    ct_args += ["--time", t_utc] if t_utc else ["--latest"]
    ok["ct"] = (sh_retry("云分类图", ct_args)[0] == 0)

    # 3) 夜间微物理合成图
    nm_png = os.path.join(out_dir, "night_microphysics_latest.png")
    nm_args = [PY, "himawari_s3_cloud_map.py", "--composite", "night_microphysics",
               *kh, "--out", nm_png]
    nm_args += ["--time", t_utc] if t_utc else ["--latest"]
    ok["nm"] = (sh_retry("夜间微物理", nm_args)[0] == 0)

    # 4) 火烧云预测 + 云数据导出
    fc_json = os.path.join(out_dir, "fire_prediction.json")
    cl_json = os.path.join(out_dir, "cloud_data.json")
    fc_args = [PY, "fire_cloud_predict.py", "--lat", args.lat, "--lon", args.lon,
               "--name", args.city, "--json", fc_json, "--export-cloud-json", cl_json,
               *kh]
    fc_args += ["--time", t_utc] if t_utc else ["--latest"]
    ok["fc"] = (sh_retry("火烧云预测", fc_args)[0] == 0)

    # 5) obs_time.json（通知前端"时次变了，去换新图"）
    obs = {"utc": t_utc or "", "updated": int(time.time() * 1000),
           "city": args.city, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(out_dir, "obs_time.json"), "w", encoding="utf-8") as f:
        import json
        json.dump(obs, f, ensure_ascii=False)

    return t_utc, ok


def upload_one(domain, token, path):
    name = os.path.basename(path)
    with open(path, "rb") as fp:
        r = requests.post(f"{domain}/api/upload",
                          files={"file": (name, fp)},
                          data={"token": token},
                          timeout=300)
    if r.status_code != 200:
        print(f"[WARN] 上传失败 {name}: HTTP {r.status_code} {r.text[:160]}", file=sys.stderr)
        return False
    print(f"[INFO] 已上传 {name} ({len(open(path, 'rb').read())}B)")
    return True


def publish(args):
    t_utc, ok = render(args)
    print("[INFO] —— 开始上传 ——")
    upload_dir = args.out
    ok_upload = {}
    for name in ALLOWED:
        p = os.path.join(upload_dir, name)
        if os.path.isfile(p):
            ok_upload[name] = upload_one(args.domain, args.token, p)
        else:
            print(f"[WARN] 本机不存在产物，跳过上传 {name}", file=sys.stderr)
            ok_upload[name] = False
    print("[INFO] —— 本轮发布完成 ——")
    return 0 if (ok["ir"] and ok_upload.get("obs_time.json")) else 1


def load_config_env():
    """读取同目录下 config.env / .env（KEY=VALUE），写入环境变量。
    优先级：CLI 参数 > 系统环境变量 > config.env 文件。此函数只 setdefault，
    不覆盖已存在的环境变量。"""
    for dirpath in (os.path.join(ROOT, "local"), ROOT):
        for name in ("config.env", ".env"):
            pth = os.path.join(dirpath, name)
            if os.path.isfile(pth):
                with open(pth, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
                return


def main():
    load_config_env()
    p = argparse.ArgumentParser(description="本机渲染并发布到 Zeabur")
    p.add_argument("--domain", default=os.environ.get("DOMAIN", ""))
    p.add_argument("--token", default=os.environ.get("UPLOAD_TOKEN", ""))
    p.add_argument("--out", default=os.environ.get("OUT_DIR", os.path.join(ROOT, "assets_out")))
    p.add_argument("--back-hours", type=float, default=float(os.environ.get("MAX_BACK_HOURS", "3")))
    p.add_argument("--city", default=os.environ.get("CITY_NAME", "上海"))
    p.add_argument("--lat", default=os.environ.get("CITY_LAT", "31.23"))
    p.add_argument("--lon", default=os.environ.get("CITY_LON", "121.47"))
    args = p.parse_args()
    if not args.token:
        print("错误：缺少 UPLOAD_TOKEN。可在 local/config.env 里配，或用 --token 传。", file=sys.stderr)
        return 2
    if not args.domain:
        print("错误：缺少 DOMAIN。可在 local/config.env 里配，或用 --domain 传。", file=sys.stderr)
        return 2
    sys.exit(publish(args))


if __name__ == "__main__":
    main()