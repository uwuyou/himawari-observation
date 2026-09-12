#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火烧云定量预测（基于 Himawari 云分类 + 几何光学模型）

参考：
  《火烧云定量预报速成（长三角适用）》（长三角火烧云爱好者编著）
  核心公式：霞光深入距离 L = √(2·R·h)   (R=地球半径, h=云底高度)
  火烧云三角时空分布：远边界 y=(x-vt)²/(2R)  近边界 y=(x-vt-d)²/(2R)+h

用法：
  python3 fire_cloud_predict.py --lat 31.23 --lon 121.47           # 上海
  python3 fire_cloud_predict.py --lat 40.00 --lon 116.33 --name 北京
  python3 fire_cloud_predict.py --latest --json out.json           # 输出 JSON

输出 JSON 格式（供网站使用）：
  {"utc":"202609111900","level":"高","duration":"6-12分钟",
   "start_rel":"日落后约8分钟","color_est":"橙红至紫红",
   "cloud_types":["高云","中云"],"direction":"西南偏西(253°)",
   "confidence":0.78,"detail":"..."}
"""

import argparse
import base64
import json
import math
import os
import sys
import datetime as dt

import numpy as np

R_EARTH = 6371.0  # 地球半径 km

# 云类型 -> 典型云底高度 (km)   （参考火烧云文档 + 气象常识）
# 对于层状云火烧云，使用云底高度；对于对流云，使用云顶高度（亮温反演）
CLOUD_BASE_HEIGHTS = {
    0: None,    # 晴空 → 无云
    1: 0.4,     # 层云/雾 St/Fg  → 太低，几乎无法形成高质量火烧云
    2: 1.5,     # 层积云 Sc      → 中等云底，傍晚可能有短时效果
    3: 2.0,     # 积云 Cu        → 夏季对流云火烧云
    4: 5.0,     # 中云 CM        → 最常见的高积云/高层云火烧云
    5: 14.0,    # 深对流 Cb      → 用云顶高度，壮观的日落火烧云
    6: 9.0,     # 卷云 CH        → 高云，霞光持久
    7: 10.0,    # 层状高云       → 厚冰云，也可能有不错效果
}

CLOUD_NAMES = {
    0: "晴空", 1: "层云/雾", 2: "层积云", 3: "积云",
    4: "中云", 5: "深对流", 6: "卷云", 7: "层状高云"
}

# 云类型的"火烧云质量评分"（经验值 0~10）
CLOUD_SCORE = {
    0: 0, 1: 1, 2: 5, 3: 7,   # 低云
    4: 8,                       # 中云（最常见的火烧云）
    5: 9,                       # 深对流（壮观但短）
    6: 7,                       # 卷云（持久但颜色偏淡）
    7: 6,                       # 层状高云（遮光可能大）
}


def _sunset_time_and_azimuth(lat, lon, date=None):
    """
    计算给定日期/位置的标准日落时间（UTC）和日落方位角（度，正北顺时针）。
    采用 NOAA 简化算法（忽略大气折射和太阳视直径）。
    """
    if date is None:
        date = dt.datetime.utcnow().strftime("%Y-%m-%d")
    y, m, d = map(int, date.split("-"))

    # 儒略日
    if m <= 2:
        y -= 1; m += 12
    A = y // 100
    B = 2 - A + A // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5

    # 太阳平黄经和近点角
    n = jd - 2451545.0
    L = (280.466 + 0.9856474 * n) % 360
    g = (357.528 + 0.9856003 * n) % 360
    lam = (L + 1.915 * math.sin(math.radians(g)) + 0.020 * math.sin(math.radians(2 * g))) % 360

    # 太阳赤纬
    obl = 23.439 - 0.0000004 * n
    sin_dec = math.sin(math.radians(obl)) * math.sin(math.radians(lam))
    dec = math.degrees(math.asin(sin_dec))

    # 时角: cos(h) = -tan(lat)*tan(dec)
    lat_r = math.radians(lat)
    dec_r = math.radians(dec)
    cos_h = -math.tan(lat_r) * math.tan(dec_r)
    cos_h = max(-1, min(1, cos_h))  # 极昼/极夜处理
    h_angle = math.degrees(math.acos(cos_h))

    # 日落UTC时间
    jd_noon = jd - lon / 360
    t_noon = jd_noon - 2451545.0
    eq_time = (L - lam) * 4  # 分钟
    sunset_utc = 12 + h_angle / 15 + eq_time / 60 - lon / 15

    # 日落方位角：E = -cos(dec)*sin(H), N = sin(dec)*cos(lat) - cos(dec)*sin(lat)*cos(H)
    h_r = math.radians(h_angle)
    dec_r = math.radians(dec)
    lat_r = math.radians(lat)
    E = -math.cos(dec_r) * math.sin(h_r)
    N = math.sin(dec_r) * math.cos(lat_r) - math.cos(dec_r) * math.sin(lat_r) * math.cos(h_r)
    az = (math.degrees(math.atan2(E, N))) % 360

    # 分/秒处理
    hh = int(sunset_utc)
    mm = int((sunset_utc - hh) * 60)
    ss = int(((sunset_utc - hh) * 60 - mm) * 60)
    ymd = date.replace("-", "")
    utc_str = f"{ymd}{hh:02d}{mm:02d}"

    return utc_str, az


def _sunrise_time_and_azimuth(lat, lon, date=None):
    """计算给定日期/位置的标准日出时间（UTC）和日出方位角（度，正北顺时针）。
    与日落对称，仅时角取上午（负号）。日出可能落在 UTC 前一日，需跨日回退。"""
    if date is None:
        date = dt.datetime.utcnow().strftime("%Y-%m-%d")
    y, m, d = map(int, date.split("-"))

    if m <= 2:
        y -= 1; m += 12
    A = y // 100
    B = 2 - A + A // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5

    n = jd - 2451545.0
    L = (280.466 + 0.9856474 * n) % 360
    g = (357.528 + 0.9856003 * n) % 360
    lam = (L + 1.915 * math.sin(math.radians(g)) + 0.020 * math.sin(math.radians(2 * g))) % 360

    obl = 23.439 - 0.0000004 * n
    dec = math.degrees(math.asin(math.sin(math.radians(obl)) * math.sin(math.radians(lam))))

    lat_r = math.radians(lat)
    dec_r = math.radians(dec)
    cos_h = -math.tan(lat_r) * math.tan(dec_r)
    cos_h = max(-1, min(1, cos_h))
    h_angle = math.degrees(math.acos(cos_h))

    # 日出时间：上午时角为负
    jd_noon = jd - lon / 360
    t_noon = jd_noon - 2451545.0
    eq_time = (L - lam) * 4
    sunrise_utc = 12 - h_angle / 15 + eq_time / 60 - lon / 15

    # 日出方位角（东侧）
    h_r = math.radians(h_angle)
    E = -math.cos(dec_r) * math.sin(-h_r)
    N = math.sin(dec_r) * math.cos(lat_r) - math.cos(dec_r) * math.sin(lat_r) * math.cos(h_r)
    az = (math.degrees(math.atan2(E, N))) % 360

    # UTC 时间归一化，处理负值（中国清晨日出多落在 UTC 前一日）
    total_min = int(round(sunrise_utc * 60))
    date_obj = dt.datetime.strptime(date, "%Y-%m-%d")
    while total_min < 0:
        total_min += 1440
        date_obj -= dt.timedelta(days=1)
    hh = (total_min // 60) % 24
    mm = total_min % 60
    ymd = date_obj.strftime("%Y%m%d")
    utc_str = f"{ymd}{hh:02d}{mm:02d}"

    return utc_str, az


def _sunset_line_speed(lat, date=None):
    """计算日落线速度 (km/min)。参考火烧云文档附录的查表插值。"""
    if date is None:
        date = dt.datetime.utcnow().strftime("%Y-%m-%d")
    # 查表（纬度 0~60, 间隔 5°, 月份 1~12）
    # 从文档附录提取的数据表
    table = {
        (0, 1): 21.6, (0, 2): 22.6, (0, 3): 23.2, (0, 4): 22.9,
        (0, 5): 21.9, (0, 6): 21.3, (0, 7): 21.6, (0, 8): 22.5,
        (0, 9): 23.1, (0, 10): 22.9, (0, 11): 22.0, (0, 12): 21.3,
        (5, 1): 21.5, (5, 2): 22.5, (5, 3): 23.1, (5, 4): 22.8,
        (5, 5): 21.8, (5, 6): 21.2, (5, 7): 21.4, (5, 8): 22.4,
        (5, 9): 23.1, (5, 10): 22.8, (5, 11): 21.9, (5, 12): 21.2,
        (10, 1): 21.2, (10, 2): 22.3, (10, 3): 22.8, (10, 4): 22.5,
        (10, 5): 21.6, (10, 6): 20.9, (10, 7): 21.2, (10, 8): 22.1,
        (10, 9): 22.8, (10, 10): 22.6, (10, 11): 21.6, (10, 12): 20.9,
        (15, 1): 20.8, (15, 2): 21.8, (15, 3): 22.4, (15, 4): 22.1,
        (15, 5): 21.1, (15, 6): 20.4, (15, 7): 20.7, (15, 8): 21.6,
        (15, 9): 22.3, (15, 10): 22.1, (15, 11): 21.2, (15, 12): 20.5,
        (20, 1): 20.1, (20, 2): 21.2, (20, 3): 21.8, (20, 4): 21.4,
        (20, 5): 20.4, (20, 6): 19.7, (20, 7): 20.0, (20, 8): 21.0,
        (20, 9): 21.7, (20, 10): 21.5, (20, 11): 20.5, (20, 12): 19.8,
        (25, 1): 19.3, (25, 2): 20.4, (25, 3): 21.0, (25, 4): 20.6,
        (25, 5): 19.6, (25, 6): 18.9, (25, 7): 19.1, (25, 8): 20.2,
        (25, 9): 21.0, (25, 10): 20.7, (25, 11): 19.7, (25, 12): 18.9,
        (30, 1): 18.3, (30, 2): 19.4, (30, 3): 20.1, (30, 4): 19.7,
        (30, 5): 18.6, (30, 6): 17.8, (30, 7): 18.1, (30, 8): 19.2,
        (30, 9): 20.0, (30, 10): 19.8, (30, 11): 18.7, (30, 12): 17.9,
        (35, 1): 17.1, (35, 2): 18.3, (35, 3): 19.0, (35, 4): 18.6,
        (35, 5): 17.4, (35, 6): 16.6, (35, 7): 16.9, (35, 8): 18.1,
        (35, 9): 18.9, (35, 10): 18.7, (35, 11): 17.6, (35, 12): 16.7,
        (40, 1): 15.7, (40, 2): 17.1, (40, 3): 17.8, (40, 4): 17.3,
        (40, 5): 16.1, (40, 6): 15.1, (40, 7): 15.5, (40, 8): 16.8,
        (40, 9): 17.7, (40, 10): 17.4, (40, 11): 16.2, (40, 12): 15.3,
        (45, 1): 14.2, (45, 2): 15.6, (45, 3): 16.4, (45, 4): 15.9,
        (45, 5): 14.5, (45, 6): 13.5, (45, 7): 13.9, (45, 8): 15.3,
        (45, 9): 16.3, (45, 10): 16.1, (45, 11): 14.7, (45, 12): 13.7,
        (50, 1): 12.4, (50, 2): 14.1, (50, 3): 14.9, (50, 4): 14.4,
        (50, 5): 12.8, (50, 6): 11.6, (50, 7): 12.1, (50, 8): 13.7,
        (50, 9): 14.8, (50, 10): 14.5, (50, 11): 13.1, (50, 12): 11.9,
        (55, 1): 10.5, (55, 2): 12.4, (55, 3): 13.3, (55, 4): 12.7,
        (55, 5): 10.9, (55, 6): 9.5, (55, 7): 10.0, (55, 8): 11.9,
        (55, 9): 13.2, (55, 10): 12.9, (55, 11): 11.2, (55, 12): 9.8,
        (60, 1): 8.2, (60, 2): 10.5, (60, 3): 11.6, (60, 4): 10.9,
        (60, 5): 8.7, (60, 6): 6.8, (60, 7): 7.6, (60, 8): 10.0,
        (60, 9): 11.5, (60, 10): 11.1, (60, 11): 9.1, (60, 12): 7.4,
    }

    m = int(date.split("-")[1])
    lat_rounded = round(lat / 5) * 5
    lat_rounded = max(0, min(60, lat_rounded))

    if (lat_rounded, m) in table:
        return table[(lat_rounded, m)]

    # 附近纬度插值
    candidates = [(k[0], v) for k, v in table.items() if k[1] == m]
    if not candidates:
        return 20.0  # 默认值
    candidates.sort()
    i_lats = [c[0] for c in candidates]
    vals = [c[1] for c in candidates]
    return float(np.interp(lat, i_lats, vals))


def predict(lat, lon, cls, LON, LAT, obs_time_utc, name=None):
    """
    核心预测逻辑。
    
    参数:
        lat, lon: 用户位置（度）
        cls: 云分类数组 (uint8), 0=晴空 1~7=各云类
        LON, LAT: 经纬度网格
        obs_time_utc: 观测时次 UTC YYYYMMDDHHMM
        name: 位置名称
    
    返回:
        dict: 预测结果
    """
    # 1. 获取日落信息
    date_str = f"{obs_time_utc[:4]}-{obs_time_utc[4:6]}-{obs_time_utc[6:8]}"
    sunset_utc_str, sunset_az = _sunset_time_and_azimuth(lat, lon, date_str)
    sunset_line_speed = _sunset_line_speed(lat, date_str)
    sunrise_utc_str, sunrise_az = _sunrise_time_and_azimuth(lat, lon, date_str)

    # 2. 判断是日落还是日出预测
    obs_hour = int(obs_time_utc[8:10])
    obs_min = int(obs_time_utc[10:12])
    obs_mins = obs_hour * 60 + obs_min
    sunset_h = int(sunset_utc_str[8:10])
    sunset_m = int(sunset_utc_str[10:12])
    sunset_mins = sunset_h * 60 + sunset_m
    is_sunset = abs(obs_mins - sunset_mins) < 360

    # 3. 确定观察方向（日落方向 = 西侧）
    #    日落方位角附近 ±45° 范围
    az_min = (sunset_az - 45) % 360
    az_max = (sunset_az + 45) % 360

    # 4. 提取用户位置到日落方向 50~500km 范围内的云况
    pix_lat_deg = abs(LAT[0, 0] - LAT[1, 0]) if LAT.shape[0] > 1 else 0.02
    pix_lon_deg = abs(LON[0, 0] - LON[0, 1]) if LON.shape[1] > 1 else 0.02
    deg_per_km_lat = 1.0 / 111.0
    deg_per_km_lon = 1.0 / (111.0 * math.cos(math.radians(lat)))

    min_dist_km, max_dist_km = 50, 500
    min_dlat = min_dist_km * deg_per_km_lat
    max_dlat = max_dist_km * deg_per_km_lat
    min_dlon = min_dist_km * deg_per_km_lon
    max_dlon = max_dist_km * deg_per_km_lon

    # 方向向量（日落方位角 ±45° 扇形区域）
    # 计算掩膜：在日落方向且在距离范围内
    dlon = LON - lon
    dlat = LAT - lat
    dist_km = np.sqrt((dlon / deg_per_km_lon) ** 2 + (dlat / deg_per_km_lat) ** 2)

    # 方位角计算（从用户位置指向每个像素）
    az_to_pixel = (np.degrees(np.arctan2(-dlat, dlon)) + 270) % 360

    # 日落方向扇形掩膜
    if az_min <= az_max:
        in_az = (az_to_pixel >= az_min) & (az_to_pixel <= az_max)
    else:  # 跨 0°
        in_az = (az_to_pixel >= az_min) | (az_to_pixel <= az_max)

    in_range = (dist_km >= min_dist_km) & (dist_km <= max_dist_km)

    # 4.4 光线能否照射到本地云底（几何可见性）
    #     日出/日落时阳光为掠射角。观测者到云边的水平距离须 ≤ √(2·R·h)，
    #     否则阳光被地球曲率遮住、云底收不到光（参考《火烧云定量预报》4.1.1）。
    #     不同云类云底高度不同 → 最大可照亮水平距离不同；低云很快超出可照范围。
    h_pix = np.zeros(cls.shape, dtype=np.float64)
    for _ct, _h in CLOUD_BASE_HEIGHTS.items():
        if _h is not None:
            h_pix[cls == _ct] = _h
    d_max_km = np.sqrt(2.0 * R_EARTH * np.maximum(h_pix, 0.0))
    is_cloud_pix = cls > 0
    lit_or_clear = (~is_cloud_pix) | (dist_km <= d_max_km)  # 晴空保留作分母，照不到的云剔除

    roi = in_az & in_range & lit_or_clear & np.isfinite(cls)

    # 如果没有有效 ROI 区域
    if not roi.any():
        return _empty_result(lat, lon, name, sunset_utc_str, sunset_az,
                             "日落方向 50~500km 范围内无卫星数据")

    # 5. 统计日落方向云类型分布
    cls_roi = cls[roi]
    total_pixels = cls_roi.size
    cloud_pixels = (cls_roi > 0).sum()
    cloud_cover = cloud_pixels / total_pixels

    type_counts = {}
    for ct in range(1, 8):
        cnt = int((cls_roi == ct).sum())
        if cnt > 0:
            type_counts[ct] = cnt

    # 6. 计算火烧云潜力
    #    加权评分：每种云类型覆盖占比 × 质量评分
    scores = []
    heights_for_geo = []
    total_score = 0.0
    for ct, cnt in type_counts.items():
        frac = cnt / total_pixels
        s = CLOUD_SCORE.get(ct, 0) * frac
        scores.append((ct, frac, s))
        total_score += s
        h = CLOUD_BASE_HEIGHTS.get(ct)
        if h is not None and h > 1.0:  # 只考虑足够高的云
            heights_for_geo.append((h, frac))

    # 7. 几何模型：计算可能的霞光深入距离
    #    用覆盖占比最高的云类的高度
    if heights_for_geo:
        # 按覆盖比加权平均高度
        weighted_h = sum(h * w for h, w in heights_for_geo) / sum(w for _, w in heights_for_geo)
    else:
        weighted_h = 2.0  # 没有高云时保守估计

    # 霞光深入距离 L = sqrt(2 * R * h)
    L_glow = math.sqrt(2 * R_EARTH * weighted_h)

    # 火烧云持续时间（理论最大）：光线从云近边界到远边界的时间
    # t = d/v, 其中 d ≈ L_glow (km), v = 日落线速度 (km/min)
    max_duration_min = L_glow / sunset_line_speed if sunset_line_speed > 0 else 10
    max_duration_min = min(max_duration_min, 45)  # 上限 45 分钟

    # 8. 综合判定等级
    #    考虑：云覆盖、云类型质量、云高度
    cloud_cover_bonus = min(1.0, cloud_cover / 0.4)  # 40%覆盖=满分
    high_cloud_bonus = 0.0
    worst_cloud_penalty = 0.0

    for ct in type_counts:
        score = CLOUD_SCORE.get(ct, 0)
        if score >= 7:  # 积云、中云、深对流、卷云
            high_cloud_bonus += 0.3 * (type_counts[ct] / total_pixels)
        if ct == 1:  # 层云/雾惩罚
            worst_cloud_penalty += 0.5 * (type_counts[ct] / total_pixels)

    # 透明度因子：层云/雾多则不利
    st_fraction = type_counts.get(1, 0) / total_pixels if total_pixels > 0 else 0
    transparency = 1.0 - st_fraction * 0.7

    raw_score = total_score * cloud_cover_bonus * transparency + high_cloud_bonus

    # 映射到等级
    if raw_score >= 2.0:
        level = "高"
        confidence = min(0.95, 0.5 + raw_score * 0.08)
    elif raw_score >= 0.8:
        level = "中"
        confidence = min(0.7, 0.3 + raw_score * 0.2)
    elif raw_score >= 0.2:
        level = "低"
        confidence = min(0.4, 0.1 + raw_score * 0.3)
    else:
        level = "无"
        confidence = 0.05

    # 9. 输出格式化
    cloud_names_present = [CLOUD_NAMES.get(ct, "未知") for ct in sorted(type_counts)]

    # 持续时间范围
    dur_min = max(1, max_duration_min * 0.3)
    dur_max = max_duration_min
    if dur_max < 3:
        duration_str = f"{dur_min:.0f}-{dur_max:.0f}分钟"
    else:
        duration_str = f"{dur_min:.0f}-{dur_max:.0f}分钟"

    # 颜色预估
    high_types = [ct for ct in type_counts if CLOUD_SCORE.get(ct, 0) >= 7]
    low_types = [ct for ct in type_counts if CLOUD_SCORE.get(ct, 0) <= 3 and ct > 0]

    if 5 in high_types:
        color_est = "橙红至紫红（对流云强烈色彩）"
    elif 6 in type_counts or 7 in type_counts:
        color_est = "粉红至橙黄（高云晕彩）"
    elif 4 in type_counts:
        color_est = "橘色至橘红（典型中云火烧云）"
    elif 3 in type_counts:
        color_est = "金色至淡橙（积云）"
    elif 2 in type_counts:
        color_est = "浅金至淡红"
    else:
        color_est = "不确定"

    # 方向描述
    dir_names = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    dir_idx = round(sunset_az / 45) % 8
    dir_str = f"{dir_names[dir_idx]}偏{dir_names[(dir_idx + 1) % 8]}"
    if dir_idx % 2 == 0:
        dir_str = dir_names[dir_idx]

    # 生成详细描述
    details = []
    details.append(f"位置: {name or f'{lat:.2f}°N, {lon:.2f}°E'}")
    details.append(f"日落时间 (UTC): {sunset_utc_str[8:10]}:{sunset_utc_str[10:12]}")
    bjt_h = (int(sunset_utc_str[8:10]) + 8) % 24
    details.append(f"北京时间: {bjt_h:02d}:{sunset_utc_str[10:12]}")
    details.append(f"日落方位: {sunset_az:.0f}° ({dir_str})")
    details.append(f"日落线速度: {sunset_line_speed:.1f} km/min")
    if heights_for_geo:
        details.append(f"等效云底高度: {weighted_h:.1f} km")
        details.append(f"理论霞光深入: {L_glow:.0f} km")
    details.append(f"日落方向云量: {cloud_cover*100:.0f}%")
    details.append(f"云类分布: {', '.join(f'{k}({CLOUD_NAMES[k]})' for k in sorted(type_counts, key=lambda x: type_counts[x], reverse=True))}")
    details.append(f"火烧云潜力评分: {raw_score:.3f}")

    # 朝霞（日出方位）摘要
    s_dir_names = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    s_idx = round(sunrise_az / 45) % 8
    if s_idx % 2 == 0:
        s_dir = s_dir_names[s_idx]
    else:
        s_dir = f"{s_dir_names[s_idx]}偏{s_dir_names[(s_idx + 1) % 8]}"

    result = {
        "utc": obs_time_utc,
        "sunset_utc": sunset_utc_str,
        "sunrise_utc": sunrise_utc_str,
        "sunrise_az": round(sunrise_az, 0),
        "sunrise_direction": f"{s_dir}({sunrise_az:.0f}°)",
        "level": level,
        "duration": duration_str,
        "start_rel": f"日落后约{max(1, int(dur_min * 0.3))}分钟",
        "color_est": color_est,
        "cloud_types": cloud_names_present,
        "direction": f"{dir_str}({sunset_az:.0f}°)",
        "cloud_cover_pct": round(cloud_cover * 100, 0),
        "confidence": round(confidence, 2),
        "detail": "\n".join(details),
    }
    return result


def _empty_result(lat, lon, name, sunset_utc_str, sunset_az, reason):
    """无数据时的空结果。"""
    result = {
        "utc": "N/A",
        "sunset_utc": sunset_utc_str,
        "level": "N/A",
        "duration": "N/A",
        "start_rel": "N/A",
        "color_est": "N/A",
        "cloud_types": ["数据不足"],
        "direction": f"日落方位({sunset_az:.0f}°)",
        "cloud_cover_pct": 0,
        "confidence": 0,
        "detail": f"位置: {name or f'{lat:.2f}°N, {lon:.2f}°E'}\n原因: {reason}",
    }
    return result


def _downsample_cloud(cls, factor=5):
    """将云分类数组按因子降采样（取众数），加速从 0.02°→0.1°。"""
    from scipy.stats import mode as sp_mode
    h, w = cls.shape
    h_new, w_new = h // factor, w // factor
    crop = cls[:h_new * factor, :w_new * factor]
    blocks = crop.reshape(h_new, factor, w_new, factor)
    blocks = blocks.transpose(0, 2, 1, 3).reshape(h_new, w_new, -1)
    result, _ = sp_mode(blocks, axis=2, keepdims=True)
    return result[:, :, 0].astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description="火烧云定量预测（基于 Himawari 云分类）")
    ap.add_argument("--lat", type=float, default=31.23, help="纬度（度，北正）")
    ap.add_argument("--lon", type=float, default=121.47, help="经度（度，东正）")
    ap.add_argument("--name", default=None, help="位置名称，如 上海")
    ap.add_argument("--time", default=None, help="UTC 时次 YYYYMMDDHHMM")
    ap.add_argument("--latest", action="store_true", help="自动探测最新时次")
    ap.add_argument("--max-back-hours", type=float, default=12.0, help="最新时次搜索范围")
    ap.add_argument("--sat", default="H09", choices=["H08", "H09"])
    ap.add_argument("--json", default=None, help="输出 JSON 文件路径（供网站使用）")
    ap.add_argument("--out", default=None, help="输出文本报告路径")
    ap.add_argument("--export-cloud-json", default=None,
                    help="导出降采样后的云分类数据为 JSON（供网站客户端查询任意城市）")
    args = ap.parse_args()

    # 1. 导入分类模块
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import himawari_cloud_type as ct_mod
    import himawari_s3_cloud_map as base

    bucket = base.BUCKETS[args.sat]
    if args.latest or not args.time:
        args.time = base.find_latest(bucket, max_back_hours=args.max_back_hours)
        if not args.time:
            print("[错误] 未找到最新数据时次")
            return 1
        print(f"[--latest] 时次: {args.time} UTC", file=sys.stderr)

    # 2. 下载数据
    bands = ["B13", "B15", "B07", "B02", "B05"]
    keys = base.discover_keys(bucket, args.time)
    if not keys:
        print(f"[错误] 时次 {args.time} 无数据")
        return 1
    sel = base.select_band_keys(keys, bands)
    dats = base.ensure_local(bucket, sel, args.workdir if hasattr(args, 'workdir') else "./himawari_cache")

    # 3. 加载 scene
    from satpy import Scene
    scn = Scene(filenames=dats, reader="ahi_hsd")
    scn.load(["B13", "B15", "B07"], calibration=["brightness_temperature"])
    scn.load(["B02", "B05"], calibration=["reflectance"])

    bbox = (70, 3, 140, 55)
    crop = scn.crop(ll_bbox=bbox)
    from pyresample.geometry import AreaDefinition
    res = 0.02
    target = AreaDefinition(
        "chn", "chn", "chn",
        projection={"proj": "longlat", "datum": "WGS84", "ellps": "WGS84"},
        width=int((bbox[2] - bbox[0]) / res),
        height=int((bbox[3] - bbox[1]) / res),
        area_extent=bbox)
    rs = crop.resample(target, resampler="nearest")

    # 4. 获取云分类
    # 构造模拟 args 对象
    class FakeArgs:
        bbox = "70,3,140,55"
        thr_high = 235.0
        thr_mid = 262.0
        thr_low_max = 296.0
        thr_dc = 228.0
        cir_split = 1.5
        ref_thr = 40.0
        thr_bt47_night = 2.0
        thr_snow = 0.35
        dem = None
        dem_thr = 2600.0
        texture_win = 5
        texture_cu = 4.0
        texture_sc = 2.0
    fake_args = FakeArgs()
    tstr = str(rs["B13"].attrs.get("start_time", ""))[:16]

    cls, daytime, LON, LAT, BT = ct_mod.classify_scene(rs, fake_args, tstr)

    # 4b. 导出降采样后的云分类数据（供网站客户端任意城市查询）
    if args.export_cloud_json:
        factor = 5  # 0.02° → 0.1°
        cls_low = _downsample_cloud(cls, factor)
        h_low, w_low = cls_low.shape
        # 经纬度网格
        n_lons = np.linspace(bbox[0] + 0.1 / 2, bbox[2] - 0.1 / 2, w_low) if w_low > 0 else []
        n_lats = np.linspace(bbox[3] - 0.1 / 2, bbox[1] + 0.1 / 2, h_low) if h_low > 0 else []
        cloud_str = "".join(str(int(v)) for v in cls_low.ravel())
        cloud_data = {
            "bbox": list(bbox),
            "res": 0.1,
            "utc": args.time,
            "nx": w_low,
            "ny": h_low,
            "lons": [round(float(v), 4) for v in n_lons],
            "lats": [round(float(v), 4) for v in n_lats],
            "data": cloud_str,
        }
        os.makedirs(os.path.dirname(args.export_cloud_json) if os.path.dirname(args.export_cloud_json) else ".", exist_ok=True)
        with open(args.export_cloud_json, "w", encoding="utf-8") as f:
            json.dump(cloud_data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"云数据已导出: {os.path.abspath(args.export_cloud_json)} ({len(cloud_str)//1024}KB)", file=sys.stderr)

    # 5. 运行预测
    result = predict(args.lat, args.lon, cls, LON, LAT, args.time, name=args.name)
    result["daytime"] = daytime

    # 6. 输出
    detail = result["detail"]
    print(detail, file=sys.stderr)
    print()

    # 简洁输出
    print(f"===== 火烧云预测 ({args.name or f'{args.lat:.1f}°N,{args.lon:.1f}°E'}) =====")
    print(f"观测时次: {args.time} UTC")
    print(f"日落时刻: {result['sunset_utc'][8:10]}:{result['sunset_utc'][10:12]} UTC")
    print(f"日落方位: {result['direction']}")
    print(f"火烧云潜力: {result['level']}  (信度: {result['confidence']:.0%})")
    print(f"预计持续时间: {result['duration']}")
    print(f"出现时间: {result['start_rel']}")
    print(f"颜色预估: {result['color_est']}")
    print(f"云类: {', '.join(result['cloud_types'])}")

    # JSON 输出
    if args.json:
        os.makedirs(os.path.dirname(args.json) if os.path.dirname(args.json) else ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已保存: {os.path.abspath(args.json)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(detail)
        print(f"报告已保存: {os.path.abspath(args.out)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())