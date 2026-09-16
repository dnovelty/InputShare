import socket
import threading
import pystray

from PIL import Image
from typing import Callable

from input.controller import is_share_enabled, schedule_share_toggle,\
                             schedule_exit as main_schedule_exit
from scrcpy_client.clipboard_event import SetClipboardEvent
from ui import ICON_ICO_PATH
from ui.settings import open_settings_window
from utils import VoidCallable
from utils.config_manager import get_config
from utils.i18n import get_i18n
from utils.clipboard import Clipboard
from utils.logger import LOGGER, LogType

Menu = pystray.Menu
MenuItem = pystray.MenuItem

tray = None

def create_tray(socket_getter: Callable[[], socket.socket | None]):
    global tray

    def send_clipboard_text():
        # 每次点击时动态获取当前 socket，重连后自动使用新连接
        client_socket = socket_getter()
        if client_socket is None:
            LOGGER.write(LogType.Info, "No active device connection, skip sending clipboard.")
            return
        current_clipboard_content = Clipboard.safe_paste()
        if current_clipboard_content is None:
            return
        event = SetClipboardEvent(current_clipboard_content)
        try:
            client_socket.sendall(event.serialize())
        except Exception as e:
            # 发送失败可能正处于重连阶段，仅记录日志，不退出程序
            LOGGER.write(LogType.Error, "Send data error: " + str(e))

    def toggle_share_keyboard_only(_, item: MenuItem):
        get_config().share_keyboard_only = not item.checked
    def toggle_sync_clipboard(_, item: MenuItem):
        get_config().sync_clipboard = not item.checked
    def toggle_share_enabled():
        # 切换键鼠共享总开关（允许贴边切换/调暗亮度等）
        schedule_share_toggle()

    def exit_tray():
        global tray
        main_schedule_exit()
        assert tray is not None
        tray.stop()

    i18n = get_i18n()
    tray_img = Image.open(ICON_ICO_PATH)
    tray_menu = Menu(
        MenuItem(
            i18n(["Enable sharing", "开启键鼠共享"]),
            action=toggle_share_enabled,
            checked=lambda _: is_share_enabled()),  # 开启时打勾，关闭时去勾
        MenuItem(
            i18n(["Send clipboard text", "发送当前剪贴板文本"]),
            action=send_clipboard_text),
        Menu.SEPARATOR,
        MenuItem(
            i18n(["Share keyboard only", "仅共享键盘"]),
            action=toggle_share_keyboard_only,
            checked=lambda _: get_config().share_keyboard_only),
        MenuItem(
            i18n(["Sync clipboard", "同步剪贴板"]),
            action=toggle_sync_clipboard,
            checked=lambda _: get_config().sync_clipboard),
        Menu.SEPARATOR,
        MenuItem(
            i18n(["Settings", "设置"]),
            action=open_settings_window),
        MenuItem(
            i18n(["Exit", "退出"]),
            action=exit_tray),
    )
    tray = pystray.Icon(
        "InputShare",
        title=i18n(["InputShare", "输入流转"]),
        icon=tray_img,
        menu=tray_menu,
    )
    LOGGER.write(LogType.Info, "Tray started.")
    tray.run()

def tray_thread_factory(socket_getter: Callable[[], socket.socket | None]) -> VoidCallable:
    def close_tray():
        global tray
        if tray is not None: tray.stop()
        LOGGER.write(LogType.Info, "Tray stopped.")

    thread = threading.Thread(
        target=create_tray,
        args=[socket_getter],
        daemon=True,
    )
    thread.start()
    return close_tray
