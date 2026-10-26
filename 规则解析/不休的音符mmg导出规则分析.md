# 不休的音符 mmg 导出规则分析（权威版）

> 分析对象：`D:\MusicScoreConvter\`（GameForKeyEditor / PatternDIY 制谱器，Unity 2018.4 Mono 打包）
> 证据链：`Assembly-CSharp.dll` 经 ilspycmd 反编译的**导出实现源码** + 官方 `sample\love9_{ez,hd,ms}.mmg` 实测校验。
> 与早期猜测版的关键差异已修正：字段顺序为 **track 在前**（i32 track, f32 time, …），记录起点是 **12+nameLen**（非固定 48），"长条头尾 + 滑键中段"的统一理解更正为 **同轨链=Hold / 换轨链=Slide**。

---

## 一、mmg 二进制导出（`DataExport.Exprot`）

### 1. 文件整体结构

```
偏移  长度   内容                       写出代码
0     4     i32  魔数 = 100000          BitConverter.GetBytes(100000)
4     4     f32  BPM                    GetBytes(EditorTimeModel.BPM)
8     4     i32  nameLen（配置串字节数）  GetBytes(UTF8串.Length)
12    nameLen  UTF-8 配置串              "曲名.mp3,G,cellCount,timeOffset"
12+nameLen …  24B × N 音符记录           每键 ParseKey()，按编辑器键表插入顺序
```

- **配置串三段数字**（`EditorTimeModel.GetNormalCofings()`）：
  - `G` = 每拍细分档（config.ini `GRID_COUNT_LIST=1,2,3,4,6,8,9,12,16`），决定编辑网格密度；
  - `cellCount` = 总格数，建谱时 `round(音频时长/(60/BPM)+1)`；
  - `timeOffset` = 音频偏移毫秒。**键的 time 是含偏移的绝对播放秒**（love9 首键 1.275s 即 offset 1275ms 落拍处）。
- 读取端（`Summoner.Format.Mmg.From`）循环 `while (array.Length > pos)` 每次 +24：文件必须恰好为 `头 + 24×N` 对齐，无尾部长度字段。

### 2. 音符记录（24 字节，`DataExport.ParseKey`）

| 偏移 | 类型 | 字段 | 含义 |
|---|---|---|---|
| 0  | i32 | Track | 轨道 0–3（4K）|
| 4  | f32 | KeyTime | 绝对秒 |
| 8  | i32 | KeyType | 1=SINGLE 单点；2=MULTI_KEY 长条/滑键节点（枚举 NONE=0/SINGLE=1/MULTI_KEY=2）|
| 12 | i32 | nodeGroupId | 链 ID；单点固定 **-1** |
| 16 | i32 | nodeIndex | 链内序号 **0..n-1**；单点固定 **-1** |
| 20 | i32 | eventId | 事件号，通常 0，原样携带 |

**特殊降级规则**（源码原样）：`MULTI_KEY 且其链只剩 1 个节点` → 写出时降级为
`type=1, group=-1, index=-1`（孤立长条退化为单点）。

### 3. 链（group）的语义 —— 实测 4 个样本验证

- 链内节点按 `index` 升序即玩法推进顺序，时间单调不减；
- **同轨链**（所有节点 Track 相同）＝ **Hold 长按条**，正常只有头尾 2 节点；
- **跨轨链**（节点间变轨）＝ **Slide 滑键链**，2~18 节点均出现（李明哲谱最长 18 节点）；
- 记录物理顺序＝编辑器 `_allKeyList` 插入序（落键先后），**不按时间排序**——这就是转编工具必须逐行保序才能字节级还原的原因。

实测样本（`verify` 全部通过，回写 MD5 与原文件一致）：

| 文件 | BPM | G | 记录数 | 单点 | Hold链 | Slide链 |
|---|---|---|---|---|---|---|
| love9_ez | 94 | 4 | 100 | 76 | 12 | 0 |
| love9_hd | 94 | 4 | 144 | 38 | 0 | 42 |
| love9_ms | 94 | 8 | 166 | 86 | 12 | 16 |
| 黎明之后_ms | 220 | 8 | 1467 | 508 | 54 | 168 |

### 4. 自动备份同款格式

`back_up\<日期>\<曲名>_时_分.mmg` 与正式导出同一 `DataExport` 链路，无额外字段。

---

## 二、BNDQ JSON 导出（`DataExportToJson.Export` + `Summoner.Format.Mmg.ToJson`，两处实现一致）

模板：`configs\toJsonTemplete.txt`（外壳）+ `configs\toJsonTempleteItem.txt`（单键），占位符替换。

### 1. 外壳规则

```json
{ "signature": "BNDQ", "version": "1.2.1", "tempo": #BPM,
  "samples": [],
  "tracks": [ {"track":3,"note":[#TRACK1]}, {"track":4,…#TRACK2},
              {"track":5,…#TRACK3}, {"track":6,…#TRACK4} ] }
```

- 4K 轨道 0–3 → JSON `track` 编号 **3/4/5/6**（恒 +3）；
- `#TRACKn` 空轨替换为空串；非空去掉末尾逗号；
- 键先按 `KeyTime` 升序排序后逐轨拼接；文件以 **ASCII** 字节写出。

### 2. 单键对象与 tick 换算

```json
{"tick": #TIME, "key": 0, "dur": #DUR, "isEnd": #ISEND,
 "toTrack": #TOTRACK, "volume": 0, "pan": 0, "attr": #ATTR}
```

- **`tick = ceil(BPM / 60 × 48 × time秒)`** —— 48 tick/拍的定点节拍坐标（MIREX/SM 系惯例），time 为绝对秒（含 offset）；
- 单点：`dur=0, isEnd=0, attr=0, toTrack=0`；
- MULTI_KEY 节点（**链尾节点不输出**，链的终点由前一节点 `isEnd=1` 表达）：
  - `attr`：链首 = **3**，链中间 = **4**；
  - `isEnd`：下一节点是链尾 → **1**，否则 0；
  - `dur`：与下一节点**同轨**（Hold 的下一段）= `ceil(next) − ceil(this)`；**同时刻跨轨** = 0；跨轨不同时刻（滑动）= 0；
  - `toTrack`：下一节点轨道 + 3（3..6），表达滑键去向；
  - `key/volume/pan` 恒 0。

---

## 三、发布包与命名规则（sample 实证 + 逻辑表）

- 官方示例包结构：`love9.mp3` + `love9_ez.mmg` / `love9_hd.mmg` / `love9_ms.mmg` + `love9_cover.png`——**曲名前缀必须完全一致**，三难度 `_ez/_hd/_ms` 后缀强约束（sample 内命名说明 txt，官方文档亦强调重名会撞服务器资源）；
- 上传链路：`PostMusicscoreUtil` + 腾讯云 COS 表单直传，`MusicscoreData` 打包 `DifficultData[]`（EZ/HD/MS）+ musicFile + coverFile，谱面包含 BPM/谱师/gameid 等元数据；
- 音频格式 MP3；封面 PNG；BPM 由 `BpmBuilder\BpmAnalyzer.exe` 离线测定后填入。

---

## 四、对转编脚本的落地约束（chart_convert.py 已实现）

1. 解析：24B 记录按 `ifiiii`（track 在前）解，记录起点 `12+nameLen`，尾部非对齐报错；
2. 链重组：group→链、index→序，同轨=Hold / 跨轨=Slide 两类 IR 对象，segs 全量保留（多节点 Hold 亦无损）；
3. 回写：逐行记录源物理位置与 eventId，按原位排布 → **4 个样本全部字节级 MD5 还原**；
4. BNDQ 导出：完全复刻官方模板替换与 ceil-tick 公式，优先读取编辑器 `configs\` 原模板；
5. 跨格式折损点：Malody/osu!mania 无滑键 → Slide 折叠为头尾 Hold；Malody 时间需吸附节拍栅格（--grid，建议 48）。
