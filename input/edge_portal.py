import time
import threading
import pynput

from typing import Callable
from input import position_mapping
from server.reporter_receiver import DevicePosition
from utils import VoidCallable, screen_size
from utils.config_manager import get_config
from utils.logger import LOGGER, LogType

EDGE_PORTAL_LOOP_INTERVAL_SEC = 1 / 1000

# --- Directions, mirroring deskflow's `Direction` enum ---
class EdgeDirection:
    NONE   = "none"
    LEFT   = "left"
    RIGHT  = "right"
    TOP    = "top"
    BOTTOM = "bottom"

# --- Corner masks, mirroring deskflow's corner masks (s_topLeftCornerMask, ...) ---
class Corner:
    NONE          = 0x00
    TOP_LEFT      = 0x01
    TOP_RIGHT     = 0x02
    BOTTOM_LEFT   = 0x04
    BOTTOM_RIGHT  = 0x08
    ALL           = TOP_LEFT | TOP_RIGHT | BOTTOM_LEFT | BOTTOM_RIGHT

screen_width, screen_height = screen_size()
mouse_controller = pynput.mouse.Controller()
pause_event = threading.Event()
close_event = threading.Event()
pause_edge_toggling_event = threading.Event()
edge_portal_passing_event = threading.Event()

edge_toggling_callbacks = []
def append_edge_toggling_callback(callback: Callable):
    global edge_toggling_callbacks
    edge_toggling_callbacks.append(callback)
def call_edge_toggling_callbacks():
    # this should be called after toggled by android client
    global edge_toggling_callbacks
    for callback in edge_toggling_callbacks: callback()

# 贴边切换前记录的 PC 光标位置（用于回位）
cursor_pos_before_toggling: tuple[int, int] | None = None

def return_cursor_to_pc():
    """把 Android 光标位置按比例映射回 PC（由手机切回/关闭共享时调用）。"""
    global cursor_pos_before_toggling
    SIDE_MARGIN = 2
    if cursor_pos_before_toggling is None: return
    android_w, android_h = position_mapping.get_android_screen_size()
    if android_w > 0 and android_h > 0:
        # 按比例把 Android 光标的退出位置映射回 PC 屏幕
        ax, ay = position_mapping.get_android_cursor()
        pc_x, pc_y = position_mapping.map_android_to_pc(
            ax, ay, device_direction,
            screen_width, screen_height, android_w, android_h, SIDE_MARGIN)
        mouse_controller.position = (pc_x, pc_y)
    else:
        # 退化方案：回到贴边切换前的位置（向内偏移，避免贴在边上再次触发）
        temp_x, temp_y = cursor_pos_before_toggling
        if   is_device_at_right : mouse_controller.position = (temp_x - SIDE_MARGIN, temp_y)
        elif is_device_at_left  : mouse_controller.position = (SIDE_MARGIN, temp_y)
        elif is_device_at_top   : mouse_controller.position = (temp_x, SIDE_MARGIN)
        elif is_device_at_bottom: mouse_controller.position = (temp_x, temp_y - SIDE_MARGIN)

config = get_config()

is_edge_toggling_enabled = config.edge_toggling
device_position     = config.device_position
trigger_margin      = config.trigger_margin   # deskflow `switchCornerSize` (corner dead-zone size)
switch_delay        = config.switch_delay     # deskflow `switchDelay` (ms), 0 = switch immediately

is_device_at_top    = device_position == DevicePosition.TOP
is_device_at_right  = device_position == DevicePosition.RIGHT
is_device_at_bottom = device_position == DevicePosition.BOTTOM
is_device_at_left   = device_position == DevicePosition.LEFT

# the direction that has a "neighbor" (the single Android device)
device_direction = (
    EdgeDirection.TOP    if is_device_at_top    else
    EdgeDirection.RIGHT  if is_device_at_right  else
    EdgeDirection.BOTTOM if is_device_at_bottom else
    EdgeDirection.LEFT
)

def pause_edge_toggling():
    LOGGER.write(LogType.Info, "Edge toggling paused.")
    pause_edge_toggling_event.set()
def resume_edge_toggling():
    LOGGER.write(LogType.Info, "Edge toggling resumed.")
    pause_edge_toggling_event.clear()

def reset_portal_state():
    """断开重连、重建会话前重置贴边切换模块状态。

    回调列表必须清空：每个会话的 create_edge_portal 都会注册回位回调，
    不清理会导致回调重复累积、多次触发。
    """
    global edge_toggling_callbacks, cursor_pos_before_toggling
    close_event.clear()
    pause_event.set()
    pause_edge_toggling_event.clear()
    edge_portal_passing_event.clear()
    edge_toggling_callbacks = []
    cursor_pos_before_toggling = None

def get_corner(x: int, y: int, size: int) -> int:
    """Port of deskflow `Server::getCorner` (src/lib/server/Server.cpp).

    Returns the corner mask the cursor is in, or `Corner.NONE`. `size` is the
    corner dead-zone size (deskflow's `switchCornerSize`).
    """
    x_side = -1 if x <= 0 else (1 if x >= screen_width - 1 else 0)
    y_side = -1 if y <= 0 else (1 if y >= screen_height - 1 else 0)

    if x_side != 0:
        if y < size:
            return Corner.TOP_LEFT if x_side < 0 else Corner.TOP_RIGHT
        elif y >= screen_height - size:
            return Corner.BOTTOM_LEFT if x_side < 0 else Corner.BOTTOM_RIGHT

    if y_side != 0:
        if x < size:
            return Corner.TOP_LEFT if y_side < 0 else Corner.BOTTOM_LEFT
        elif x >= screen_width - size:
            return Corner.TOP_RIGHT if y_side < 0 else Corner.BOTTOM_RIGHT

    return Corner.NONE

def create_edge_portal():
    from input.controller import schedule_toggle as main_schedule_toggle,\
                                 is_share_enabled

    def return_to_before_toggling():
        # 由 reporter TOGGLE 事件触发；Android 端暂停贴边切换时不回位
        if pause_edge_toggling_event.is_set(): return
        return_cursor_to_pc()  # 复用模块级回位逻辑（controller.py 关闭共享时也调用）
    append_edge_toggling_callback(return_to_before_toggling)

    # --- switch state machine (port of Server::m_switchDir / m_switchWaitTimer) ---
    switch_dir = EdgeDirection.NONE
    switch_wait_start = None

    def stop_switch():
        nonlocal switch_dir, switch_wait_start
        switch_dir = EdgeDirection.NONE
        switch_wait_start = None

    def start_switch_wait():
        nonlocal switch_wait_start
        switch_wait_start = time.perf_counter()

    def is_switch_wait_started() -> bool:
        return switch_wait_start is not None

    def switch_to_device(pos: tuple[int, int]):
        global cursor_pos_before_toggling  # 变量为模块级，须用 global 绑定
        cursor_pos_before_toggling = pos
        # fraction-map the PC cursor's exit position to an Android entry position
        # and warp the Android pointer there (deskflow switchScreen/enter + mapToPixel).
        # then move it inward so it does not sit on the return-edge strip, which
        # would immediately re-trigger a switch-back (deskflow avoidJumpZone).
        android_w, android_h = position_mapping.get_android_screen_size()
        if android_w > 0 and android_h > 0:
            ax, ay = position_mapping.map_pc_to_android(
                pos[0], pos[1], device_direction,
                screen_width, screen_height, android_w, android_h)
            ax, ay = position_mapping.avoid_jump_zone(
                ax, ay, device_direction, android_w, android_h)
            position_mapping.warp_android_pointer(ax, ay)
        main_schedule_toggle(True)

    def is_switch_okay(dir: str, x: int, y: int) -> bool:
        """Port of deskflow `Server::isSwitchOkay` (src/lib/server/Server.cpp).

        Returns `True` when the switch should happen immediately, otherwise
        updates the switch state machine (wait / stop) and returns `False`.
        """
        nonlocal switch_dir

        # no neighbor in this direction -> don't switch, don't try to switch later
        if dir != device_direction:
            stop_switch()
            return False

        # note if the switch direction has changed
        is_new_direction = (dir != switch_dir)
        if is_new_direction or switch_dir == EdgeDirection.NONE:
            switch_dir = dir

        prevent_switch = False

        # if waiting before a switch then prepare to switch later
        if switch_delay > 0:
            if is_new_direction or not is_switch_wait_started():
                start_switch_wait()
            prevent_switch = True

        # locked corner? (deskflow: getCorner & switchCorners, switchCornerSize)
        if trigger_margin > 0 and get_corner(x, y, trigger_margin) != Corner.NONE:
            prevent_switch = True
            stop_switch()

        # locked to screen (deskflow: isLockedToScreen)
        if pause_edge_toggling_event.is_set():
            prevent_switch = True
            stop_switch()

        return not prevent_switch

    while not close_event.is_set():
        temp_pos = mouse_controller.position
        if temp_pos is None:
            # since the value of `mouse_controller.position` may be None sometimes,
            # make it a check here.
            # see this issue: https://github.com/moses-palmer/pynput/issues/559
            time.sleep(EDGE_PORTAL_LOOP_INTERVAL_SEC)
            continue
        x, y = temp_pos

        if pause_event.is_set():
            # --- input NOT redirected: PC -> Android switch (deskflow onMouseMovePrimary) ---
            # 贴边切换需要：贴边功能开启 且 键鼠共享开启 且 未被手机端暂停
            if not is_edge_toggling_enabled or not is_share_enabled() \
                    or pause_edge_toggling_event.is_set():
                time.sleep(EDGE_PORTAL_LOOP_INTERVAL_SEC)
                continue

            # jump-zone detection (deskflow jump zone == 1px, i.e. "at the edge")
            dirh = EdgeDirection.NONE
            dirv = EdgeDirection.NONE
            if x <= 0:
                dirh = EdgeDirection.LEFT
            elif x >= screen_width - 1:
                dirh = EdgeDirection.RIGHT
            if y <= 0:
                dirv = EdgeDirection.TOP
            elif y >= screen_height - 1:
                dirv = EdgeDirection.BOTTOM

            if dirh == EdgeDirection.NONE and dirv == EdgeDirection.NONE:
                # still on local screen -> cancel any pending switch (deskflow noSwitch)
                stop_switch()
                time.sleep(EDGE_PORTAL_LOOP_INTERVAL_SEC)
                continue

            # in a corner there may be a neighbor both horizontally and vertically,
            # so check both directions (deskflow onMouseMovePrimary)
            switched = False
            for dir in (dirh, dirv):
                if dir == EdgeDirection.NONE:
                    continue
                if is_switch_okay(dir, x, y):
                    switch_to_device((x, y))
                    stop_switch()
                    switched = True
                    break

            # switch-wait timeout (deskflow handleSwitchWaitTimeout)
            if not switched and switch_dir != EdgeDirection.NONE \
                    and switch_delay > 0 and is_switch_wait_started():
                if time.perf_counter() - switch_wait_start >= switch_delay / 1000.0:
                    switch_to_device((x, y))
                    stop_switch()
        else:
            # --- input redirected: wrap-around portal (keep cursor from getting stuck) ---
            is_at_left_side = x <= 0
            is_at_right_side = x >= screen_width - 1
            is_at_top_side = y <= 0
            is_at_bottom_side = y >= screen_height - 1
            if is_at_left_side or is_at_right_side or is_at_top_side or is_at_bottom_side:
                edge_portal_passing_event.set()
            if is_at_left_side:
                mouse_controller.move(screen_width - 1, 0)
            if is_at_right_side:
                mouse_controller.move(1 - screen_width, 0)
            if is_at_top_side:
                mouse_controller.move(0, screen_height - 1)
            if is_at_bottom_side:
                mouse_controller.move(0, 1 - screen_height)

        time.sleep(EDGE_PORTAL_LOOP_INTERVAL_SEC)
    LOGGER.write(LogType.Info, "Edge portal closed.")

def edge_portal_thread_factory() -> tuple[
    VoidCallable, VoidCallable, VoidCallable
]:
    def start_edge_portal():
        pause_event.clear()
    def pause_edge_portal():
        pause_event.set()
    def close_edge_portal():
        close_event.set()

    pause_event.set()
    threading.Thread(target=create_edge_portal, daemon=True).start()
    return start_edge_portal, pause_edge_portal, close_edge_portal
