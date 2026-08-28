"""Waitress and packaged Windows UI lifecycle."""

import sys
import threading

from waitress import create_server

from . import config


def run_server(app):
    desktop_ui = None
    server_ref = {"server": None}
    shutting_down = threading.Event()

    def shutdown():
        if shutting_down.is_set():
            return
        shutting_down.set()
        print("[Picture Bed] 正在停止服务...")
        if desktop_ui is not None:
            desktop_ui.stop_tray()
        server = server_ref["server"]
        if server is not None:
            try:
                server.close()
            except Exception as error:  # noqa: BLE001
                print(f"[Picture Bed] 停止服务时发生错误：{error}")

    if getattr(sys, "frozen", False) and sys.platform == "win32":
        from . import desktop_ui as packaged_ui

        desktop_ui = packaged_ui
        desktop_ui.start(on_exit=shutdown)
        desktop_ui.start_tray(on_exit=shutdown)

    print("[Picture Bed] 正在启动图片床服务...")
    try:
        server = create_server(app, host=config.SERVER_HOST, port=config.SERVER_PORT, threads=4)
    except OSError as error:
        print(f"[Picture Bed] 启动失败：无法监听 {config.SERVER_HOST}:{config.SERVER_PORT}（{error}）")
        print(f"[Picture Bed] 请确认没有其他程序正在使用端口 {config.SERVER_PORT}。")
        return

    server_ref["server"] = server
    if shutting_down.is_set():
        server.close()
        return

    print(f"[Picture Bed] 服务已启动：http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    print(f"[Picture Bed] 本机访问：http://127.0.0.1:{config.SERVER_PORT}")
    print("[Picture Bed] 公网访问请使用 HTTPS 反向代理，以保证登录 Cookie 安全。")
    print("[Picture Bed] 关闭运行窗口时可选择收至任务栏或退出程序。")
    try:
        server.run()
    finally:
        if desktop_ui is not None:
            desktop_ui.stop_tray()
        print("[Picture Bed] 服务已停止。")
