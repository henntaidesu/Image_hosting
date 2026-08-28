"""Windows packaged-app log window and system-tray controls."""

from __future__ import annotations

import queue
import re
import sys
import threading
from pathlib import Path
from typing import Callable

from . import config


APP_NAME = "Picture Bed"
_MAX_LINES = 5_000
_LOG_QUEUE: queue.Queue[str] = queue.Queue(maxsize=5_000)
_COMMAND_QUEUE: queue.Queue[str] = queue.Queue(maxsize=64)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_started = False
_window_ready = False
_on_exit: Callable[[], None] | None = None
_root = None
_text = None
_tray_icon = None


class _LogTee:
    """Forward stdout/stderr to the visible log window when frozen."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, base) -> None:
        self._base = base

    def write(self, content: str) -> int:
        if self._base is not None:
            try:
                self._base.write(content)
            except Exception:  # noqa: BLE001
                pass
        if content:
            try:
                _LOG_QUEUE.put_nowait(content)
            except queue.Full:
                pass
        return len(content)

    def flush(self) -> None:
        if self._base is not None:
            try:
                self._base.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str):
        if self._base is None:
            raise AttributeError(name)
        return getattr(self._base, name)


def start(on_exit: Callable[[], None]) -> bool:
    """Open the log window and begin redirecting process output to it."""
    global _on_exit, _started
    if _started or sys.platform != "win32":
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001
        return False

    _started = True
    _on_exit = on_exit
    _install_log_tee()
    threading.Thread(target=_ui_main, name="picture-bed-log-window", daemon=True).start()
    return True


def show_window() -> bool:
    return _send_command("show")


def hide_window() -> bool:
    return _send_command("hide")


def start_tray(on_exit: Callable[[], None]) -> bool:
    """Start the Windows notification-area icon used to restore the window."""
    global _tray_icon
    if _tray_icon is not None or sys.platform != "win32":
        return _tray_icon is not None
    try:
        import pystray
        from PIL import Image
    except Exception:  # noqa: BLE001
        return False

    try:
        image = Image.open(_icon_path())
    except Exception:  # noqa: BLE001
        image = Image.new("RGBA", (64, 64), (45, 117, 214, 255))

    def show(icon, item) -> None:  # noqa: ANN001
        show_window()

    def hide(icon, item) -> None:  # noqa: ANN001
        hide_window()

    def exit_app(icon, item) -> None:  # noqa: ANN001
        stop_tray()
        on_exit()

    menu = pystray.Menu(
        pystray.MenuItem("显示运行窗口", show, default=True),
        pystray.MenuItem("收至任务栏", hide),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出程序", exit_app),
    )
    _tray_icon = pystray.Icon("PictureBed", image, APP_NAME, menu)
    _tray_icon.run_detached()
    return True


def stop_tray() -> None:
    global _tray_icon
    icon, _tray_icon = _tray_icon, None
    if icon is None:
        return
    try:
        icon.visible = False
        icon.stop()
    except Exception:  # noqa: BLE001
        pass


def notify_tray(message: str) -> None:
    if _tray_icon is None:
        return
    try:
        _tray_icon.notify(message, APP_NAME)
    except Exception:  # noqa: BLE001
        pass


def _install_log_tee() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if not isinstance(stream, _LogTee):
            setattr(sys, name, _LogTee(stream))


def _icon_path() -> Path:
    return config.ASSET_DIR / "static" / "app-icon.png"


def _send_command(command: str) -> bool:
    if not _window_ready:
        return False
    try:
        _COMMAND_QUEUE.put_nowait(command)
    except queue.Full:
        return False
    return True


def _ui_main() -> None:
    global _root, _text, _window_ready
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(f"{APP_NAME} - 运行日志")
        root.geometry("900x560")
        root.minsize(540, 320)
        _apply_window_icon(root)

        text = tk.Text(
            root,
            wrap="none",
            state="disabled",
            bg="#11151c",
            fg="#d6dde8",
            insertbackground="#d6dde8",
            selectbackground="#2f5d8a",
            font=("Consolas", 10),
            borderwidth=0,
            highlightthickness=0,
        )
        y_scroll = tk.Scrollbar(root, orient="vertical", command=text.yview)
        x_scroll = tk.Scrollbar(root, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        root.protocol("WM_DELETE_WINDOW", _on_close_clicked)

        _root, _text = root, text
        _window_ready = True
        root.after(80, _pump)
        root.mainloop()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _window_ready = False
        _root = None
        _text = None


def _apply_window_icon(root) -> None:  # noqa: ANN001
    try:
        import tkinter as tk

        root._icon_ref = tk.PhotoImage(file=str(_icon_path()))
        root.iconphoto(True, root._icon_ref)
    except Exception:  # noqa: BLE001
        pass


def _pump() -> None:
    root, text = _root, _text
    if root is None or text is None:
        return
    try:
        while True:
            command = _COMMAND_QUEUE.get_nowait()
            if command == "show":
                root.deiconify()
                root.lift()
                root.focus_force()
            elif command == "hide":
                root.withdraw()
    except queue.Empty:
        pass

    chunks: list[str] = []
    try:
        for _ in range(400):
            chunks.append(_LOG_QUEUE.get_nowait())
    except queue.Empty:
        pass
    if chunks:
        _append_log(text, _ANSI_RE.sub("", "".join(chunks)))
    root.after(80, _pump)


def _append_log(text, content: str) -> None:  # noqa: ANN001
    at_bottom = text.yview()[1] >= 0.999
    text.configure(state="normal")
    text.insert("end", content)
    lines = int(text.index("end-1c").split(".")[0])
    if lines > _MAX_LINES:
        text.delete("1.0", f"{lines - _MAX_LINES + 1}.0")
    text.configure(state="disabled")
    if at_bottom:
        text.see("end")


def _on_close_clicked() -> None:
    root = _root
    if root is None:
        return
    choice = _ask_close_action(root)
    if choice == "tray":
        root.withdraw()
        notify_tray("程序仍在后台运行。双击右下角图标可恢复运行窗口。")
    elif choice == "exit":
        root.withdraw()
        if _on_exit is not None:
            _on_exit()


def _ask_close_action(root) -> str:  # noqa: ANN001
    import tkinter as tk

    result = {"value": "cancel"}
    dialog = tk.Toplevel(root)
    dialog.title(f"关闭 {APP_NAME}")
    dialog.resizable(False, False)
    dialog.transient(root)

    def choose(value: str) -> None:
        result["value"] = value
        dialog.destroy()

    body = tk.Frame(dialog, padx=20, pady=16)
    body.pack(fill="both", expand=True)
    tk.Label(body, text="确定要关闭 Picture Bed 吗？", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    tk.Label(
        body,
        text="收至任务栏：隐藏运行窗口，服务继续在后台运行。\n"
        "退出程序：停止图片床服务并完全退出。",
        justify="left",
        font=("Microsoft YaHei UI", 9),
    ).pack(anchor="w", pady=(8, 16))

    buttons = tk.Frame(body)
    buttons.pack(anchor="e")
    tk.Button(buttons, text="收至任务栏", width=12, command=lambda: choose("tray")).pack(side="left")
    tk.Button(buttons, text="退出程序", width=12, command=lambda: choose("exit")).pack(side="left", padx=8)
    tk.Button(buttons, text="取消", width=8, command=lambda: choose("cancel")).pack(side="left")

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
    dialog.bind("<Escape>", lambda event: choose("cancel"))
    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_width()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_height()) // 3
    dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    dialog.grab_set()
    root.wait_window(dialog)
    return result["value"]
