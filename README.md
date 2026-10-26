# Malody 谱面转换器

将不休的音符 / GameForKeyEditor（PatternDIY）的二进制 MMG 谱面与 Malody Slide（mode=7）谱面互相转换，提供 Tkinter 图形界面和命令行入口。

## 环境与启动

- Python 3.10 或更高版本，包含 Tkinter；Python 代码仅依赖标准库。
- Windows 双击 `启动转编.bat`，也可拖放谱面文件到该启动脚本。
- 跨平台可运行 `python launcher.py` 打开界面。
- OGG 转 MP3 需要 FFmpeg。请自行安装并加入 PATH，或将 `ffmpeg.exe` 放在项目目录；仓库不包含此第三方程序。

## 命令行

```console
python chart_convert.py "输入.mmg" --to slide-mc -o "输出目录"
python chart_convert.py "输入.mmg" --to slide-mcz -o "输出目录"
python chart_convert.py "输入.mcz" --to mmg -o "输出目录"
python chart_convert.py --help
```

`slide-mc` 输出标准 MC 和资源文件；`slide-mcz` 直接打包为 MCZ；`mmg` 输出二进制 MMG。`--grid` 默认 768 tick/拍。默认输出位置和自动转换方向以脚本及界面提示为准。

## 项目文件

- `chart_convert.py`：谱面解析、中间模型、转换与资源处理。
- `launcher.py`：Tkinter 界面，支持文件选择和转换进度显示。
- `启动转编.bat`：Windows 启动入口。
- `规则解析/`：MMG 格式及原 PatternDIY 编辑器的分析资料。

## 格式说明与限制

MMG 的记录从 `12 + nameLen` 开始，每条记录 24 字节；同轨节点链表示 Hold，跨轨链表示 Slide。转换器会将 Malody Slide 坐标量化到轨道，复杂滑动形状在往返转换时可能发生变化。

解析文档中的原编辑器目录、早期验证结果及其他格式说明属于分析背景；当前命令行支持的格式以 `--help` 为准。MMG 本身不包含音频，转换时请将对应素材放在可被脚本查找的位置。

本仓库不包含本地测试谱面、音乐、转换输出、探测脚本、Python 缓存和 FFmpeg 二进制文件。
