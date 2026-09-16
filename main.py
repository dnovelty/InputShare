import socket
import sys
import time
from typing import Callable

import adbutils
from adbutils import AdbInstallError
from multiprocessing import freeze_support
from server import deploy_reporter_server, deploy_scrcpy_server, scrcpy_receiver, reporter_receiver
from input import position_mapping
from input.callbacks import callback_context_wrapper
from input.controller import reset_controller_state, user_exit_event
from input.edge_portal import reset_portal_state
from ui.connecting_window import open_connecting_window
from ui.fullscreen_mask import reset_mask_events
from ui.tray import tray_thread_factory
from utils.adb_controller import ADBWiredConnectionError, append_adb_device, get_adb_client,\
    get_device_screen_size, get_last_wireless_addr, start_adb_server, try_connect_device
from utils.brightness_controller import restore_screen
from utils.config_manager import get_config
from utils.i18n import get_i18n
from utils.logger import LogType, LOGGER
from utils.network import get_ip_from_ip_port, scan_port
from utils.notification import Notification, send_notification

# 可重连错误类型：设备断开/网络中断等，可通过重建会话恢复
RECONNECTABLE = (
    ADBWiredConnectionError,                    # 有线设备未连接
    scrcpy_receiver.InvalidDummyByteException,  # scrcpy 握手失败
    adbutils.AdbError,                          # adb 命令失败（设备离线等）
    OSError,                                    # socket 断开/超时等所有网络类错误
)
RECONNECT_RETRY_INTERVAL_SEC = 2  # 重连轮询间隔

# 当前会话的 scrcpy 控制 socket（None=无连接）；托盘经 getter 动态读取以支持重连
socket_holder: list[socket.socket | None] = [None]

def get_client_socket() -> socket.socket | None:
    """托盘等长生命周期组件获取当前 socket 的统一入口。"""
    return socket_holder[0]

def close_notification_resolver(errno: Exception | None):
    close_notification = None
    i18n = get_i18n()
    match errno:
        case None: pass
        case ADBWiredConnectionError():
            close_notification = Notification(
                i18n(["ConnectionError", "连接错误"]),
                i18n(["Wired connection failed, please check if the device is connected correctly.", "有线连接失败，请检查是否正确连接设备。"]))
        case scrcpy_receiver.InvalidDummyByteException():
            close_notification = Notification(
                i18n(["NetworkError", "网络错误"]),
                i18n(["Connection with device failed, please retry.", "设备连接失败，请重试。"]))
        case AdbInstallError():
            close_notification = Notification(
                i18n(["NetworkError", "网络错误"]),
                i18n(["Android client installation failed, please retry.", "安卓客户端安装失败，请重试。"]))
        case socket.timeout | TimeoutError():
            close_notification = Notification(
                i18n(["NetworkError", "网络错误"]),
                i18n(["Connection with device timeout, please retry.", "设备连接超时，请重试。"]))
        case ConnectionAbortedError() | ConnectionResetError():
            close_notification = Notification(
                i18n(["NetworkError", "网络错误"]),
                i18n(["Unexpected connection aborted.", "连接意外中断。"]))
        case _:
            error_name = errno.__class__.__name__
            close_notification = Notification(
                i18n(["Error", "错误"]),
                i18n([f"Unknown error: {error_name}", f"未知错误：{error_name}"]))
    get_adb_client().server_kill()
    LOGGER.write(LogType.Adb, "ADB server killed.")
    LOGGER.write(LogType.Info, "Program terminated with: " + str(close_notification))
    send_notification(close_notification)

def reset_session_state():
    """会话重建前清理跨会话状态，避免上一会话残留影响新会话。"""
    reset_controller_state()
    reset_portal_state()
    reset_mask_events()

def wait_for_device_reconnect(is_wired_connection: bool) -> bool:
    """断开后轮询等待设备恢复可用；返回 False 表示用户经托盘取消重连。"""
    last_addr = get_last_wireless_addr()
    while True:
        # 用户主动退出（托盘）：取消重连
        if user_exit_event.is_set():
            LOGGER.write(LogType.Info, "Reconnect cancelled by user.")
            return False

        try:
            if is_wired_connection:
                # 有线：轮询设备列表，插回后即可恢复
                device_list = get_adb_client().device_list()
                if len(device_list) > 0:
                    append_adb_device(device_list[0])
                    LOGGER.write(LogType.Adb, "Wired device reconnected: " + str(device_list[0]))
                    return True
            elif last_addr is None:
                # 无线模式却无地址记录（异常情况），无法自动重连
                LOGGER.write(LogType.Error, "No wireless address recorded, cannot reconnect.")
                return False
            else:
                # 无线：优先按最近成功地址直连
                if try_connect_device(last_addr) is not None:
                    LOGGER.write(LogType.Adb, "Wireless device reconnected: " + last_addr)
                    return True
                # 无线调试重启后端口可能变化，按配置回退到端口扫描
                if get_config().scan_port:
                    ip = get_ip_from_ip_port(last_addr)
                    for port in scan_port(ip):
                        if try_connect_device(f"{ip}:{port}") is not None:
                            LOGGER.write(LogType.Adb, f"Wireless device reconnected: {ip}:{port}")
                            return True
        except Exception as e:
            # 探测过程中的 adb 异常视为设备未就绪，继续轮询
            LOGGER.write(LogType.Error, "Reconnect probe failed: " + str(e))
        time.sleep(RECONNECT_RETRY_INTERVAL_SEC)

def run_session() -> Exception | None:
    """运行单次完整会话（部署服务 → 输入主循环）。

    返回导致会话结束的异常；None 表示用户主动退出。
    """
    res = deploy_scrcpy_server()
    if isinstance(res, Exception):
        return res
    scrcpy_server_process, scrcpy_client_socket = res
    socket_holder[0] = scrcpy_client_socket
    restore_screen()  # 重连成功后恢复上次调暗的屏幕亮度（无调暗记录时为空操作）

    # 查询设备屏幕尺寸，用于贴边切换的按比例坐标映射
    device_size = get_device_screen_size()
    if isinstance(device_size, Exception):
        LOGGER.write(LogType.Error, "Get device screen size failed: " + str(device_size))
    else:
        position_mapping.set_android_screen_size(*device_size)

    stop_scrcpy_receiver = scrcpy_receiver.server_receiver_factory(scrcpy_client_socket)
    stop_reporter_receiver: Callable | None = None

    if get_config().edge_toggling:
        res = deploy_reporter_server()
        if isinstance(res, Exception):
            # reporter 部署失败：先释放本会话已创建的资源再返回
            stop_scrcpy_receiver()
            scrcpy_server_process.terminate()
            socket_holder[0] = None
            return res
        stop_reporter_receiver = reporter_receiver.server_receiver_factory()

    callbacks = callback_context_wrapper(scrcpy_client_socket)

    from input.controller import main_loop
    main_errno = main_loop(*callbacks)

    # 会话结束，释放本会话资源（屏幕亮度恢复统一放在最终退出处处理）
    LOGGER.write(LogType.Info, "Session terminated, closing...")
    stop_scrcpy_receiver()
    stop_reporter_receiver and stop_reporter_receiver() # type: ignore
    scrcpy_server_process.terminate()
    socket_holder[0] = None
    return main_errno

if __name__ == "__main__":
    freeze_support()

    start_adb_server()
    is_wired_connection = open_connecting_window()
    if is_wired_connection:
        device_list = get_adb_client().device_list()
        if len(device_list) == 0:
            # selected wired connection
            close_notification_resolver(ADBWiredConnectionError())
            sys.exit(1)
        append_adb_device(device_list[0])

    # 托盘全程存活：socket 经 getter 动态获取，重连后剪贴板发送自动恢复
    close_tray = tray_thread_factory(get_client_socket)

    # 监督循环：会话因可重连错误结束后，等待设备并重建会话
    main_errno: Exception | None = None
    while True:
        try:
            main_errno = run_session()
        except Exception as e:
            main_errno = e  # 未预期异常统一走退出/重连判定

        if main_errno is None:
            break  # 用户主动退出
        if not isinstance(main_errno, RECONNECTABLE):
            break  # 不可恢复错误，直接退出

        # 断开重连：通知 → 等待设备 → 清理旧会话状态 → 重建
        LOGGER.write(LogType.Error, "Connection lost, preparing to reconnect: " + str(main_errno))
        i18n = get_i18n()
        send_notification(Notification(
            i18n(["Reconnecting", "重连中"]),
            i18n(["Connection lost, reconnecting...", "连接已断开，正在尝试重连..."])))
        if not wait_for_device_reconnect(is_wired_connection):
            main_errno = None  # 用户取消重连，视为正常退出
            break
        # 等待期间旧会话后台线程的滞后退出请求已触发完毕，此处统一清理
        reset_session_state()

    LOGGER.write(LogType.Info, "Terminated, closing...")
    restore_screen()  # 退出前恢复手机屏幕亮度（若之前调暗过）
    close_notification_resolver(main_errno)
    close_tray()
