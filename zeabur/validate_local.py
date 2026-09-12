#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机路由校验：不启动调度器，仅验证 Flask 静态站 + /assets + 状态接口。
用法：cd /workspace/himawari-site/zeabur && python3 validate_local.py
"""
import json
import os
import sys
import tempfile

# 指向临时数据目录，避免触碰 /data
TMP = tempfile.mkdtemp(prefix="himawari_test_")
os.environ["DATA_DIR"] = TMP
os.environ["SCHED_MINUTES"] = "9999"   # 未启用调度器（main 才启动），此处仅做防御

import app as svc  # noqa: E402

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    return cond

ok = True
client = svc.app.test_client()

# 准备测试用的 web/index.html 与 assets
os.makedirs(svc.WEB_DIR, exist_ok=True)
os.makedirs(svc.ASSETS_DIR, exist_ok=True)
with open(os.path.join(svc.WEB_DIR, "index.html"), "w", encoding="utf-8") as f:
    f.write("<html><body>TEST PAGE 观测台</body></html>")
with open(os.path.join(svc.ASSETS_DIR, "ir_latest.png"), "wb") as f:
    f.write(b"\x89PNG fake")
obs = {"utc": "202609120630", "updated": 1690000000000, "city": "上海"}
with open(os.path.join(svc.ASSETS_DIR, "obs_time.json"), "w", encoding="utf-8") as f:
    json.dump(obs, f, ensure_ascii=False)

ok &= check("GET / -> index.html", client.get("/").status_code == 200)
ok &= check("index.html 含中文", "观测台" in client.get("/").get_data(as_text=True))
ok &= check("GET /assets/ir_latest.png -> 200", client.get("/assets/ir_latest.png").status_code == 200)
ok &= check("GET /assets/obs_time.json -> 200", client.get("/assets/obs_time.json").status_code == 200)
ok &= check("GET /assets/缺失.png -> 404", client.get("/assets/not_exist.png").status_code == 404)
st = client.get("/api/status")
ok &= check("GET /api/status -> 200", st.status_code == 200)
ok &= check("obs.utc 正确", st.get_json().get("obs", {}).get("utc") == "202609120630")
ok &= check("/api/update 触发", client.post("/api/update").status_code == 200)

print("\n" + ("全部通过 ✔" if ok else "存在失败 ✘"))
sys.exit(0 if ok else 1)