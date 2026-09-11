#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 NOAA 公共 AWS S3（免注册、免凭证）匿名下载 Himawari HSD 原始数据，
用 satpy（reader=ahi_hsd）读取并绘制“中国区域云图”。

数据桶（公开，匿名访问，无需账号）：
    Himawari-8 -> noaa-himawari8 ；Himawari-9 -> noaa-himawari9
    https://registry.opendata.aws/noaa-himawari/
键名（兼容新旧结构）：
    AHI-L1b-FLDK/{年份}/{月}/{日}/{HHMM}/HS_H<sat>_<YYYYMMDD>_<HHMM>_<BAND>_FLDK_R??_S<seg>10.DAT.bz2   （新）
    AHI-L1b-FLDK/{年份}/{年积日}/{HH}/HS_H<sat>_...S<seg>10.DAT.bz2                                  （老）

用法：
    python3 himawari_s3_cloud_map.py                     # H09、自动找最近时次、真彩色
    python3 himawari_s3_cloud_map.py --time 202609110600 --sat H09
    python3 himawari_s3_cloud_map.py --time 202609110600 --composite B13    # 红外云图(轻量快)
    python3 himawari_s3_cloud_map.py --time ... --bbox 70,3,140,55 --out out.png

依赖：pip install satpy cartopy xarray matplotlib
注意：true_color 需 B01+B02+B03，全盘合计数百 MB~GB 级，首次较慢；B13 单通道约 40MB，最快。
"""
import argparse
import bz2
import datetime as dt
import os
import re
import sys
import urllib.parse

import numpy as np
import urllib.request

BUCKETS = {"H08": "noaa-himawari8", "H09": "noaa-himawari9"}
# 复合产品 -> 所需波段（AHI）
COMPOSITE_BANDS = {
    "true_color": ["B01", "B02", "B03"],
    "natural_color": ["B05", "B07", "B11"],
    "overview": ["B01", "B02", "B03", "B05", "B07", "B11"],
    "night_microphysics": ["B07", "B13", "B15"],   # 夜间微物理 RGB：R=BTD(B15-B13) G=BTD(B13-B07) B=BT13（JMA 雾监测）
}
USER_AGENT = "Mozilla/5.0 fog-aws-cli"
CHINA_BBOX = (70, 3, 140, 55)


def _get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_keys(bucket, prefix):
    """匿名 ListObjectsV2，返回某前缀下全部对象键。"""
    keys, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        url = "https://%s.s3.amazonaws.com/?%s" % (bucket, urllib.parse.urlencode(params))
        xml = _get(url).decode("utf-8", "ignore")
        keys += re.findall(r"<Key>([^<]+)</Key>", xml)
        if re.search(r"<IsTruncated>false</IsTruncated>", xml):
            return keys
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        token = m.group(1) if m else None
        if not token:
            return keys


def discover_keys(bucket, ymd_hhmm):
    """对 UTC 时次 YYYYMMDDHHMM，尝试新/旧路径结构，返回 .bz2 键列表。"""
    y, m, d = int(ymd_hhmm[0:4]), int(ymd_hhmm[4:6]), int(ymd_hhmm[6:8])
    hh = ymd_hhmm[8:10]
    doy = dt.date(y, m, d).timetuple().tm_yday
    cands = [
        f"AHI-L1b-FLDK/{y:04d}/{m:02d}/{d:02d}/{hh}{ymd_hhmm[10:12]}/",  # 新：分钟
        f"AHI-L1b-FLDK/{y:04d}/{doy:03d}/{hh}/",                          # 老：年积日/整点
    ]
    for prefix in cands:
        keys = [k for k in list_keys(bucket, prefix) if k.endswith(".bz2")]
        if keys:
            return keys
    return []


def find_latest(bucket, now=None, max_back_hours=8, step_min=10):
    """自动探测最近一个“已有数据”的 10 分钟时次，返回 YYYYMMDDHHMM 或 None。

    NOAA 公共桶相对观测时间通常滞后 1-3 小时，本函数自 now 起按 10 分钟
    粒度逐时次回退，直到命中一个含 .bz2 数据的时次。
    """
    now = now or dt.datetime.utcnow()
    now = now.replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute // step_min * step_min)
    limit = int(max_back_hours * 60 / step_min) + 1
    for i in range(limit):
        t = now - dt.timedelta(minutes=i * step_min)
        ymd = f"{t.year:04d}{t.month:02d}{t.day:02d}{t.hour:02d}{t.minute:02d}"
        if discover_keys(bucket, ymd):
            return ymd
    return None


PAT = re.compile(r"_([A-Z]\d{2})_FLDK_R\d+_S(\d{2})10\.DAT\.bz2$")


def select_band_keys(keys, bands):
    """挑出属于指定波段的键，按分段号升序。"""
    picks = []
    for k in keys:
        m = PAT.search(k)
        if m and m.group(1) in bands:
            picks.append((int(m.group(2)), k))
    picks.sort()
    return [k for _, k in picks]


def ensure_local(bucket, keys, workdir):
    """按需下载 .bz2 并解压为 .DAT（命中本地缓存则跳过）。"""
    os.makedirs(workdir, exist_ok=True)
    local = []
    for key in keys:
        name = os.path.basename(key)[:-4]            # 去 .bz2 后缀
        dat = os.path.join(workdir, name)
        if not os.path.isfile(dat):
            url = "https://%s.s3.amazonaws.com/%s" % (bucket, urllib.parse.quote(key))
            print("  下载", os.path.basename(key))
            data = bz2.decompress(_get(url))
            with open(dat, "wb") as f:
                f.write(data)
        local.append(dat)
    return local


def make_scene(dat_files):
    from satpy import Scene, available_readers
    if "ahi_hsd" not in set(available_readers()):
        raise RuntimeError("当前 satpy 缺 ahi_hsd reader，请 pip install -U satpy")
    return Scene(filenames=dat_files, reader="ahi_hsd")


def plot(scene, composite, bbox, out, sat, res=0.02, dpi=200):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    for f in ("Noto Sans CJK SC", "WenQuanYi Micro Hei", "Noto Sans SC",
              "Microsoft YaHei", "SimHei"):
        if any(o.name == f for o in fm.fontManager.ttflist):
            matplotlib.rcParams["font.sans-serif"] = [f]
            break
    matplotlib.rcParams["axes.unicode_minus"] = False

    # 热红外波段（B07-B16）用亮温增强渲染，冷云顶=亮白、暖地表=深灰
    is_ir = bool(re.fullmatch(r"B(0[7-9]|1[0-6])", composite))
    is_night_micro = (composite == "night_microphysics")
    if is_night_micro:
        # JMA 式 Night Microphysics RGB：R=BTD(B15-B13) G=BTD(B13-B07) B=BT13
        scene.load(["B07", "B13", "B15"], calibration=["brightness_temperature"])
    elif is_ir:
        scene.load([composite], calibration=["brightness_temperature"])
    else:
        scene.load([composite])
    crop_scene = scene.crop(ll_bbox=bbox)

    # 重投影到等经纬度网格（便于按经纬度 extent 正确叠加国界）
    # res=0.02 度 ≈ 2km，与 B13/B15 热红外原始分辨率一致，裁切放大不糊
    from pyresample.geometry import AreaDefinition
    lon0, lat0, lon1, lat1 = bbox
    cols = max(2, int(round((lon1 - lon0) / res)))
    rows = max(2, int(round((lat1 - lat0) / res)))
    target = AreaDefinition(
        "chn", "chn", "chn",
        projection={"proj": "longlat", "datum": "WGS84", "ellps": "WGS84"},
        width=cols, height=rows,
        area_extent=(lon0, lat0, lon1, lat1),
    )
    rs = crop_scene.resample(target, resampler="nearest")

    if is_night_micro:
        # JMA Night Microphysics RGB：R=BTD(B15-B13) G=BTD(B13-B07) B=BT13
        BT13 = rs["B13"].values.astype("float64")
        BT07 = rs["B07"].values.astype("float64")
        BT15 = rs["B15"].values.astype("float64")
        R = BT15 - BT13           # BTD(B15-B13)
        G = BT13 - BT07           # BTD(B13-B07)  = -WB4
        B = BT13
        # 各通道采用百分位拉伸，自适应范围
        def _stretch(ch, lo=2, hi=98):
            v = ch[np.isfinite(ch)]
            if v.size == 0:
                return np.full_like(ch, 0.5)
            mn, mx = np.percentile(v, [lo, hi])
            if mx - mn < 1e-6:
                mx = mn + 1.0
            return np.clip((ch - mn) / (mx - mn), 0, 1)
        arr = np.stack([_stretch(R, 2, 98), _stretch(G, 2, 98), _stretch(B, 0, 100)], axis=0)
        tstr = str(rs["B13"].attrs.get("start_time", ""))[:16]
        data = rs["B13"]  # 仅用于 tstr
    else:
        data = rs[composite]
        arr = data.values
        tstr = str(data.attrs.get("start_time", ""))[:16]
    ext = (lon0, lon1, lat0, lat1)

    # figsize 使输出像素数与遥感网格基本一致，保证裁切放大细节不丢
    fig_w, fig_h = cols / dpi, rows / dpi
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        ax.set_extent(ext, crs=ccrs.PlateCarree())
        # 经纬网格线已按用户要求移除（不显示经纬网）
        ax.add_feature(cfeature.COASTLINE.with_scale("50m"), edgecolor="black", lw=1.0)
        ax.add_feature(cfeature.BORDERS, edgecolor="black", lw=0.8)
        ax.add_feature(cfeature.LAKES.with_scale("50m"), edgecolor="black", alpha=0.5)
        _render(ax, arr, bbox, ext, is_ir=is_ir)
    except Exception as e:
        print("  [提示] 回退基础展示:", e)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
        _render(ax, arr, bbox, None, is_ir=is_ir)

    ax.set_title(f"{sat} Himawari {composite} 云图  {tstr} UTC\n中国区域")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print("已保存：", os.path.abspath(out))


def _render(ax, arr, bbox, ext, is_ir=False):
    """公共绘图：RGB 真彩 or 单通道（红外/可见光）增强图。"""
    if arr.ndim == 3 and arr.shape[0] == 3:
        img = arr.transpose(1, 2, 0)
        img = (img - img.min()) / (img.max() - img.min() + 1e-9)
        kwargs = dict(transform=ax.projection, origin="upper",
                      interpolation="nearest") if ext else dict(origin="upper")
        if ext:
            kwargs["extent"] = ext
        ax.imshow(img, **kwargs)
    elif is_ir:
        # 夜间/红外亮温：190~320 K，冷云顶亮、暖地表暗
        img = np.clip((arr - 190.0) / (320.0 - 190.0), 0, 1)
        kwargs = dict(cmap="gray_r", origin="upper", vmin=0, vmax=1,
                      interpolation="nearest")
        if ext:
            kwargs.update(transform=ax.projection, extent=ext)
        ax.imshow(img, **kwargs)
    else:
        img = (arr - float(arr.min())) / (float(arr.max()) - float(arr.min()) + 1e-9)
        kwargs = dict(cmap="gray", origin="upper", vmin=0, vmax=1,
                      interpolation="nearest")
        if ext:
            kwargs.update(transform=ax.projection, extent=ext)
        ax.imshow(img, **kwargs)


def main():
    ap = argparse.ArgumentParser(description="NOAA 公共 AWS S3 下载 Himawari 并画中国区域云图")
    ap.add_argument("--sat", default="H09", choices=["H08", "H09"])
    ap.add_argument("--time", default=None, help="UTC 时次 YYYYMMDDHHMM（缺省自动探测）")
    ap.add_argument("--latest", action="store_true",
                    help="自动探测最近一个已有数据的 10 分钟时次")
    ap.add_argument("--max-back-hours", type=float, default=8.0,
                    help="--latest 回退搜索范围（小时）")
    ap.add_argument("--composite", default="true_color",
                    help="true_color / natural_color / overview，或单波段如 B13")
    ap.add_argument("--bbox", default=",".join(map(str, CHINA_BBOX)),
                    help="minLon,minLat,maxLon,maxLat")
    ap.add_argument("--out", default="himawari_chn_cloud.png")
    ap.add_argument("--workdir", default="./himawari_cache")
    ap.add_argument("--res", type=float, default=0.02,
                    help="重投影网格分辨率（度）。0.02≈2km 与红外原始一致；0.01≈1km 更高清")
    ap.add_argument("--dpi", type=int, default=200,
                    help="输出 PNG 分辨率（像素/英寸）。越高图越大越清楚")
    ap.add_argument("--years", action="store_true", help="列出 bucket 可用年份")
    args = ap.parse_args()

    bucket = BUCKETS[args.sat]

    if args.years:
        yrs = sorted(int(m.group(1)) for m in
                     (re.match(r"AHI-L1b-FLDK/(\d{4})/", p)
                      for p in list_keys(bucket, "AHI-L1b-FLDK/"))
                     if m)
        print("bucket:", bucket, "可用年份:", yrs)
        return 0

    if args.latest:
        args.time = find_latest(bucket, max_back_hours=args.max_back_hours)
        if not args.time:
            print(f"[错误] 近 {args.max_back_hours:g} 小时内未探测到可用时次（{bucket}）")
            return 1
        print("[--latest] 自动选定时次（UTC）：", args.time)
    elif not args.time:
        now = dt.datetime.utcnow()
        for off in range(0, 300, 10):
            t = now - dt.timedelta(minutes=off)
            args.time = t.strftime("%Y%m%d%H%M")
            if discover_keys(bucket, args.time):
                print("自动选定时次（UTC）：", args.time)
                break
        else:
            print("[错误] 未自动找到时次，请用 --time YYYYMMDDHHMM 指定")
            return 1

    keys = discover_keys(bucket, args.time)
    if not keys:
        print(f"[错误] bucket={bucket} 时次 {args.time} 无数据")
        return 1

    bbox = tuple(float(x) for x in args.bbox.split(","))
    bands = COMPOSITE_BANDS.get(args.composite, [args.composite])
    sel = select_band_keys(keys, bands)
    if not sel:
        print("[错误] 指定波段未找到:", bands)
        return 1
    print(f"选中 {len(sel)} 个 HSD 文件，波段 {bands}")

    dat_files = ensure_local(bucket, sel, args.workdir)
    scene = make_scene(dat_files)
    print("加载 satpy，生成", args.composite)
    plot(scene, args.composite, bbox, args.out, args.sat,
         res=args.res, dpi=args.dpi)
    return 0


if __name__ == "__main__":
    sys.exit(main())