#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chart_convert.py —— 自制谱 .mmg 一键转编脚本

支持的两种 .mmg（按文件头自动识别）：
  1) Malody 谱面包：ZIP，内含 *.mc(JSON) + *.mp3 + *.jpg/png
  2) 不休的音符系二进制谱（GameForKeyEditor/PatternDIY 导出）
     —— 布局依据反编译 DataExport.cs / Summoner.Format.Mmg 的权威实现：

     头:  i32  100000            魔数
          f32  BPM
          i32  nameLen            配置串字节数
          utf8 "曲名.mp3,G,cellCount,timeOffset"
              G=每拍细分(1,2,3,4,6,8,9,12,16) cellCount=总格数
              timeOffset=音频偏移ms（键时间戳本身是含偏移的绝对秒）
     记录: 24 字节定长，按编辑器键表插入顺序排列
          i32  track    (0..3)
          f32  time     绝对秒
          i32  type     1=SINGLE单点  2=MULTI_KEY长条/滑键节点
          i32  group    链ID（SINGLE 固定 -1）
          i32  index    链内序号 0..n-1（SINGLE 固定 -1）
          i32  eventId  事件号（通常为 0，原样保留）
     链语义：同轨链(所有节点 track 相同)= Hold 头尾两点；
             换轨链 = Slide 滑键，按 index 顺序逐点变轨。
             同拍恰好两点换轨 = PatternDIY 两轨同时占位（无独立方向字节）。
             几何仍用 index 序 track[0]→track[1] 写成瞬时 dir（不占时值）；
             禁止补 1/4 拍 seg，否则会接到下一键，钩子被拉长。
             三节点钩子 (停 A)→同拍折 B = 短斜 seg，占满停顿。
             划完再停 (t,A),(t,B),(t2,B) = 一条两段 seg：1/96 横移 x:±61，
             再按剩余时值停在同一相对 x（不用 dir，也不写 lane_width 的 x:64）。
             往返划钩 (t,A),(t,B),(t2,B),(t2,A) = 叠键方向 A→B，相对方向瞬时
             划钩：第一段 1/96 x:±61，第二段回程 x:∓1（不写 dir 叠 Hold）。

     BNDQ JSON 导出规则（DataExportToJson / configs\\toJsonTemplete*）：
          tick = ceil(BPM/60 * 48 * time)   （48 tick/拍）
          SINGLE: dur=0 isEnd=0 attr=0 toTrack=0
          MULTI : 只输出"存在下一节点"的节点；链首 attr=3，其余 attr=4；
                  下一节点是链尾则 isEnd=1；同轨 dur=tick差，同时刻跨轨 dur=0；
                  toTrack=下一节点track+3；轨 0..3 → tracks 3/4/5/6。

中间表示(IR)：Chart/Note(tap|hold|slide, 秒, track, [segs])
输出格式：malody(.mc/.mmg/.mcz) | mmg(二进制, 官方布局) | osu(osu!mania) | json | bndq
仅依赖标准库。

补充：.mcz / Malody 游玩模式 (mode)
  .mcz = ZIP + 内嵌 *.mc（资产在同子目录，如 0/xxx.mc, 0/bg.png, 0/song-bg.ogg）。
  mode 0 Key：note 用 column/endbeat。
  mode 7 Slide：note 用连续横坐标 x(0~255)+宽度 w 的滑道；
    - 无 type/seg          -> Tap 单击
    - type=4               -> Wipe 划键（判定等同单击，按 Tap 转出）
    - 带 seg:[关键帧]      -> Slide 滑条：帧 beat 为相对主 note 的拍数偏移，
      帧 x 为相对主 note x 的偏移（缺省=回到主 x）；帧按中心 x+w/2
      量化到轨道：全帧同轨 -> Hold 链，跨轨 -> Slide 链
    - type=1 且带 sound    -> 乐曲声明条目（音频偏移等），非可玩音符，跳过
  x 量化：轨道中心=(lane+0.5)*256/lanes，取音符中心最近轨道；默认 4K。

用法：
  python chart_convert.py 输入.mmg                    # auto 转出其余格式
  python chart_convert.py 输入.mmg --to bndq
  python chart_convert.py 输入.mmg -o 输出目录 --grid 48
"""

import argparse
import json
import math
import os
import struct
import sys
import zipfile
from dataclasses import dataclass, field

TAP = "tap"
HOLD = "hold"
SLIDE = "slide"

MMG_MAGIC = 100000
MMG_RECORD_SIZE = 24
BNDQ_TICKS_PER_BEAT = 48

# Malody Slide 横坐标布局（x 为音符中心）。与音游工具箱/rmstZ 的判定表一致：
# 轨道 i 的中心 = offset + i*lane_width；4K 键宽统一 After Dawn w:61。
SLIDE_LAYOUT = {
    4: dict(lane_width=64, offset=31, base_width=61),
    5: dict(lane_width=51, offset=25, base_width=50),
    6: dict(lane_width=43, offset=21, base_width=40),
    7: dict(lane_width=36, offset=18, base_width=34),
    8: dict(lane_width=32, offset=16, base_width=30),
    9: dict(lane_width=28, offset=14, base_width=26),
    10: dict(lane_width=25, offset=13, base_width=25),
}


# ------------------------------------------------------------------ IR

@dataclass
class Note:
    kind: str                 # TAP | HOLD | SLIDE
    t: float                  # 起始绝对秒
    lane: int                 # 0 基轨道
    t_end: float = 0.0        # HOLD 结束秒
    segs: list = None         # SLIDE/HOLD 链节点: [(t, lane), ...] 按 index 升序
    gid: int = -1             # 二进制 mmg 链 ID（Tap=-1）
    evt: int = 0              # eventId 原样保留
    order: int = 0            # 源文件中的首个记录位置（保序回写）
    row_orders: list = None   # 每个节点行在源文件中的记录位置（逐行保序）
    row_evts: list = None     # 每个节点行的 eventId（逐行保真）
    slide_x: float = None     # Malody Slide 主节点左边缘坐标（0~256）
    slide_w: float = None     # Malody Slide 宽度
    slide_segs: list = None   # [(相对秒, 相对 x)]，保留原生 Slide 折线关键帧
    source_beat: list = None  # 原始主节点 beat，Slide 直出时避免量化损失
    source_seg_beats: list = None  # 原始相对关键帧 beat
    arrow_dir: int = None     # 仅 MC 读入的 dir；MMG 无此字段


@dataclass
class Chart:
    title: str = "Untitled"
    artist: str = ""
    creator: str = ""
    bpm: float = 120.0
    offset_sec: float = 0.0
    lanes: int = 4
    notes: list = field(default_factory=list)
    audio_bytes: bytes = None
    audio_name: str = None
    image_bytes: bytes = None
    image_name: str = None
    src_format: str = ""
    mc_version: str = ""      # Malody 谱面难度标签（meta.version）
    slide_mode: bool = False  # 源为 Malody mode 7 Slide
    mc_dir: str = ""          # mc 在包内的子目录（配资产用）
    audio_ext: str = ""       # 源音频真实扩展名（.mp3/.ogg/.wav）
    raw_cfg: str = ""         # 二进制 mmg 的完整配置串（回写保真）
    grid_g: int = 8           # 配置串 G
    cells: int = 0            # 配置串 cellCount
    times_relative: bool = False  # True=键时间从谱面 beat 0 起算；False=MMG 含音频偏移

    def sorted(self):
        return sorted(self.notes, key=lambda n: (n.t, n.lane))


# ------------------------------------------------------------------ Malody

def _beat_to_sec(beat, bpm):
    b, n, d = beat[0], beat[1], (beat[2] or 1)
    return (b + n / d) * 60.0 / bpm


SLIDE_BEAT_GRID = 768          # 音游工具箱 / Malody Slide 内部 tick
SLIDE_TIME_BEAT = [0, 0, 1000000]  # 音游工具箱空谱 / 原版.mc
SLIDE_DIR_WIDTH = 61           # After Dawn：单击 / Hold / 划键 / 折线一律 w:61
SLIDE_DIR_RIGHT = 2
SLIDE_DIR_LEFT = 8
SLIDE_FLICK_HOLD_BEAT = [0, 1, 96]
SLIDE_FLICK_HOLD_DX = 61       # 瞬时横移用键宽，不用 lane_width 64
SLIDE_FLICK_RETURN_DX = 1      # 往返回程贴回链头，右出 -1、左出 +1
SLIDE_SAME_TICK = 1e-4


def _sec_to_beat(sec, bpm, grid, keep_denom=False):
    """秒数转 Malody beat [整拍, 分子, 分母]。"""
    beatf = max(0.0, float(sec) * float(bpm) / 60.0)
    grid = max(1, int(grid))
    snapped = round(beatf * grid)
    b, num = divmod(int(snapped), grid)
    if keep_denom:
        return [b, num, grid]
    if num == 0:
        return [b, 0, 1]
    divisor = math.gcd(num, grid)
    return [b, num // divisor, grid // divisor]


def _slide_lane(cx, lanes):
    """Slide 中心横坐标 -> 最近轨道（按 SLIDE_LAYOUT 网格四舍五入）。"""
    lay = SLIDE_LAYOUT.get(lanes, SLIDE_LAYOUT[4])
    i = int(round((float(cx) - lay["offset"]) / lay["lane_width"]))
    return max(0, min(lanes - 1, i))


def _slide_lane_delta(dx, lanes):
    """seg 相对位移 -> 轨道变化量。"""
    lay = SLIDE_LAYOUT.get(lanes, SLIDE_LAYOUT[4])
    return int(round(float(dx) / lay["lane_width"]))


def _lane_x(lane, lanes=4):
    """离散轨道 -> Slide 中心横坐标（精确落在工具链可识别的轨道网格上）。"""
    lay = SLIDE_LAYOUT.get(lanes, SLIDE_LAYOUT[4])
    return lay["offset"] + int(lane) * lay["lane_width"]


def _lane_w(lanes=4):
    return SLIDE_LAYOUT.get(lanes, SLIDE_LAYOUT[4])["base_width"]


def _chart_to_beat_seconds(chart, t):
    """将 IR 时间换回 Malody beat 0 起算的秒数。"""
    return t if chart.times_relative else t - chart.offset_sec


def _chart_to_mmg_seconds(chart, t):
    """将 IR 时间换成二进制 MMG 的含音频偏移绝对秒。"""
    return t + chart.offset_sec if chart.times_relative else t


def _mc_to_chart(n, j, chart_dir):
    """单张 mc JSON -> Chart；当前仅支持保真转换 Malody Slide (mode=7)。"""
    meta = j.get("meta", {})
    mode = meta.get("mode", 0)
    song = meta.get("song", {})
    tl = j.get("time", [])
    bpm = float(tl[0].get("bpm", 120.0)) if tl else 120.0
    col = meta.get("mode_ext", {}).get("column", 4) or 4
    chart = Chart(title=song.get("title", "Untitled"),
                  artist=song.get("artist", ""),
                  creator=meta.get("creator", ""),
                  bpm=bpm, lanes=col, src_format="malody")
    chart.mc_version = meta.get("version", "")
    chart.mc_dir = chart_dir or ""
    _sf = song.get("file", "")
    chart.audio_ext = os.path.splitext(_sf)[1].lower() if _sf else ""
    chart.image_name = meta.get("background", "")
    if mode == 7:
        chart.slide_mode = True
        chart.lanes = 4
        chart.times_relative = True
        sfx_offset = 0.0
        for nt in j.get("note", []):
            if nt.get("type") == 1 and "sound" in nt:
                sfx_offset = float(nt.get("offset", 0) or 0) / 1000.0
                if not chart.audio_name:
                    chart.audio_name = nt["sound"]
                    chart.audio_ext = os.path.splitext(chart.audio_name)[1].lower()
        # Slide 的 beat 以谱面 0 拍起算；offset 仅用于音频同步，不能叠加到音符时间。
        chart.offset_sec = sfx_offset
        for i, nt in enumerate(j.get("note", [])):
            if nt.get("type") == 1 and "sound" in nt:
                continue
            if "beat" not in nt:
                continue
            t = _beat_to_sec(nt["beat"], bpm)
            source_beat = list(nt["beat"])
            x0, w0 = float(nt.get("x", 128)), float(nt.get("w", 64))
            lane0 = _slide_lane(x0, chart.lanes)
            seg = nt.get("seg")
            dirv = nt.get("dir")
            if not seg:
                if dirv in (SLIDE_DIR_LEFT, SLIDE_DIR_RIGHT):
                    delta = -1 if dirv == SLIDE_DIR_LEFT else 1
                    dest = max(0, min(chart.lanes - 1, lane0 + delta))
                    chart.notes.append(Note(
                        SLIDE, t, lane0,
                        segs=[(t, lane0), (t, dest)], order=i,
                        slide_x=x0, slide_w=w0, source_beat=source_beat,
                        arrow_dir=dirv))
                else:
                    chart.notes.append(Note(TAP, t, lane0, order=i,
                                            slide_x=x0, slide_w=w0,
                                            source_beat=source_beat))
                continue
            pts = [(t, lane0)]                # Slide 滑条：主 note + 关键帧
            raw_segs = []
            raw_seg_beats = []
            for fr in seg:
                fb = fr["beat"]
                rel_t = _beat_to_sec(fb, bpm)
                ft = t + rel_t
                rel_x = float(fr.get("x", 0))
                raw_segs.append((rel_t, rel_x))
                raw_seg_beats.append(list(fb))
                lane_k = lane0 + _slide_lane_delta(rel_x, chart.lanes)
                pts.append((ft, max(0, min(chart.lanes - 1, lane_k))))
            # 保留同时刻换轨帧（MMG 的“同时刻跨轨”滑键节点），只剔除完全重复的点
            kept = []
            for tt, lk in pts[1:]:
                if tt < t - 1e-6:
                    continue
                prev = kept[-1] if kept else (t, lane0)
                if abs(tt - prev[0]) < 1e-6 and lk == prev[1]:
                    continue
                kept.append((tt, lk))
            pts = kept
            if not pts:
                chart.notes.append(Note(TAP, t, lane0, order=i,
                                        slide_x=x0, slide_w=w0,
                                        source_beat=source_beat))
                continue
            if len({lane0} | {lk for _, lk in pts}) == 1:
                te = max(pts[-1][0], t + 0.001)
                chart.notes.append(Note(HOLD, t, lane0, te,
                                        segs=[(t, lane0)] + pts, order=i,
                                        slide_x=x0, slide_w=w0,
                                        slide_segs=raw_segs,
                                        source_beat=source_beat,
                                        source_seg_beats=raw_seg_beats))
            else:
                chart.notes.append(Note(SLIDE, t, lane0,
                                        segs=[(t, lane0)] + pts, order=i,
                                        slide_x=x0, slide_w=w0,
                                        slide_segs=raw_segs,
                                        source_beat=source_beat,
                                        source_seg_beats=raw_seg_beats))
    else:
        raise ValueError(f"只支持 Malody Slide（mode=7），当前 mode={mode}")
    return chart


def parse_malody_all(path, mode=7):
    """只解析 Malody Slide（mode=7）谱面；多玩法包会跳过其余玩法。"""
    if not zipfile.is_zipfile(path):
        raise ValueError("不是有效的 ZIP/Malody 谱面包 (.mcz 即 ZIP)")
    charts = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for n in names:
            if not n.lower().endswith(".mc"):
                continue
            try:
                j = json.loads(z.read(n).decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError):
                print(f"  [警告] 跳过无法解析的谱: {_fix_zipname(n)}")
                continue
            m = j.get("meta", {}).get("mode", 0)
            if m != mode:
                print(f"  [跳过] 非 Slide mode={m}: {_fix_zipname(n)}")
                continue
            try:
                chart = _mc_to_chart(n, j, os.path.dirname(n))
            except ValueError as e:
                print(f"  [警告] {e}: {_fix_zipname(n)}")
                continue
            charts.append(chart)
        # 资产挂到每张谱：mcz 结构按同子目录取（bg.png/song-bg.ogg），退化为全包搜索
        def _assets_for(entry_dir, wanted_audio="", wanted_image=""):
            audio = image = None
            aname = iname = None
            cand = [x for x in names if os.path.dirname(x) == entry_dir] or names
            wanted_audio = os.path.basename(wanted_audio).lower()
            wanted_image = os.path.basename(wanted_image).lower()
            for x in cand:
                low = x.lower()
                fixed = _fix_zipname(x)
                is_audio = low.endswith((".mp3", ".ogg", ".wav"))
                is_image = low.endswith((".jpg", ".jpeg", ".png"))
                if is_audio and wanted_audio and fixed.lower() == wanted_audio:
                    audio, aname = z.read(x), fixed
                elif is_image and wanted_image and fixed.lower() == wanted_image:
                    image, iname = z.read(x), fixed
                elif audio is None and is_audio:
                    audio, aname = z.read(x), _fix_zipname(x)
                elif image is None and is_image:
                    image, iname = z.read(x), _fix_zipname(x)
            return audio, aname, image, iname
        for ch in charts:
            a, an, im, imn = _assets_for(ch.mc_dir, ch.audio_name, ch.image_name)
            ch.audio_bytes, ch.audio_name = a, an
            ch.image_bytes, ch.image_name = im, imn
            if not ch.audio_ext and an:
                ch.audio_ext = os.path.splitext(an)[1].lower()
    if not charts:
        raise ValueError("包内没有可转换的 Malody Slide 谱面（mode=7）")
    charts.sort(key=lambda c: (-len(c.notes), c.mc_version))
    return charts


def parse_malody(path, prefer_version=None, mode=0):
    if not zipfile.is_zipfile(path):
        raise ValueError("不是有效的 ZIP/Malody 谱面包")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        mcs = [n for n in names if n.lower().endswith(".mc")]
        if not mcs:
            raise ValueError("谱面包里没有 .mc 文件")
        jss = []
        for n in mcs:
            try:
                j = json.loads(z.read(n).decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError):
                # 非标准 mc（如 8K slide 谱缺 column 字段等）跳过并提示
                print(f"  [警告] 跳过无法解析的谱: {_fix_zipname(n)}")
                continue
            jss.append((n, j))
        # 只取指定模式（0=Key）；Malody 的 slide/dj 等模式谱字段不同
        cand = [(n, j) for n, j in jss if j.get("meta", {}).get("mode", 0) == mode]
        if not cand:
            raise ValueError(f"包内没有 mode={mode} (Key) 的谱面")
        if prefer_version is not None:
            sel = [c for c in cand if c[1].get("meta", {}).get("version", "") == prefer_version]
            if sel:
                cand = sel
        best, best_mc = -1, None
        for n, j in cand:
            if len(j.get("note", [])) > best:
                best, best_mc = len(j["note"]), j
        j = best_mc
        meta = j.get("meta", {})
        song = meta.get("song", {})
        tl = j.get("time", [])
        bpm = float(tl[0].get("bpm", 120.0)) if tl else 120.0
        col = meta.get("mode_ext", {}).get("column", 4) or 4
        chart = Chart(title=song.get("title", "Untitled"),
                      artist=song.get("artist", ""),
                      creator=meta.get("creator", ""),
                      bpm=bpm, lanes=col, src_format="malody")
        chart.mc_version = meta.get("version", "")
        for i, nt in enumerate(j.get("note", [])):
            lane = int(nt.get("column", 0))
            t = _beat_to_sec(nt["beat"], bpm)
            if nt.get("endbeat"):
                te = _beat_to_sec(nt["endbeat"], bpm)
                chart.notes.append(Note(HOLD, t, lane, te, order=i))
            else:
                chart.notes.append(Note(TAP, t, lane, order=i))
        for n in names:
            low = n.lower()
            if low.endswith((".mp3", ".ogg", ".wav")):
                chart.audio_name, chart.audio_bytes = _fix_zipname(n), z.read(n)
            elif low.endswith((".jpg", ".jpeg", ".png")):
                chart.image_name, chart.image_bytes = _fix_zipname(n), z.read(n)
    return chart


def _num(v):
    """整数值输出为 int，避免原版 MC 中不存在的 64.0 形式。"""
    v = round(float(v), 4)
    return int(v) if abs(v - round(v)) < 1e-9 else v


def _slide_asset_name(chart, kind):
    """MC 内引用名：优先源文件名（空谱/原版都用真实曲名），MCZ 内再规整为 ASCII。"""
    if kind == "audio":
        src = chart.audio_name or ""
        ext = (chart.audio_ext or os.path.splitext(src)[1] or ".mp3").lower()
        if not ext.startswith("."):
            ext = "." + ext
        base = os.path.basename(src.replace("\\", "/")) if src else ""
        if base and os.path.splitext(base)[1]:
            return base
        return (_safe(chart.title) or "audio") + ext
    src = chart.image_name or ""
    ext = (os.path.splitext(src)[1] or ".jpg").lower()
    base = os.path.basename(src.replace("\\", "/")) if src else ""
    if base and os.path.splitext(base)[1]:
        return base
    return "background" + ext


def _mc_rel_beat(ticks, grid=SLIDE_BEAT_GRID):
    """音游工具箱：内部 tick 用 768；1/4 拍写成 [0,192,768]，不用 [0,0,4]。"""
    ticks = max(0, int(round(ticks)))
    whole, rem = divmod(ticks, grid)
    return [whole, rem, grid]


def _same_tick(a, b):
    return abs(float(a) - float(b)) < SLIDE_SAME_TICK


def _mmg_head_cross(pts):
    """链头同拍换轨 A→B。返回 (起轨, 落轨, t0)，否则 None。"""
    if not pts or len(pts) < 2:
        return None
    (t0, start), (t1, dest) = pts[0], pts[1]
    if not _same_tick(t0, t1) or int(start) == int(dest):
        return None
    return int(start), int(dest), float(t0)


def _mmg_instant_cross(pts):
    """同拍恰好两点换轨：写成瞬时 dir。"""
    if not pts or len(pts) != 2:
        return None
    cross = _mmg_head_cross(pts)
    return None if cross is None else (cross[0], cross[1])


def _mmg_flick_then_hold(pts):
    """划完再停：(t,A),(t,B) 后一直停在 B。"""
    cross = _mmg_head_cross(pts)
    if cross is None or len(pts) < 3:
        return None
    start, dest, t0 = cross
    if any(int(lane) != dest for _, lane in pts[2:]):
        return None
    t_end = max(float(t) for t, _ in pts)
    if t_end - t0 < SLIDE_SAME_TICK:
        return None
    return start, dest, t_end


def _mmg_flick_return(pts):
    """往返划钩：叠键方向 A→B，中间停 B，结尾回到 A。"""
    cross = _mmg_head_cross(pts)
    if cross is None or len(pts) < 3:
        return None
    start, dest, t0 = cross
    t_end, last = float(pts[-1][0]), int(pts[-1][1])
    if last != start or t_end - t0 < SLIDE_SAME_TICK:
        return None
    if any(int(lane) != dest for _, lane in pts[2:-1]):
        return None
    return start, dest, t_end


def _dir_of(start, dest):
    return SLIDE_DIR_LEFT if dest < start else SLIDE_DIR_RIGHT


def _apply_dir_note(item, chart, start, dest, dirv=None):
    item["x"] = _num(_lane_x(start, chart.lanes))
    item["w"] = SLIDE_DIR_WIDTH
    item["dir"] = dirv if dirv in (SLIDE_DIR_LEFT, SLIDE_DIR_RIGHT) else _dir_of(start, dest)
    return item


def _apply_flick_hook(item, chart, start, dest, t_end, beat_seconds, grid, back=None):
    """瞬时划钩：第一段 1/96 x:±61；第二段停则同 x，回则 ∓1。"""
    dx = SLIDE_FLICK_HOLD_DX * (dest - start)
    hold_sec = _chart_to_beat_seconds(chart, t_end) - beat_seconds
    hold_ticks = int(round(max(0.0, hold_sec) * chart.bpm / 60.0 * grid))
    item["x"] = _num(_lane_x(start, chart.lanes))
    item["w"] = SLIDE_DIR_WIDTH
    item["seg"] = [
        {"beat": list(SLIDE_FLICK_HOLD_BEAT), "x": dx},
        {"beat": _mc_rel_beat(hold_ticks, grid), "x": dx if back is None else back},
    ]
    return item


def _slide_seg_frames(chart, n, beat_seconds, grid):
    """对齐音游工具箱原版.mc：同拍只留落点，斜滑占满上一段停顿。

    钩子 (t0,A),(t1,A),(t1,B) → 一根 seg：beat=t1-t0，x=(B-A)*64。
    不再在停顿后再叠 1/4 拍（那会拉出长箭头）。
    """
    points = n.segs or [(n.t, n.lane), (n.t_end, n.lane)]
    groups = []
    for t, lane in points:
        t = _chart_to_beat_seconds(chart, t)
        if groups and abs(t - groups[-1][0]) < 1e-6:
            groups[-1][1] = int(lane)
        else:
            groups.append([t, int(lane)])

    lane_w = SLIDE_LAYOUT.get(chart.lanes, SLIDE_LAYOUT[4])["lane_width"]
    frames = []
    prev_ticks = 0
    cur_lane = n.lane
    for t, dest in groups:
        rel_ticks = int(round(max(0.0, t - beat_seconds) * chart.bpm / 60.0 * grid))
        if rel_ticks <= 0:
            cur_lane = dest
            continue
        if dest == cur_lane and rel_ticks <= prev_ticks:
            continue
        if rel_ticks <= prev_ticks:
            continue
        prev_ticks = rel_ticks
        cur_lane = dest
        dx = int(round((dest - n.lane) * lane_w))
        frame = {"beat": _mc_rel_beat(rel_ticks, grid)}
        if dx:
            frame["x"] = dx
        frames.append(frame)
    return frames


def write_slide_malody(chart, out_path, grid=16, package=True):
    """输出标准 Malody Slide（mode=7）MC；package=True 时再打包为 MCZ。"""
    grid = SLIDE_BEAT_GRID if not chart.times_relative else max(int(grid), SLIDE_BEAT_GRID)
    notes = []
    bpm_out = _num(chart.bpm)
    for n in chart.sorted():
        beat_seconds = _chart_to_beat_seconds(chart, n.t)
        keep_raw = chart.times_relative and n.source_beat is not None
        beat = list(n.source_beat) if keep_raw \
            else _sec_to_beat(max(0.0, beat_seconds), chart.bpm, grid, True)
        keep_xy = chart.times_relative and n.slide_x is not None
        width = float(n.slide_w) if keep_xy else _lane_w(chart.lanes)
        x = float(n.slide_x) if keep_xy else _lane_x(n.lane, chart.lanes)
        item = {"beat": beat, "x": _num(x), "w": _num(width)}

        if n.kind == SLIDE and not n.slide_segs:
            pts = n.segs or []
            if n.arrow_dir in (SLIDE_DIR_LEFT, SLIDE_DIR_RIGHT):
                item["w"] = SLIDE_DIR_WIDTH
                item["dir"] = n.arrow_dir
                notes.append(item)
                continue
            cross = _mmg_instant_cross(pts)
            if cross:
                notes.append(_apply_dir_note(item, chart, *cross))
                continue
            flick_hold = _mmg_flick_then_hold(pts)
            if flick_hold:
                notes.append(_apply_flick_hook(
                    item, chart, *flick_hold, beat_seconds, grid))
                continue
            flick_back = _mmg_flick_return(pts)
            if flick_back:
                start, dest, t_end = flick_back
                back = -SLIDE_FLICK_RETURN_DX if dest > start else SLIDE_FLICK_RETURN_DX
                notes.append(_apply_flick_hook(
                    item, chart, start, dest, t_end, beat_seconds, grid, back))
                continue
        if n.kind in (HOLD, SLIDE):
            raw_segs = n.slide_segs
            raw_beats = n.source_seg_beats
            if raw_segs and chart.times_relative:
                segs = []
                for i, (rel_t, rel_x) in enumerate(raw_segs):
                    fb = list(raw_beats[i]) if raw_beats and i < len(raw_beats) \
                        else _sec_to_beat(rel_t, chart.bpm, grid, True)
                    rel_beats = fb[0] + fb[1] / max(1, fb[2])
                    if rel_beats <= 1e-12:
                        continue
                    frame = {"beat": fb}
                    if abs(rel_x) > 1e-9:
                        frame["x"] = _num(rel_x)
                    segs.append(frame)
                if segs:
                    item["seg"] = segs
            else:
                segs = _slide_seg_frames(chart, n, beat_seconds, grid)
                if segs:
                    item["seg"] = segs
        notes.append(item)

    a_ext = (chart.audio_ext or os.path.splitext(chart.audio_name or "")[1] or ".mp3").lower()
    i_ext = (os.path.splitext(chart.image_name or "")[1] or ".jpg").lower()
    has_audio = chart.audio_bytes is not None or chart.audio_name
    has_image = bool(chart.image_bytes or chart.image_name)
    if package:
        aname = f"audio{a_ext}" if has_audio else ""
        iname = f"background{i_ext}" if has_image else ""
    else:
        aname = _slide_asset_name(chart, "audio") if has_audio else ""
        iname = _slide_asset_name(chart, "image") if has_image else ""
    if aname:
        bgm = {"beat": [0, 0, 1], "type": 1, "sound": aname}
        off_ms = int(round(chart.offset_sec * 1000))
        if off_ms:
            bgm["offset"] = off_ms
        notes.append(bgm)
    k = chart.lanes
    mc = {
        "meta": {"id": 0, "creator": chart.creator or "Guest",
                 "background": iname, "cover": iname,
                 "version": f"bxSmart_{k}k_hd_Lv.?",
                 "mode": 7,
                 "song": {"title": chart.title, "artist": chart.artist,
                          "titleorg": chart.title, "artistorg": chart.artist,
                          "file": aname, "bpm": bpm_out},
                 "mode_ext": {"bar_begin": 0},
                 "aimode": ""},
        "time": [{"beat": list(SLIDE_TIME_BEAT), "bpm": bpm_out, "delay": 0.0}],
        "note": notes,
        "extra": None,
    }
    base = out_path
    for e in (".mcz", ".mmg", ".mc", ".osu", ".json"):
        if base.lower().endswith(e):
            base = base[:-len(e)]
            break
    json_bytes = json.dumps(mc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not package:
        pkg = base + ".mc"
        with open(pkg, "wb") as f:
            f.write(json_bytes)
        produced = [pkg]
        if chart.audio_bytes is not None:
            audio_path = os.path.join(os.path.dirname(pkg), aname)
            with open(audio_path, "wb") as f:
                f.write(chart.audio_bytes)
            produced.append(audio_path)
        if chart.image_bytes:
            image_path = os.path.join(os.path.dirname(pkg), iname)
            with open(image_path, "wb") as f:
                f.write(chart.image_bytes)
            produced.append(image_path)
        return produced
    if chart.audio_bytes is not None:
        pkg = base + ".mcz"
        with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
            # Malody 参考谱包使用 0/ 目录；MC 内引用保持同目录的裸文件名。
            prefix = "0"
            if chart.image_bytes:
                z.writestr(f"{prefix}/{iname}", chart.image_bytes)
            z.writestr(f"{prefix}/{aname}", chart.audio_bytes)
            z.writestr(f"{prefix}/chart.mc", json_bytes)
        return pkg
    pkg = base + ".mc"
    with open(pkg, "wb") as f:
        f.write(json_bytes)
    return pkg


write_malody = write_slide_malody  # 兼容旧的 Python 调用名


# ------------------------------------------------------------------ 二进制 mmg（官方布局）

def parse_mmg_binary(path):
    b = open(path, "rb").read()
    magic, bpm, nlen = struct.unpack_from("<IfI", b, 0)
    if magic != MMG_MAGIC:
        raise ValueError(f"魔数不符：{magic}")
    cfg = b[12:12 + nlen].decode("utf-8", "replace")
    parts = cfg.split(",")
    audio_name = parts[0]
    g = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8
    cells = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    off = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0

    chart = Chart(title=os.path.splitext(audio_name)[0], bpm=float(bpm),
                  offset_sec=off / 1000.0, lanes=4, audio_name=audio_name,
                  src_format="mmg", raw_cfg=cfg, grid_g=g, cells=cells)

    chains = {}
    pos = 12 + nlen
    rec_i = 0
    while pos + MMG_RECORD_SIZE <= len(b):
        track, t, typ, grp, idx, evt = struct.unpack_from("<ifiiii", b, pos)
        pos += MMG_RECORD_SIZE
        if typ == 1 or typ == 0:
            chart.notes.append(Note(TAP, t, track, gid=-1, evt=evt, order=rec_i))
        elif typ == 2:
            chains.setdefault(grp, []).append((idx, track, t, evt, rec_i))
        rec_i += 1
    if len(b) - pos:
        raise ValueError(f"尾部残留 {len(b)-pos} 字节，非 24 对齐，文件可能损坏")

    for gid, segs in chains.items():
        segs.sort(key=lambda s: s[0])
        lanes = [s[1] for s in segs]
        ts = [s[2] for s in segs]
        first_order = min(s[4] for s in segs)
        evt = segs[0][3]
        all_segs = [(s[2], s[1]) for s in segs]
        row_pos = [s[4] for s in segs]
        row_evts = [s[3] for s in segs]
        if len(set(lanes)) == 1:
            chart.notes.append(Note(HOLD, ts[0], lanes[0],
                                    t_end=max(ts[-1], ts[0] + 0.001),
                                    segs=all_segs, gid=gid, evt=evt,
                                    order=first_order, row_orders=row_pos,
                                    row_evts=row_evts))
        else:
            chart.notes.append(Note(SLIDE, ts[0], lanes[0],
                                    segs=all_segs, gid=gid, evt=evt,
                                    order=first_order, row_orders=row_pos,
                                    row_evts=row_evts))
    return chart


def write_mmg_binary(chart, out_path):
    """按 DataExport.cs 的官方布局回写；Tap 按时间序、链按原始位置序。"""
    cfg = chart.raw_cfg
    if not cfg:
        a_ext = (chart.audio_ext or os.path.splitext(chart.audio_name or "")[1] or ".mp3").lower()
        stem = _safe(chart.audio_name or chart.title)
        if os.path.splitext(stem)[1].lower() != a_ext:
            stem = os.path.splitext(stem)[0] + a_ext
        last_t = _chart_to_mmg_seconds(chart, chart.sorted()[-1].t)
        cells = chart.cells or int(round(last_t / (60.0 / chart.bpm))) + 1
        cfg = f"{stem},{chart.grid_g},{cells},{int(round(chart.offset_sec * 1000))}"
    cfg_b = cfg.encode("utf-8")
    out = bytearray(struct.pack("<IfI", MMG_MAGIC, float(chart.bpm), len(cfg_b)))
    out += cfg_b

    # 逐行回写：每行记住它在源文件中的位置，最终按位置排序拼接
    used = [n.gid for n in chart.notes if n.gid and n.gid > 0]
    nxt = (max(used) + 1) if used else 1
    rows = []   # (行位置, 24B 字节)
    for n in sorted(chart.notes, key=lambda x: x.order):
        if n.kind == TAP:
            t = _chart_to_mmg_seconds(chart, n.t)
            rows.append((n.order, struct.pack("<ifiiii", n.lane, t, 1, -1, -1, n.evt)))
            continue
        gid = n.gid if n.gid and n.gid > 0 else nxt
        if not (n.gid and n.gid > 0):
            nxt += 1
        pts = n.segs if n.segs else [(n.t, n.lane), (max(n.t_end, n.t + 0.001), n.lane)]
        rpos = n.row_orders if n.row_orders else list(range(n.order, n.order + len(pts)))
        revt = n.row_evts if n.row_evts else [n.evt if i == 0 else 0 for i in range(len(pts))]
        for i, (tt, lk) in enumerate(pts):
            t = _chart_to_mmg_seconds(chart, tt)
            rows.append((rpos[i], struct.pack("<ifiiii", lk, t, 2, gid, i, revt[i])))
    rows.sort(key=lambda x: x[0])
    for _, payload in rows:
        out += payload
    with open(out_path, "wb") as f:
        f.write(out)
    return out_path


# ------------------------------------------------------------------ BNDQ JSON（编辑器同款导出）

def _bpm_tick(t, bpm):
    return math.ceil(bpm / 60.0 * BNDQ_TICKS_PER_BEAT * t)


def write_bndq(chart, out_path):
    tpl_dir = None
    # 优先使用编辑器原模板文件
    pd_data = _find_patterndiy_data()
    if pd_data:
        tpl_dir = os.path.join(pd_data, "configs")
    if tpl_dir:
        text = open(os.path.join(tpl_dir, "toJsonTemplete.txt"), encoding="utf-8").read()
        item_tpl = open(os.path.join(tpl_dir, "toJsonTempleteItem.txt"), encoding="utf-8").read()
    else:
        text = ('{"signature":"BNDQ","version":"1.2.1","tempo":#BPM,"samples":[],'
                '"tracks":[{"track":3,"note":[#TRACK1]},'
                '{"track":4,"note":[#TRACK2]},'
                '{"track":5,"note":[#TRACK3]},'
                '{"track":6,"note":[#TRACK4]}]}')
        item_tpl = ('{"tick": #TIME, "key": 0, "dur": #DUR, "isEnd": #ISEND,'
                    ' "toTrack": #TOTRACK, "volume": 0, "pan": 0, "attr": #ATTR}')
    bpm = chart.bpm
    tracks = {0: [], 1: [], 2: [], 3: []}
    plain = sorted(chart.notes, key=lambda n: (n.t, n.lane))

    def mk(tick, dur, isend, to_track, attr):
        return (item_tpl.replace("#TIME", str(tick)).replace("#DUR", str(dur))
                .replace("#ISEND", str(isend)).replace("#TOTRACK", str(to_track))
                .replace("#ATTR", str(attr)))

    for n in plain:
        if n.kind == TAP:
            tracks[n.lane].append(mk(_bpm_tick(n.t, bpm), 0, 0, 0, 0))
        elif n.kind == HOLD:
            t0, t1 = _bpm_tick(n.t, bpm), _bpm_tick(n.t_end, bpm)
            tracks[n.lane].append(mk(t0, t1 - t0, 1, 0, 3))
        elif n.kind == SLIDE and n.segs:
            segs = n.segs
            ticks = [_bpm_tick(tt, bpm) for tt, _ in segs]
            for i in range(len(segs) - 1):      # 最后一节点不输出（同官方）
                lk = segs[i][1]
                same_track = segs[i + 1][1] == lk
                same_time = _same_tick(segs[i + 1][0], segs[i][0])
                dur = (ticks[i + 1] - ticks[i]) if same_track else 0
                attr = 3 if i == 0 else 4
                isend = 1 if i == len(segs) - 2 else 0
                tracks[lk].append(mk(ticks[i], dur, isend, segs[i + 1][1] + 3, attr))

    text = text.replace("#BPM", str(bpm))
    for i, ph in enumerate(["#TRACK1", "#TRACK2", "#TRACK3", "#TRACK4"]):
        body = ",\r\n".join(tracks.get(i, []))
        text = text.replace(ph, body)
    with open(out_path, "w", encoding="ascii", errors="replace") as f:
        f.write(text)
    return out_path


# ------------------------------------------------------------------ osu!mania

def write_osu(chart, out_path):
    bpm = chart.bpm
    def ms(sec): return int(round(sec * 1000))
    lines = ["osu file format v14", "", "[General]", "AudioFilename: audio.mp3",
             "PreviewTime: -1", "Countdown: 0", "Mode: 3",
             "", "[Metadata]", f"Title:{chart.title}", f"Artist:{chart.artist}",
             f"Creator:{chart.creator or 'chart_convert'}", "Version:转编",
             "", "[Difficulty]",
             "HPDrainRate:5", "CircleSize:4", "OverallDifficulty:8", "ApproachRate:9",
             "SliderMultiplier:1.4", "SliderTickRate:1",
             "", "[Events]", "", "[TimingPoints]",
             f"{ms(chart.offset_sec)},{60000.0 / bpm},4,1,0,80,1,0", "", "[HitObjects]"]
    for n in chart.sorted():
        x = int(256 / chart.lanes * (n.lane + 0.5))
        t = ms(n.t)
        te = None
        if n.kind == HOLD:
            te = ms(max(n.t_end, n.t + 1))
        elif n.kind == SLIDE and n.segs:
            te = ms(max(n.segs[-1][0], n.t + 1))   # 滑链折叠为 Hold
        if te is None:
            lines.append(f"{x},192,{t},1,0,0:0:0:0:")
        else:
            lines.append(f"{x},192,{t},128,0,2|1|0:0:0:0:0:{te}:0:0:0:")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


# ------------------------------------------------------------------ JSON IR

def write_json(chart, out_path):
    d = {"title": chart.title, "artist": chart.artist, "creator": chart.creator,
         "bpm": chart.bpm, "offset_sec": chart.offset_sec, "lanes": chart.lanes,
         "src_format": chart.src_format}
    if chart.raw_cfg:
        d["mmg_cfg"] = chart.raw_cfg
    d["notes"] = []
    for n in chart.sorted():
        e = {"kind": n.kind, "t": round(n.t, 5), "lane": n.lane}
        if n.kind == HOLD:
            e["t_end"] = round(n.t_end, 5)
        elif n.kind == SLIDE:
            e["segs"] = [[round(tt, 5), lk] for tt, lk in n.segs]
        d["notes"].append(e)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return out_path


# ------------------------------------------------------------------ 通用

def _safe(s):
    return "".join(c for c in (s or "chart") if c not in '\\/:*?"<>|').strip() or "chart"


def _fix_zipname(n):
    """zipfile 对未带 UTF-8 标志的条目按 cp437 解码，中文名会成乱码，还原之。"""
    base = os.path.basename(n)
    try:
        fixed = base.encode("cp437").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return base


def _attach_audio(chart, dirs):
    """二进制 mmg 无内嵌音频：在候选目录里按 cfg 音频名（含同主名 mp3/ogg/wav 变体）找曲。"""
    if not chart.audio_name:
        return False
    stem = os.path.splitext(chart.audio_name)[0]
    exts = [os.path.splitext(chart.audio_name)[1].lower(), ".mp3", ".ogg", ".wav"]
    tried = set()
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        seen = set()
        for e in exts:
            if not e or e in seen:
                continue
            seen.add(e)
            fp = os.path.join(d, stem + e)
            if fp in tried:
                continue
            tried.add(fp)
            if os.path.isfile(fp):
                chart.audio_bytes = open(fp, "rb").read()
                chart.audio_name = stem + e
                chart.audio_ext = e
                return True
    return False


def _find_ffmpeg():
    """查找 ffmpeg：优先脚本同目录自带的 ffmpeg.exe，其次 PATH / 常见安装位置。"""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.isfile(here):
        return here
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import glob as _glob
    cands = []
    for drv in ("C", "D", "E", "F"):
        cands += _glob.glob(f"{drv}:\\Ultimate Vocal Remover\\ffmpeg.exe")
        cands += _glob.glob(f"{drv}:\\JianyingPro\\*\\ffmpeg.exe")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _find_patterndiy_data():
    """自动定位 PatternDIY 编辑器的 PatternDIY_editor_Data 目录。

    优先级：环境变量 PATTERNDIY_DATA_DIR -> 脚本自身/上级 -> 各本地盘符一级目录。
    """
    env = os.environ.get("PATTERNDIY_DATA_DIR")
    if env and os.path.isdir(env):
        return env

    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(here)):
        hit = os.path.join(base, "PatternDIY_editor_Data")
        if os.path.isdir(hit):
            return hit

    # 各本地盘符一级目录下搜索（覆盖 D:\\MusicScoreConvter\\PatternDIY_editor_Data 等布局）
    for drv in ("C", "D", "E", "F"):
        root = drv + ":\\"
        if not os.path.isdir(root):
            continue
        try:
            children = os.listdir(root)
        except OSError:
            continue
        for child in children:
            hit = os.path.join(root, child, "PatternDIY_editor_Data")
            if os.path.isdir(hit):
                return hit
    return None


def _ogg_to_mp3(data, out_mp3):
    """音频字节 -> mp3（编辑器只认 mp3/wav，ogg 无法解码）。"""
    import subprocess, tempfile
    ff = _find_ffmpeg()
    if not ff:
        return False
    tmp = tempfile.NamedTemporaryFile(suffix=".src.ogg", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", tmp.name,
             "-vn", "-acodec", "libmp3lame", "-b:a", "192k", out_mp3],
            capture_output=True, timeout=300)
        return r.returncode == 0 and os.path.isfile(out_mp3) and os.path.getsize(out_mp3) > 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def detect_and_parse(path):
    """返回单个 Chart（多谱包取 Hold+音符综合最多的一张），兼容旧调用。"""
    charts = parse_charts(path)
    return charts[0]


def parse_charts(path):
    """返回包内全部可转谱面（Malody 每个难度一张；二进制 mmg 一张）。"""
    head = open(path, "rb").read(4)
    if head[:2] == b"PK":
        return parse_malody_all(path)
    magic = struct.unpack_from("<I", head, 0)[0] if len(head) >= 4 else 0
    if magic == MMG_MAGIC:
        return [parse_mmg_binary(path)]
    if head[:1] == b"{":
        return [_mc_json_to_chart(json.loads(open(path, "rb").read().decode("utf-8")))]
    raise ValueError(f"无法识别的谱面格式（头={head.hex()}）")


def _mc_json_to_chart(j):
    return _mc_to_chart("", j, "")


def summarize(chart):
    c = {}
    for n in chart.notes:
        c[n.kind] = c.get(n.kind, 0) + 1
    return (f"标题={chart.title!r} BPM={chart.bpm:g} 轨道={chart.lanes} "
            f"来源={chart.src_format} 音符={len(chart.notes)} "
            f"(Tap {c.get(TAP, 0)}/Hold {c.get(HOLD, 0)}/Slide {c.get(SLIDE, 0)})")


def convert(path, out_dir=None, to="auto", grid=16):
    """在 Malody Slide（mode=7）与二进制 MMG 间转换。"""
    charts = parse_charts(path)
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "_转编输出")
    os.makedirs(out_dir, exist_ok=True)
    stem = _safe(os.path.splitext(os.path.basename(path))[0])

    aliases = {"slide": "slide-mc", "malody": "slide-mcz"}
    to = aliases.get(to, to)
    results = []
    for chart in charts:
        if chart.src_format == "mmg":
            home = "mmg"
        elif chart.slide_mode:
            home = "slide"
        else:
            home = "malody"
        if to == "auto":
            targets = ["slide-mc"] if home == "mmg" else ["mmg"]
        else:
            targets = [to]
        # 二进制 mmg 不含音频：打出 Slide MC/MCZ 前按 cfg 名就近找同名曲。
        if (chart.src_format == "mmg" and chart.audio_bytes is None
                and any(t in ("slide-mc", "slide-mcz") for t in targets)):
            pd_data = _find_patterndiy_data()
            audio_dirs = [os.path.dirname(os.path.abspath(path))]
            if pd_data:
                audio_dirs.append(os.path.join(pd_data, "musics"))
            audio_dirs.append(out_dir)
            _attach_audio(chart, audio_dirs)
        label = _safe(chart.mc_version).strip() if (chart.src_format == "malody" and chart.mc_version and chart.mc_version != "chart") else ""
        f_stem = f"{stem}_{label}" if label else stem
        produced = []
        for t in targets:
            if t == "slide-mc":
                produced.extend(write_slide_malody(
                    chart, os.path.join(out_dir, f_stem), grid=grid, package=False))
            elif t == "slide-mcz":
                produced.append(write_slide_malody(
                    chart, os.path.join(out_dir, f_stem), grid=grid, package=True))
            elif t == "mmg":
                produced.append(write_mmg_binary(chart, os.path.join(out_dir, f_stem + "_bin.mmg")))
            elif t == "osu":
                produced.append(write_osu(chart, os.path.join(out_dir, f_stem + ".osu")))
            elif t == "json":
                produced.append(write_json(chart, os.path.join(out_dir, f_stem + ".json")))
            elif t == "bndq":
                produced.append(write_bndq(chart, os.path.join(out_dir, f_stem + ".bndq.json")))
        # 附带音频副本：编辑器 musics\ 只认 mp3（NAudio AudioFileReader）
        # ogg/wav 源自动转 mp3，并同步重写 mmg 的 cfg 音频名
        if chart.audio_bytes is not None and "mmg" in targets:
            a_ext = (chart.audio_ext or os.path.splitext(chart.audio_name or "")[1] or ".mp3").lower()
            want = _safe(chart.audio_name or (chart.title + a_ext))
            if os.path.splitext(want)[1].lower() != a_ext:
                want = os.path.splitext(want)[0] + a_ext
            if a_ext != ".mp3" and _find_ffmpeg():
                mp3_name = os.path.splitext(want)[0] + ".mp3"
                mp3_path = os.path.join(out_dir, mp3_name)
                if _ogg_to_mp3(chart.audio_bytes, mp3_path):
                    for bf in [f for f in produced if f.endswith("_bin.mmg")]:
                        bb = open(bf, "rb").read()
                        nlen = struct.unpack_from("<I", bb, 8)[0]
                        old_cfg = bb[12:12 + nlen].decode("utf-8")
                        nb = (mp3_name + "," + old_cfg.split(",", 1)[1]).encode("utf-8")
                        open(bf, "wb").write(
                            struct.pack("<IfI", MMG_MAGIC, float(chart.bpm), len(nb))
                            + nb + bb[12 + nlen:])
                    chart.audio_name, chart.audio_ext = mp3_name, ".mp3"
                    produced.append(mp3_path + "   <-- 放编辑器 musics\\ 目录")
                else:
                    ap = os.path.join(out_dir, want)
                    with open(ap, "wb") as f:
                        f.write(chart.audio_bytes)
                    produced.append(ap + "   <-- 转mp3失败，请自行转换！编辑器不支持 " + a_ext)
            else:
                ap = os.path.join(out_dir, want)
                with open(ap, "wb") as f:
                    f.write(chart.audio_bytes)
                tip = "   <-- 警告：编辑器只认mp3，请先转码！" if a_ext != ".mp3" \
                    else "   <-- 需与 _bin.mmg 同名放在编辑器 musics\\ 目录"
                produced.append(ap + tip)
        results.append((chart, produced))
    return results


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Malody Slide（mode=7）与二进制 .mmg 转编")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", default=None, help="输出目录")
    ap.add_argument("--to", default="auto",
                    choices=["auto", "slide-mc", "slide-mcz", "slide", "malody", "mmg"],
                    help="slide-mc=推荐：标准 MC 与资源；slide-mcz=直接打包；"
                         "slide/malody 为旧参数别名")
    ap.add_argument("--grid", type=int, default=768,
                    help="Slide 每拍 tick 数（默认 768，对齐 Malody/工具箱）")
    args = ap.parse_args(argv)

    for p in args.inputs:
        print("=" * 68)
        print("输入:", p)
        results = convert(p, args.out, args.to, args.grid)
        for chart, outs in results:
            print(summarize(chart))
            for o in outs:
                print("  ->", o)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
