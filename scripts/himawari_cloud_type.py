#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 Himawari-8/9 源数据（NOAA 公共 AWS S3，免注册）离线推算低/中/高云分类。

原理（参考 JMA HCAI / CMP 算法体系，MSC Tech Note 61 & 62）：
  - 云顶亮温 BT13（10.4µm）主要分层：
        BT13 < 235 K                    → 高云（冰相）
        235 K ≤ BT13 < 262 K            → 中云
        BT13 ≥ 262 K                    → 低云 或 晴空（白天用可见光，夜间用 3.9µm 微物理）
  - 高云细分（参考 HCAI 分裂窗技术）：
        BT13 < 228 K                    → 深对流（Cb，冷顶厚冰云）
        BT13 < 235K 且 BTD(B13-B15)≥1.5K → 卷云（薄半透明冰晶）
        其余高云                         → 层状高云（厚冰云/密卷云）
  - 低云局地纹理（参考 HCAI 基于邻域 BT 变化区分 Cu/Sc/St）：
        BT13 邻域标准差 > 4K            → 积云（Cu，起伏大）
        BT13 邻域标准差 2~4K           → 层积云（Sc，中等纹理）
        BT13 邻域标准差 < 2K            → 层云/雾（St/Fg，平坦均匀）
  - 夜间低云使用 B07(3.9µm)-B13(10.4µm) 亮温差 WB4（液态水云正差），
    并融入 Night Microphysics RGB 三维判据（JMA 雾监测技术文档）：
    分裂窗 B13-B15 ≈ 0（光学厚水云→层云/雾）vs 偏差大（层积云）
    配合 BT13 局地纹理区分积云

用法：
  python3 himawari_cloud_type.py --latest
  python3 himawari_cloud_type.py --time 202609110600 --sat H08 --bbox 70,3,140,55 --out cls.png
  python3 himawari_cloud_type.py --latest --hcai-mode    # 完整 HCAI 模式（含 B08/B10 水汽通道）

依赖：satpy cartopy xarray matplotlib scipy
数据：B13(10.4µm)、B15(12.4µm)、B07(3.9µm) + B02(0.51µm)/B05(1.6µm)（白天积雪剔除）
     B14(11.2µm) 可选（Night Microphysics 增强）
"""
import argparse
import os
import re
import sys

import numpy as np


def _dem_high_region(dem_path, lon0, lat0, lon1, lat1, shape, dem_thr):
    """读高程 GeoTIFF 并重投影到目标经纬网格，返回海拔 > dem_thr 的布尔掩膜。"""
    try:
        import rasterio
        from rasterio.enums import Resampling as RIO_RS
        from rasterio.transform import from_bounds
    except ImportError:
        print("[警告] 无 rasterio，跳过高程掩膜")
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
    """逐像元太阳天顶角余弦。"""
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
    H = np.deg2rad(15.0 * (utc_hour + lon / 15.0 - 12.0))
    return np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(H)


def _local_std(arr, win=5):
    """滑动窗口标准差。win=5 约对应 10km（0.02°×5），适合检测云顶纹理。"""
    from scipy.ndimage import uniform_filter
    arr = np.where(np.isfinite(arr), arr, 0)
    c1 = uniform_filter(arr, size=win, mode="reflect")
    c2 = uniform_filter(arr * arr, size=win, mode="reflect")
    var = np.maximum(c2 - c1 * c1, 0)
    return np.sqrt(var)


def classify_scene(rs, args, tstr):
    """
    输入 satpy 重投影后的 Scene (rs)，返回云分类数组 cls (uint8)。
    编码:
      0=晴空 1=层云/雾 2=层积云 3=积云 4=中云 5=深对流 6=卷云 7=层状高云
    """
    import numpy as np
    BT = rs["B13"].values.astype("float64")
    BT15 = rs["B15"].values.astype("float64")
    split = BT - BT15
    BT4 = rs["B07"].values.astype("float64")
    WB4 = BT4 - BT

    REF = rs["B02"].values.astype("float64")
    REF5 = rs["B05"].values.astype("float64")

    bbox = tuple(float(x) for x in args.bbox.split(","))
    lon0, lat0, lon1, lat1 = bbox
    hh, ww = REF.shape[0], REF.shape[1]
    _lons = lon0 + (np.arange(ww) + 0.5) * (lon1 - lon0) / ww
    _lats = lat0 + (np.arange(hh) + 0.5) * (lat1 - lat0) / hh
    LON, LAT = np.meshgrid(_lons, _lats)
    cosZ = _cos_solar_zenith(tstr.replace("-","").replace(":","").replace(" ",""),
                             LON, LAT)
    normREF = REF / np.clip(cosZ, 0.15, 1.0)
    daytime = bool(np.nanmedian(cosZ) > 0.25) if cosZ.size else False

    eps = 1e-6
    snow = (REF - REF5) / (REF + REF5 + eps) > args.thr_snow

    dem_high = None
    if args.dem:
        dem_high = _dem_high_region(args.dem, lon0, lat0, lon1, lat1,
                                    rs["B13"].shape, args.dem_thr)

    BT_std = _local_std(BT, win=args.texture_win)

    cls = np.zeros(BT.shape, dtype=np.uint8)
    valid = np.isfinite(BT)

    # 高云
    high = valid & (BT < args.thr_high)
    cb = high & (BT < args.thr_dc)
    cls[cb] = 5
    cir = high & ~cb & (split >= args.cir_split)
    cls[cir] = 6
    dense = high & ~cb & ~cir
    cls[dense] = 7

    # 中云
    mid = valid & (BT >= args.thr_high) & (BT < args.thr_mid)
    cls[mid] = 4

    # 低云
    low_candidate = valid & (BT >= args.thr_mid) & (BT <= args.thr_low_max)
    if daytime:
        low = low_candidate & (normREF > args.ref_thr)
        low = low & ~snow
        if dem_high is not None:
            low = low & ~dem_high
        st = low & (BT_std < args.texture_sc)
        sc_cld = low & (BT_std >= args.texture_sc) & (BT_std < args.texture_cu)
        cu = low & (BT_std >= args.texture_cu)
        cls[st] = 1; cls[sc_cld] = 2; cls[cu] = 3
    else:
        low = low_candidate & (WB4 > args.thr_bt47_night)
        if low.any():
            cu = low & (BT_std >= args.texture_cu)
            rest = low & ~cu
            st = rest & (np.abs(split) < 1.5)
            sc_cld = rest & ~st
            cls[st] = 1; cls[sc_cld] = 2; cls[cu] = 3

    return cls, daytime, LON, LAT, BT


def main():
    import himawari_s3_cloud_map as base  # 复用其下载/发现逻辑

    ap = argparse.ArgumentParser(description="Himawari 源数据 低/中/高云分类（参考 JMA HCAI）")
    ap.add_argument("--sat", default="H09", choices=["H08", "H09"])
    ap.add_argument("--time", default="202609110600",
                    help="UTC 时次 YYYYMMDDHHMM（--latest 存在时忽略）")
    ap.add_argument("--latest", action="store_true",
                    help="自动探测最近一个已有数据的 10 分钟时次")
    ap.add_argument("--max-back-hours", type=float, default=8.0,
                    help="--latest 回退搜索范围（小时）")
    ap.add_argument("--bbox", default="70,3,140,55", help="minLon,minLat,maxLon,maxLat")
    ap.add_argument("--out", default="himawari_cloud_type.png")
    ap.add_argument("--workdir", default="./himawari_cache")

    # HCAI 模式：加载 B08(6.2µm) 水汽通道以增强高云/深对流判别
    ap.add_argument("--hcai-mode", action="store_true",
                    help="完整 HCAI 模式（加装 B08/B10 水汽通道，更准但下载更多）")

    # ---- 温度阈值（参考 HCAI / CMP 标准） ----
    ap.add_argument("--thr-high", type=float, default=235.0, help="高云亮温阈值 K")
    ap.add_argument("--thr-mid", type=float, default=262.0, help="中云亮温阈值 K")
    ap.add_argument("--thr-low-max", type=float, default=296.0,
                    help="低云云顶亮温上限 K")
    ap.add_argument("--thr-dc", type=float, default=228.0,
                    help="深对流（Cb）云顶亮温上限 K（HCAI 标准约 228-230K）")
    # 分裂窗阈值（参考 JMA CCI 技术报告表 3 & HCAI 卷云判据）
    ap.add_argument("--cir-split", type=float, default=1.5,
                    help="卷云 B13-B15 分裂窗下限 K（HCAI: ≥1.5K 半透明冰晶；<1.5K 厚冰云）")
    # 白天低云
    ap.add_argument("--ref-thr", type=float, default=40.0,
                    help="白天低云可见光反照率阈值（B02，0-100 百分比）")
    ap.add_argument("--thr-bt47-night", type=float, default=2.0,
                    help="夜间低云 3.9µm-11µm 亮温差阈值 K")
    # 积雪/高程
    ap.add_argument("--thr-snow", type=float, default=0.35,
                    help="NDSI 阈值，高于此判为雪")
    ap.add_argument("--dem", default=None, help="高程 GeoTIFF 路径（可选）")
    ap.add_argument("--dem-thr", type=float, default=2600.0,
                    help="高程掩膜阈值（米）")
    # 纹理窗口（参照 HCAI 邻域分析）
    ap.add_argument("--texture-win", type=int, default=5,
                    help="BT13 纹理标准差滑动窗口（像元）")
    # 积云/层积云/层云的纹理标准差阈值
    ap.add_argument("--texture-cu", type=float, default=4.0,
                    help="BT13 局地标准差 > 此值为积云（Cu，起伏大）")
    ap.add_argument("--texture-sc", type=float, default=2.0,
                    help="BT13 局地标准差 > 此值且 < --texture-cu 为层积云（Sc）")
    # 输出参数
    ap.add_argument("--res", type=float, default=0.02,
                    help="重投影网格分辨率（度）")
    ap.add_argument("--dpi", type=int, default=200, help="输出 PNG 分辨率")
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

    # ---- 数据加载 ----
    bands = ["B13", "B15", "B07"]  # B07=3.9µm 微物理通道（夜间低云判据必需）
    bands.append("B02")  # 可见光
    bands.append("B05")  # 1.6µm 积雪剔除

    if args.hcai_mode:
        # HCAI 完整模式：加装 B08(6.2µm) 水汽通道，提升深对流/高云顶鉴别
        bands.append("B08")
        # B10(7.3µm) 更耗资源，不作为默认；用户如需可在 bands 中追加

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
    scn.load(["B02", "B05"], calibration=["reflectance"])
    if args.hcai_mode and "B08" in bands:
        scn.load(["B08"], calibration=["brightness_temperature"])

    # ---- 重投影 ----
    from pyresample.geometry import AreaDefinition
    crop = scn.crop(ll_bbox=bbox)
    res = args.res
    target = AreaDefinition("chn", "chn", "chn",
                            projection={"proj": "longlat", "datum": "WGS84", "ellps": "WGS84"},
                            width=int((lon1 - lon0) / res),
                            height=int((lat1 - lat0) / res),
                            area_extent=(lon0, lat0, lon1, lat1))
    rs = crop.resample(target, resampler="nearest")

    BT = rs["B13"].values.astype("float64")
    BT15 = rs["B15"].values.astype("float64")
    split = BT - BT15         # B13-B15 分裂窗差异（正=薄冰，负或零=厚云）
    BT4 = rs["B07"].values.astype("float64")
    WB4 = BT4 - BT            # 3.9µm-11µm 微物理差（水云正差）

    # 可选水汽通道
    B08 = rs["B08"].values.astype("float64") if (args.hcai_mode and "B08" in rs) else None
    BTD_WV = (B08 - BT) if B08 is not None else None  # 水汽-红外差，≤0 为对流层顶附近

    tstr = str(rs["B13"].attrs.get("start_time", ""))[:16]

    # ---- 太阳几何与白天判定 ----
    REF = rs["B02"].values.astype("float64")
    REF5 = rs["B05"].values.astype("float64")
    hh, ww = REF.shape[0], REF.shape[1]
    _lons = lon0 + (np.arange(ww) + 0.5) * (lon1 - lon0) / ww
    _lats = lat0 + (np.arange(hh) + 0.5) * (lat1 - lat0) / hh
    LON, LAT = np.meshgrid(_lons, _lats)
    cosZ = _cos_solar_zenith(args.time, LON, LAT)
    normREF = REF / np.clip(cosZ, 0.15, 1.0)
    daytime = bool(np.nanmedian(cosZ) > 0.25) if cosZ.size else False

    # 积雪剔除（NDSI）
    eps = 1e-6
    snow = (REF - REF5) / (REF + REF5 + eps) > args.thr_snow

    # 可选 DEM 高程掩膜
    dem_high = None
    if args.dem:
        dem_high = _dem_high_region(args.dem, lon0, lat0, lon1, lat1,
                                    rs["B13"].shape, args.dem_thr)

    # ---- 局地纹理（参考 HCAI 邻域分析区分 Cu/Sc/St） ----
    BT_std = _local_std(BT, win=args.texture_win)

    # ---- 分类 ----
    cls, daytime, LON, LAT, BT = classify_scene(rs, args, tstr)
    valid = np.isfinite(BT)

    # 统计报告
    names = ["晴空", "层云/雾", "层积云", "积云", "中云", "深对流", "卷云", "层状高云"]
    for i, lbl in enumerate(names):
        n = int((cls == i).sum())
        frac = n / (valid.sum() or 1) * 100
        if frac > 0.5:
            print(f"  {lbl}: {frac:.1f}%")

    # ---- 着色（参考 HCAI 云类型配色体系） ----
    color = np.array([
        [0.12, 0.24, 0.50],   # 0 晴空
        [0.55, 0.75, 0.78],   # 1 层云/雾  St/Fg  淡蓝灰
        [0.35, 0.65, 0.55],   # 2 层积云 Sc     蓝绿
        [0.13, 0.72, 0.35],   # 3 积云 Cu       鲜绿
        [0.98, 0.84, 0.05],   # 4 中云 CM       黄
        [1.00, 0.20, 0.20],   # 5 深对流 Cb     红
        [0.75, 0.52, 0.99],   # 6 卷云 CH       紫
        [0.56, 0.81, 0.91],   # 7 层状高云     浅蓝
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
        print("[提示] cartopy 不可用:", e)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=args.dpi)
        ax.imshow(rgb, origin="upper")

    import matplotlib.patches as mpatches
    handles = [mpatches.Patch(color=color[i], label=lbl)
               for i, lbl in enumerate(names)]
    ax.legend(handles=handles, loc="lower left", fontsize=8.5, frameon=True, ncol=2,
              bbox_to_anchor=(0.01, 0.01))

    mode_tag = "HCAI" if args.hcai_mode else "标准"
    ax.set_title(
        f"{args.sat} Himawari 云分类（参考 JMA HCAI/{mode_tag}）  {tstr} UTC\n"
        f"深对流 BT13<{args.thr_dc:.0f}K | 卷云分裂窗≥{args.cir_split:.1f}K | 低云纹理分 Cu/Sc/St")
    fig.tight_layout()
    fig.savefig(args.out, bbox_inches="tight")
    print("已保存：", os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())