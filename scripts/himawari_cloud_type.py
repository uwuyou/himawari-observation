#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 Himawari-8/9 源数据（NOAA 公共 AWS S3，免注册）离线推算低/中/高云分类。

原理（多波段红外阈值法）：
  - 云顶亮温 BT13（11µm）分层：BT 越低云越高。
        BT13 < 235 K                       -> 高云（多为冰相/深对流）
        235 K <= BT13 < 262 K              -> 中云
        白天 BT13>=262K 且可见光反射率高   -> 低云
        夜间 BT13>=262K 且 3.9µm-11µm>2K  -> 低云（液态水云微物理正差）
        其余                               -> 晴空
  说明：这是业务常用的简化方案。白天用太阳校正后的可见光反射率区分低云与暖地表；
        夜间太阳反射消失，改用 3.9µm(B07) 微物理通道——液态水云 3.9µm 亮温高于
        11µm 呈正差，而冷却后的地表为负差，故正差即判低云。

用法：
  python3 himawari_cloud_type.py --time 202609110600
  python3 himawari_cloud_type.py --time 202609110600 --sat H08 --bbox 70,3,140,55 --out cls.png

依赖：satpy cartopy xarray matplotlib（与 himawari_s3_cloud_map.py 相同）
数据：B13、B15、B07（2km 热红外；B07=3.9µm 微物理通道用于夜间低云判据）
"""
import argparse
import os
import re
import sys

import numpy as np


def _dem_high_region(dem_path, lon0, lat0, lon1, lat1, shape, dem_thr):
    """读高程 GeoTIFF 并重投影到目标经纬网格，返回海拔 > dem_thr 的布尔掩膜。"""
    import numpy as np
    try:
        import rasterio
        from rasterio.enums import Resampling as RIO_RS
        from rasterio.transform import from_bounds
    except Exception as e:
        print(f"[警告] 无法加载 rasterio 以读取 DEM（{e}），跳过高程掩膜")
        return None
    height, width = int(shape[0]), int(shape[1])
    elev = np.zeros((height, width), dtype="float32")
    with rasterio.open(dem_path) as src:
        rasterio.warp.reproject(
            source=rasterio.band(src, 1), destination=elev,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=from_bounds(lon0, lat0, lon1, lat1, width, height),
            dst_crs="EPSG:4326", resampling=RIO_RS.bilinear, nodata=np.nan)
    return elev > dem_thr


def _cos_solar_zenith(ymd_hhmm, lon, lat):
    """逐像元太阳天顶角余弦。用于把可见光反射率归一到正午，抑制下午漏检。"""
    import numpy as np
    y, m, d = int(ymd_hhmm[0:4]), int(ymd_hhmm[4:6]), int(ymd_hhmm[6:8])
    hh = int(ymd_hhmm[8:10]); mm = int(ymd_hhmm[10:12])
    utc_hour = hh + mm / 60.0
    doy = (np.datetime64(f"{y}-{m:02d}-{d:02d}") - np.datetime64(f"{y}-01-01")).astype(int) + 1
    gamma = 2 * np.pi / 365.0 * (doy - 1 + (utc_hour - 12) / 24.0)
    decl = (0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
            - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
            - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma))
    phi = np.deg2rad(lat)
    H = np.deg2rad(15.0 * (utc_hour + lon / 15.0 - 12.0))  # 时角
    return np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(H)


def main():
    import himawari_s3_cloud_map as base  # 复用其下载/发现逻辑

    ap = argparse.ArgumentParser(description="Himawari 源数据 低/中/高云分类")
    ap.add_argument("--sat", default="H09", choices=["H08", "H09"])
    ap.add_argument("--time", default="202609110600", help="UTC 时次 YYYYMMDDHHMM（--latest 存在时忽略）")
    ap.add_argument("--latest", action="store_true",
                    help="自动探测最近一个已有数据的 10 分钟时次")
    ap.add_argument("--max-back-hours", type=float, default=8.0,
                    help="--latest 回退搜索范围（小时）")
    ap.add_argument("--bbox", default="70,3,140,55", help="minLon,minLat,maxLon,maxLat")
    ap.add_argument("--out", default="himawari_cloud_type.png")
    ap.add_argument("--workdir", default="./himawari_cache")
    ap.add_argument("--thr-high", type=float, default=235.0, help="高云亮温阈值 K")
    ap.add_argument("--thr-mid", type=float, default=262.0, help="中云亮温阈值 K")
    ap.add_argument("--thr-split", type=float, default=0.6, help="低云分裂窗阈值 K")
    ap.add_argument("--thr-low-max", type=float, default=296.0,
                    help="低云云顶亮温上限 K（避免午后暖地表误判为低云）")
    ap.add_argument("--visible", dest="visible", action="store_true",
                    help="叠加可见光 B04(0.86µm) 反射率，白天判云更准（推荐）")
    ap.add_argument("--no-visible", dest="visible", action="store_false",
                    help="关闭可见光，纯红外法")
    ap.set_defaults(visible=True)
    ap.add_argument("--ref-thr", type=float, default=40.0,
                    help="白天低云可见光反照率阈值（B02 0.51µm，0-100 百分比）")
    ap.add_argument("--sza-fixed", action="store_true",
                     help="把太阳高度角固定为正午（normREF=原始反射率）。注意：下午原始反射率偏低，固定后反而更不漏，仅供对比")
    ap.add_argument("--thr-bt47", type=float, default=10.0,
                    help="白天低云 3.9µm-11µm 亮温差阈值 K（液态水云正差大，用于捞暗低云）")
    ap.add_argument("--bt47-ref-min", type=float, default=15.0,
                    help="白天该水云微物理路径要求的最低归一化反射率，避免把过暗地表也判为云")
    ap.add_argument("--thr-bt47-night", type=float, default=2.0,
                    help="夜间低云 3.9µm-11µm 亮温差阈值 K（夜间水云正差约1-3K，内陆地表为负；默认2可分离）")
    ap.add_argument("--thr-dc", type=float, default=210.0,
                    help="深对流云顶亮温上限 K（更冷即为深对流）")
    ap.add_argument("--cir-split-day", type=float, default=2.0,
                    help="卷云白天 11-12.4µm 分裂窗下限 K（薄冰云日间正差大）")
    ap.add_argument("--cir-split-night", type=float, default=-1.5,
                    help="卷云夜间分裂窗上限 K（薄冰云夜间负差）")
    ap.add_argument("--thr-snow", type=float, default=0.35,
                    help="积雪 NDSI(可见-B05可见差值指数)阈值，高于此判为雪（剔除，非低云）")
    ap.add_argument("--dem", default=None,
                    help="可选：高程 GeoTIFF 路径（如 SRTM/ETOPO1），用于按海拔剔除亮地表假阳")
    ap.add_argument("--dem-thr", type=float, default=2600.0,
                    help="海拔高于该值(m)的区域不判低云")
    ap.add_argument("--res", type=float, default=0.02,
                    help="重投影网格分辨率（度）。0.02≈2km 与红外原始一致；0.01≈1km 最高清")
    ap.add_argument("--dpi", type=int, default=200,
                    help="输出 PNG 分辨率（像素/英寸），越高越清晰")
    args = ap.parse_args()

    bucket = base.BUCKETS[args.sat]
    if args.latest:
        args.time = base.find_latest(bucket, max_back_hours=args.max_back_hours)
        if not args.time:
            print(f"[错误] 近 {args.max_back_hours:g} 小时内未探测到可用时次（{bucket}）")
            return 1
        print(f"[--latest] 自动选取最新时次: {args.time} UTC")
    bbox = tuple(float(x) for x in args.bbox.split(","))
    lon0, lat0, lon1, lat1 = bbox

    bands = ["B13", "B15", "B07"]   # B07=3.9µm 微物理通道，夜间低云判据必需
    if args.visible:
        bands.append("B02")   # 0.51µm 可见光，用于白天“低云 vs 地表”
        bands.append("B05")   # 1.6µm 短波红外，用于剔除积雪（雪可见光亮、1.6µm暗）
    keys = base.discover_keys(bucket, args.time)
    if not keys:
        print(f"[错误] bucket={bucket} 时次 {args.time} 无数据")
        return 1
    sel = base.select_band_keys(keys, bands)
    print(f"选中 {len(sel)} 个 HSD 文件，波段 {bands}")
    dats = base.ensure_local(bucket, sel, args.workdir)

    from satpy import Scene, available_readers
    if "ahi_hsd" not in set(available_readers()):
        print("[错误] 缺少 ahi_hsd reader，请 pip install -U satpy")
        return 1
    scn = Scene(filenames=dats, reader="ahi_hsd")
    scn.load(["B13", "B15", "B07"], calibration=["brightness_temperature"])
    if args.visible:
        scn.load(["B02", "B05"], calibration=["reflectance"])

    # 裁剪 + 重投影到经纬度网格
    from pyresample.geometry import AreaDefinition
    crop = scn.crop(ll_bbox=bbox)
    res = args.res
    target = AreaDefinition("chn", "chn", "chn",
                            projection={"proj": "longlat", "datum": "WGS84", "ellps": "WGS84"},
                            width=int((lon1 - lon0) / res), height=int((lat1 - lat0) / res),
                            area_extent=(lon0, lat0, lon1, lat1))
    rs = crop.resample(target, resampler="nearest")
    BT = rs["B13"].values.astype("float64")
    BT15 = rs["B15"].values.astype("float64")
    split = BT - BT15
    tstr = str(rs["B13"].attrs.get("start_time", ""))[:16]

    # 白天判定：可见光反照率整体较高即视为白天（启用可见光时）
    daytime = False
    snow = None
    dem_high = None
    normREF = None
    if args.visible and "B02" in rs:
        REF = rs["B02"].values.astype("float64")
        REF5 = rs["B05"].values.astype("float64")
        hh, ww = REF.shape[0], REF.shape[1]
        _lons = lon0 + (np.arange(ww) + 0.5) * (lon1 - lon0) / ww
        _lats = lat0 + (np.arange(hh) + 0.5) * (lat1 - lat0) / hh
        LON, LAT = np.meshgrid(_lons, _lats)
        cosZ = _cos_solar_zenith(args.time, LON, LAT)
        # 反射率按太阳高度角归一化，抬正午后因太阳过低导致的可见光反射率整体下压
        if args.sza_fixed:
            normREF = REF.copy()  # 固定正午：使用原始反射率
        else:
            normREF = REF / np.clip(cosZ, 0.15, 1.0)
        # 以太阳高度角判定白天（cosZ 中位 >0.25 ≈ 太阳高于 15°），比用反射率阈值更可靠，
        # 避免凌晨/黄昏把夜间误当白天
        daytime = bool(np.nanmedian(cosZ) > 0.25) if cosZ.size else False
        # 积雪剔除：雪可见光亮(B02高)、1.6µm暗(B05低) → NDSI指数（用百分比直接算）
        eps = 1e-6
        snow = (REF - REF5) / (REF + REF5 + eps) > args.thr_snow
    else:
        REF = None

    # 3.9µm 微物理差（液态水云强正差），白天捞暗低云、夜间判低云
    BT4 = rs["B07"].values.astype("float64") if "B07" in rs else np.full(BT.shape, np.nan)
    WB4 = BT4 - BT
    # 可选 DEM 高程掩膜：海拔过高处不应出现低云
    if args.dem:
        dem_high = _dem_high_region(args.dem, lon0, lat0, lon1, lat1,
                                    rs["B13"].shape, args.dem_thr)

    # ---- 分类 ----
    # 0=晴空, 1=低云, 2=中云, 3=深对流, 4=卷云, 5=层状高云
    cls = np.zeros(BT.shape, dtype=np.uint8)
    cls[BT < args.thr_high] = 5               # 先统置为“高云”
    cls[(BT >= args.thr_high) & (BT < args.thr_mid)] = 2
    if REF is not None and daytime:
        # 白天低云：仅用归一化可见光反射率（已太阳校正）。
        # 注意：不在白天叠加 3.9µm 微物理——白天地表（尤其沙漠/裸岩）受太阳加热
        # 致 3.9µm-11µm 也呈大正差，实测新疆沙漠会 48.7% 假阳。3.9µm 仅用于夜间。
        low = (BT >= args.thr_mid) & (BT <= args.thr_low_max) & (normREF > args.ref_thr)
        if snow is not None:
            low &= ~snow
        if dem_high is not None:
            low &= ~dem_high
        cls[low] = 1
    else:
        # 夜间：无太阳反射干扰，3.9µm-11µm 正差显著即液态水云（低云），替代原分裂窗法。
        # 夜间暖地表冷却、3.9µm 亮温低于 11µm 呈负差，而液态水云的 3.9µm>11µm 正差仍可分离。
        night_low = ((BT >= args.thr_mid) & (BT <= args.thr_low_max)
                     & (WB4 > args.thr_bt47_night))
        cls[night_low] = 1
        _dbg = (BT >= args.thr_mid) & (BT <= args.thr_low_max) & (WB4 > args.thr_bt47_night)
        _valid = np.isfinite(BT) & np.isfinite(BT4)
        print(
            f"[DBG] 夜间 daytime={daytime} BT非有限={int((~np.isfinite(BT)).sum())} "
            f"BT4非有限={int((~np.isfinite(BT4)).sum())} "
            f"262<=BT<=296 且 WB4非有限={int((_valid&(BT>=args.thr_mid)&(BT<=args.thr_low_max)&~np.isfinite(WB4)).sum())} "
            f"夜低候选(262<=BT<=296且WB4>{args.thr_bt47_night:g})={( _dbg).sum()} "
            f"其中有限WB4={int((_valid&_dbg).sum())}",
            file=sys.stderr)
    # 高云细分：深对流(极冷厚顶) > 卷云(薄冰,分裂窗日正夜负) > 层状高云(其余)
    highmask = BT < args.thr_high
    deep = highmask & (BT < args.thr_dc)
    cls[deep] = 3
    if np.any(highmask & ~deep):
        if daytime:
            cir = highmask & ~deep & (split > args.cir_split_day)
        else:
            cir = highmask & ~deep & (split < args.cir_split_night)
        cls[cir] = 4
    cls[np.isnan(BT)] = 0

    # ---- 着色 ----
    color = np.array([
        [0.12, 0.24, 0.50],   # 0 晴空 蓝
        [0.13, 0.72, 0.35],   # 1 低云 绿
        [0.98, 0.84, 0.05],   # 2 中云 黄
        [1.00, 1.00, 1.00],   # 3 深对流 白
        [0.75, 0.52, 0.99],   # 4 卷云 紫
        [0.56, 0.81, 0.91],   # 5 层状高云 浅蓝
    ])
    rgb = color[cls]

    # ---- 绘制 ----
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

    # 输出像素数与重投影网格匹配，保证裁切放大不糊
    fig_w = (lon1 - lon0) / res / args.dpi
    fig_h = (lat1 - lat0) / res / args.dpi
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        fig = plt.figure(figsize=(fig_w, fig_h), dpi=args.dpi)
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        ax.set_extent((lon0, lon1, lat0, lat1), crs=ccrs.PlateCarree())
        ax.gridlines(draw_labels=True, dms=True, color="gray", alpha=0.6, lw=0.5)
        ax.add_feature(cfeature.COASTLINE.with_scale("50m"), edgecolor="black", lw=0.9)
        ax.add_feature(cfeature.BORDERS, edgecolor="black", lw=0.7)
        ax.add_feature(cfeature.LAKES.with_scale("50m"), edgecolor="black", alpha=0.5)
        ax.imshow(rgb, origin="upper", transform=ccrs.PlateCarree(),
                  extent=(lon0, lon1, lat0, lat1), interpolation="nearest")
    except Exception as e:
        print("[提示] cartopy 不可用，基础显示:", e)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=args.dpi)
        ax.imshow(rgb, origin="upper")

    # 图例
    import matplotlib.patches as mpatches
    handles = [mpatches.Patch(color=color[i], label=lbl)
               for i, lbl in enumerate(["晴空", "低云", "中云", "深对流", "卷云", "层状高云"])]
    ax.legend(handles=handles, loc="lower left", fontsize=8.5, frameon=True, ncol=2,
              bbox_to_anchor=(0.01, 0.01))

    ax.set_title(f"{args.sat} Himawari 云分类（高云细分）  {tstr} UTC\n中国区域（高云: 深对流<{args.thr_dc:.0f}K | 卷云薄冰 | 层状其余）")
    fig.tight_layout()
    fig.savefig(args.out, bbox_inches="tight")
    print("已保存：", os.path.abspath(args.out))

    # 统计
    for i, lbl in enumerate(["晴空", "低云", "中云", "深对流", "卷云", "层状高云"]):
        n = int((cls == i).sum())
        frac = n / (np.isfinite(BT).sum() or 1) * 100
        print(f"  {lbl}: {frac:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())