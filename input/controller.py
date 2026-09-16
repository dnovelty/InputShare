import threading
import time
from pynput import keyboard, mouse
from input import EXIT_KEY_COMBINATION, SWITCH_KEY_COMBINATION
from scrcpy_client.clipboard_event import GetClipboardEvent, SetClipboardEvent
from scrcpy_client.hid_event import KeyEmptyEvent
from input.callbacks import KeyEventCallback,\
    MouseClickCallback, MouseMoveCallback, MouseScrollCallback,\
    SendDataCallback, SendDataAsyncCallback
from input.edge_portal import edge_portal_thread_factory, return_cursor_to_pc
from server.scrcpy_receiver import ReceivedClipboardText
from ui.fullscreen_mask import mask_thread_factory
from utils.brightness_controller import dim_screen, restore_screen
from utils.clipboard import Clipboard
from utils.config_manager import get_config
from utils.logger import LOGGER, LogType

is_redirecting = False
share_enabled = True  # 键鼠共享总开关：开启时允许贴边切换，关闭时禁止并强制切回电脑
keyboard_controller = keyboard.Controller()
toggle_event = threading.Event()
exit_event = threading.Event()
user_exit_event = threading.Event()  # 用户主动退出（热键/托盘），与错误退出区分，供重连等待时判断
main_errno: Exception | None = None

last_toggling_time  = time.perf_counter()
DEBOUNCING_DURATION = 0.25
def schedule_toggle(force: bool | None = None):
    global toggle_event, is_redirecting, last_toggling_time
    current_time = time.perf_counter()
    if current_time - last_toggling_time < DEBOUNCING_DURATION:
        LOGGER.write(LogType.Info, "Toggling debounced."); return
    last_toggling_time = current_time

    if force is None:
        # triggered by hotkey
        keyboard_controller.release(keyboard.Key.ctrl)
        keyboard_controller.release(keyboard.Key.alt)
    is_redirecting = force if force is not None else not is_redirecting
    toggle_event.set()

def schedule_exit(errno: Exception | None = None):
    global exit_event, main_errno
    # 退出流程已启动则忽略重复/迟到的退出请求（会话结束后台线程的滞后报错）
    if exit_event.is_set(): return
    if errno is None:
        user_exit_event.set()  # 标记为用户主动退出
    else:
        main_errno = errno
    schedule_toggle()
    exit_event.set()

def reset_controller_state():
    """断开重连、重建会话前重置控制器状态，避免上一会话残留影响新会话。"""
    global is_redirecting, share_enabled, main_errno, last_toggling_time
    is_redirecting = False
    share_enabled = True
    main_errno = None
    last_toggling_time = time.perf_counter()
    toggle_event.clear()
    exit_event.clear()
    user_exit_event.clear()

def is_share_enabled() -> bool:
    """返回键鼠共享是否开启。"""
    return share_enabled

def schedule_share_toggle(force: bool | None = None):
    """切换键鼠共享总开关（托盘菜单与 ctrl+alt+s 热键入口）。

    关闭时：禁止贴边切换，若正在控制手机则强制切回电脑并回位光标，
    同时按配置调暗手机屏幕；开启时：恢复手机屏幕亮度。
    """
    global share_enabled, is_redirecting, toggle_event, last_toggling_time
    current_time = time.perf_counter()
    if current_time - last_toggling_time < DEBOUNCING_DURATION:
        LOGGER.write(LogType.Info, "Share toggling debounced."); return
    last_toggling_time = current_time

    if force is None:
        # 热键触发：释放可能按住的 ctrl/alt，避免按键卡住
        keyboard_controller.release(keyboard.Key.ctrl)
        keyboard_controller.release(keyboard.Key.alt)

    new_state = (not share_enabled) if force is None else bool(force)
    if new_state == share_enabled: return  # 状态未变化，无需处理
    share_enabled = new_state

    if new_state:
        restore_screen()  # 开启共享：恢复手机亮度
    else:
        # 关闭共享：若正在控制手机则强制切回电脑
        # （直接置状态并唤醒主循环，避免被 schedule_toggle 的防抖拦截）
        if is_redirecting:
            is_redirecting = False
            toggle_event.set()
            return_cursor_to_pc()  # 把 Android 光标位置映射回 PC
        # 按配置调暗手机屏幕
        if get_config().dim_screen_when_disabled:
            dim_screen()

switch_hotkey = keyboard.HotKey(keyboard.HotKey.parse(SWITCH_KEY_COMBINATION), schedule_share_toggle)
exit_hotkey = keyboard.HotKey(keyboard.HotKey.parse(EXIT_KEY_COMBINATION), schedule_exit)

def keyboard_press_handler_factory(callback: KeyEventCallback):
    def keyboard_press_handler(k: keyboard.Key | keyboard.KeyCode | None):
        global is_redirecting, keyboard_listener
        assert keyboard_listener is not None
        if k is None: return

        canonical_k = keyboard_listener.canonical(k)
        switch_hotkey.press(canonical_k)
        exit_hotkey.press(canonical_k)
        callback(canonical_k, is_redirecting)
    return keyboard_press_handler

def keyboard_release_handler_factory(callback: KeyEventCallback):
    def keyboard_release_handler(k: keyboard.Key | keyboard.KeyCode | None):
        global is_redirecting, keyboard_listener
        assert keyboard_listener is not None
        if k is None: return

        canonical_k = keyboard_listener.canonical(k)
        switch_hotkey.release(canonical_k)
        exit_hotkey.release(canonical_k)
        callback(canonical_k, is_redirecting)
    return keyboard_release_handler

def mouse_move_handler_factory(callback: MouseMoveCallback):
    def mouse_move_handler(x: int, y: int):
        global is_redirecting
        callback(x, y, is_redirecting)
    return mouse_move_handler

def mouse_click_handler_factory(callback: MouseClickCallback):
    def mouse_click_handler(x: int, y: int, button: mouse.Button, pressed: bool):
        global is_redirecting, main_errno
        callback(x, y, button, pressed, is_redirecting)
    return mouse_click_handler

def mouse_scroll_handler_factory(callback: MouseScrollCallback):
    def mouse_scroll_handler(x: int, y: int, dx: int, dy: int):
        global is_redirecting, main_errno
        callback(x, y, dx, dy, is_redirecting)
    return mouse_scroll_handler

def main_loop(
    send_data: SendDataCallback,
    send_data_async: SendDataAsyncCallback,
    keyboard_press_callback: KeyEventCallback,
    keyboard_release_callback: KeyEventCallback,
    mouse_move_callback: MouseMoveCallback,
    mouse_click_callback: MouseClickCallback,
    mouse_scroll_callback: MouseScrollCallback,
) -> Exception | None:
    global is_redirecting, keyboard_listener, main_errno, toggle_event

    def before_toggle(is_redirecting: bool):
        if is_redirecting:
            last_received = ReceivedClipboardText.read()
            current_clipboard_content = Clipboard.safe_paste()
            if exit_event.is_set(): return
            if not get_config().sync_clipboard: return
            if current_clipboard_content is None: return
            if last_received is not None and\
               last_received == current_clipboard_content: return
            send_data_async(SetClipboardEvent(current_clipboard_content).serialize())

    def after_toggle(is_redirecting: bool):
        nonlocal show_mask, hide_mask,\
            start_edge_portal, pause_edge_portal
        if is_redirecting:
            if not get_config().share_keyboard_only:
                show_mask(); start_edge_portal()
            LOGGER.write(LogType.Info, "Input redirecting enabled.")
        else:
            send_data(KeyEmptyEvent().serialize())
            hide_mask(); pause_edge_portal()
            LOGGER.write(LogType.Info, "Input redirecting disabled.")

    main_errno = send_data(GetClipboardEvent().serialize()) # start server clipboard sync
    show_mask, hide_mask, exit_mask = mask_thread_factory()
    start_edge_portal, pause_edge_portal, close_edge_portal = edge_portal_thread_factory()

    keyboard_listener = None
    mouse_listener = mouse.Listener(
        on_move=mouse_move_handler_factory(mouse_move_callback),
        on_click=mouse_click_handler_factory(mouse_click_callback),
        on_scroll=mouse_scroll_handler_factory(mouse_scroll_callback),
    )
    mouse_listener.start()
    while not exit_event.is_set() and main_errno is None:
        if (res := after_toggle(is_redirecting)) is not None:
            main_errno = res; break

        keyboard_listener = keyboard.Listener(
            suppress=is_redirecting,
            on_press=keyboard_press_handler_factory(keyboard_press_callback),
            on_release=keyboard_release_handler_factory(keyboard_release_callback),
        )
        keyboard_listener.start()
        toggle_event.wait()
        toggle_event.clear()
        before_toggle(is_redirecting)
        keyboard_listener.stop()

    mouse_listener.stop()
    exit_mask()
    close_edge_portal()
    return main_errno
