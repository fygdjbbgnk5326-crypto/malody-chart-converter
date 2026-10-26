#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""谱面转编启动器 —— 一键转换二进制 MMG 与 Malody Slide（mode=7）。"""

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "chart_convert.py")
DEFAULT_OUT = os.path.join(HERE, "转编输出")

# 模式：界面文案 -> chart_convert --to
MODES = {
    "to_slide": {
        "label": "MMG → Slide MC（推荐）",
        "hint": "输出标准 mode=7 的 .mc、audio.* 与 background.*；导入音游工具箱后再导出 MCZ。",
        "to": "slide-mc",
        "filetypes": [
            ("二进制 MMG", "*.mmg"),
            ("全部", "*.*"),
        ],
    },
    "to_mcz": {
        "label": "MMG → Slide MCZ（直接打包）",
        "hint": "直接生成 mode=7 曲包，用于不需要音游工具箱二次导出的场景。",
        "to": "slide-mcz",
        "filetypes": [
            ("二进制 MMG", "*.mmg"),
            ("全部", "*.*"),
        ],
    },
    "to_mmg": {
        "label": "Slide MCZ → MMG",
        "hint": "只读取 Malody Slide（mode=7）；多玩法包会自动跳过其余模式。ogg 音频会经 ffmpeg 转 mp3。",
        "to": "mmg",
        "filetypes": [
            ("Malody Slide 包", "*.mcz"),
            ("Malody Slide MC", "*.mc"),
            ("全部", "*.*"),
        ],
    },
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("谱面转编启动器")
        self.geometry("720x520")
        self.minsize(640, 440)
        self.configure(padx=12, pady=10)

        self.mode_key = tk.StringVar(value="to_slide")
        self.input_path = tk.StringVar()
        self.out_dir = tk.StringVar(value=DEFAULT_OUT)
        self.busy = False

        self._build()
        self._on_mode_change()

        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.input_path.set(os.path.abspath(sys.argv[1]))
            self._guess_mode(sys.argv[1])

    def _build(self):
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="转换模式", font=("", 11, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        mode_box = ttk.Frame(frm)
        mode_box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        for key, meta in MODES.items():
            ttk.Radiobutton(
                mode_box, text=meta["label"], value=key,
                variable=self.mode_key, command=self._on_mode_change,
            ).pack(anchor="w", pady=1)

        self.hint = ttk.Label(frm, text="", foreground="#555")
        self.hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(frm, text="输入文件").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.input_path).grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=(0, 6))
        ttk.Button(frm, text="浏览…", command=self._pick_input, width=10).grid(
            row=4, column=2, sticky="e")

        ttk.Label(frm, text="输出目录").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm, textvariable=self.out_dir).grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=(0, 6))
        ttk.Button(frm, text="浏览…", command=self._pick_out, width=10).grid(
            row=6, column=2, sticky="e")

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=14)
        self.run_btn = ttk.Button(btn_row, text="开始转换", command=self._run)
        self.run_btn.pack(side="left")
        ttk.Button(btn_row, text="打开输出目录", command=self._open_out).pack(
            side="left", padx=8)
        ttk.Button(btn_row, text="清空日志", command=lambda: self.log.delete("1.0", "end")).pack(
            side="left")

        ttk.Label(frm, text="运行日志").grid(row=8, column=0, sticky="w")
        self.log = tk.Text(frm, height=16, wrap="word", font=("Consolas", 10))
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(frm, command=self.log.yview)
        scroll.grid(row=9, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(9, weight=1)

    def _on_mode_change(self):
        meta = MODES[self.mode_key.get()]
        self.hint.configure(text=meta["hint"])

    def _guess_mode(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mcz":
            self.mode_key.set("to_mmg")
        elif ext == ".mmg":
            self.mode_key.set("to_slide")
        self._on_mode_change()

    def _pick_input(self):
        meta = MODES[self.mode_key.get()]
        path = filedialog.askopenfilename(
            title="选择谱面",
            initialdir=HERE,
            filetypes=meta["filetypes"],
        )
        if path:
            self.input_path.set(path)

    def _pick_out(self):
        path = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.out_dir.get() or HERE,
        )
        if path:
            self.out_dir.set(path)

    def _open_out(self):
        out = self.out_dir.get().strip() or DEFAULT_OUT
        os.makedirs(out, exist_ok=True)
        os.startfile(out)

    def _append(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _run(self):
        if self.busy:
            return
        src = self.input_path.get().strip().strip('"')
        out = self.out_dir.get().strip() or DEFAULT_OUT
        meta = MODES[self.mode_key.get()]

        if not os.path.isfile(SCRIPT):
            messagebox.showerror("错误", f"找不到脚本：\n{SCRIPT}")
            return
        if not src or not os.path.isfile(src):
            messagebox.showwarning("提示", "请先选择有效的输入谱面文件")
            return

        os.makedirs(out, exist_ok=True)
        cmd = [
            sys.executable, "-X", "utf8", SCRIPT, src,
            "-o", out, "--to", meta["to"],
        ]

        self.busy = True
        self.run_btn.configure(state="disabled")
        self._append("=" * 60 + "\n")
        self._append(f"模式: {meta['label']}\n")
        self._append(f"命令: {' '.join(cmd)}\n\n")

        def worker():
            try:
                p = subprocess.Popen(
                    cmd, cwd=HERE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                )
                for line in p.stdout:
                    self.after(0, self._append, line)
                code = p.wait()
                self.after(0, self._append, f"\n退出码: {code}\n")
                if code == 0:
                    self.after(0, lambda: messagebox.showinfo(
                        "完成", f"转换成功\n输出目录：\n{out}"))
                else:
                    self.after(0, lambda: messagebox.showerror(
                        "失败", f"转换失败，退出码 {code}\n请查看日志"))
            except Exception as e:
                self.after(0, self._append, f"\n异常: {e}\n")
                self.after(0, lambda: messagebox.showerror("异常", str(e)))
            finally:
                def done():
                    self.busy = False
                    self.run_btn.configure(state="normal")
                self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()


def main():
    # 无图形环境时退回命令行菜单
    if os.environ.get("CHART_LAUNCHER_CLI") == "1" or (
            sys.platform.startswith("linux") and not os.environ.get("DISPLAY")):
        return cli_menu()
    app = App()
    app.mainloop()
    return 0


def cli_menu():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("谱面转编启动器")
    print("  1) MMG → Slide MC（推荐） [--to slide-mc]")
    print("  2) MMG → Slide MCZ        [--to slide-mcz]")
    print("  3) Slide MCZ → MMG        [--to mmg]")
    choice = input("选择模式 [1/2/3，默认1]: ").strip() or "1"
    key = {"1": "to_slide", "2": "to_mcz", "3": "to_mmg"}.get(choice, "to_slide")
    meta = MODES[key]
    src = input("输入文件路径: ").strip().strip('"')
    if not src or not os.path.isfile(src):
        print("无效输入文件")
        return 1
    out = input(f"输出目录 [{DEFAULT_OUT}]: ").strip() or DEFAULT_OUT
    os.makedirs(out, exist_ok=True)
    cmd = [sys.executable, "-X", "utf8", SCRIPT, src, "-o", out, "--to", meta["to"]]
    print("执行:", " ".join(cmd))
    return subprocess.call(cmd, cwd=HERE)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
