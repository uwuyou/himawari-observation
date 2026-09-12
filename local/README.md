# 本机发布脚本（常驻渲染）使用说明

用途：在你自己的电脑上每 30 分钟渲染一次卫星云图与火烧云预测，并上传到 Zeabur
静态托管（`/api/upload`）。Zeabur 只负责放页面和图，不跑重型渲染，不再吃内存。

## 目录结构

```
项目根/
├─ scripts/                    # 渲染脚本（已包含）
│   ├─ himawari_s3_cloud_map.py    # 下载 + 红外/夜间微物理出图
│   ├─ himawari_cloud_type.py      # 云分类图
│   └─ fire_cloud_predict.py       # 火烧云预测 + 光几何可见性
└─ local/
    ├─ publish.py              # 编排 + 上传（读取 config.env）
    ├─ config.env              # ★已填好域名和上传密钥，一般不用改
    ├─ run_publish.bat         # 一键运行（Windows 直接双击/调度用）
    ├─ requirements.txt        # 依赖清单
    └─ README.md
```

> `publish.py` 假定 `scripts/` 和 `local/` 在同一个项目根下，请保持此结构。

## 一、装依赖（一次性）

```bash
pip install -r local/requirements.txt
```

## 二、配置（已帮你填好）

`local/config.env` 里已预先填好你的真实上传密钥和新域名，**一般无需改动**：

```
UPLOAD_TOKEN=_Zpw-pSpMPiAboQasrpKoyQ0uV5PA8vu
DOMAIN=https://zgzwxahzz.zeabur.app
CITY_NAME=上海
CITY_LAT=31.23
CITY_LON=121.47
MAX_BACK_HOURS=3
```

如需换城市/改回退时长，改这里即可。此文件含密钥，**不要上传到 GitHub、不要外传**。

## 三、手动跑一次验证（必须做）

```bash
# Windows 直接双击 local/run_publish.bat，或：
python local/publish.py

# macOS / Linux：
python local/publish.py
```

看到结尾出现 `[INFO] —— 本轮发布完成 ——`，并且 Zeabur 侧日志出现
`收到上传: obs_time.json`，就说明整条链路通了。

如果你想临时用别的参数覆盖 config.env，可显式加 `--token` / `--domain` / `--city`
等（命令行参数优先级最高）。

## 四、设置每 30 分钟自动运行

### Windows（推荐用 bat）

1. 打开「任务计划程序」→「创建任务」。
2. 常规：名称随意，勾选「使用最高权限运行」。
3. 触发器：新建 →「按预定计划」「重复任务间隔 30 分钟」。
4. 操作：新建 → 程序填：
   ```
   C:\Windows\System32\cmd.exe
   ```
   参数填 `/c "D:\实时云图\local\run_publish.bat"`（换成你项目实际路径），
   起始位置填项目根路径。
5. 确定保存。
6. 保持电脑开机（别睡眠即可自动更新）。

### macOS / Linux（crontab）

```bash
crontab -e
```

追加一行（换成本机实际路径）：

```
*/30 * * * * cd /路径/你的项目 && python local/publish.py >> publish.log 2>&1
```

保存退出即可，日志写入 `publish.log`。

## 五、验证自动更新

- 打开 `https://zgzwxahzz.zeabur.app/api/status`，看 `obs` 里的 `utc` 是否有 12 位
  时次、`updated` 是否不断变化。
- 打开 `https://zgzwxahzz.zeabur.app/` 看三张云图 + 火烧云预测是否随最新观测时次更新。

## 排障

- 上传返回 403：`config.env` 里 `UPLOAD_TOKEN` 与 Zeabur 环境变量不一致。
- `Failed to resolve ...amazonaws.com`：本机网络抖动，脚本已带自动重试（3 次/步），
  一般会自动恢复。
- 内存不足/渲染慢：把 `MAX_BACK_HOURS` 从 3 调小（如 1），并关些占用内存的程序。