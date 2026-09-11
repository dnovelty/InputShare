"""手机屏幕亮度控制：通过 adb shell 读写系统亮度设置。

用于"关闭键鼠共享时调暗手机屏幕"功能；所有 adb 操作均捕获异常，
避免 adb 失败影响主流程。
"""
from utils.adb_controller import get_adb_device
from utils.config_manager import get_config
from utils.logger import LOGGER, LogType

# Android 传统亮度最大值（settings system screen_brightness 范围 0-255）
BRIGHTNESS_MAX = 255
# 系统亮度滑块是感知曲线（gamma≈2.2，已实测验证）：配置百分比需按同一曲线
# 换算，才能与滑块位置/体感一致，否则"5%"实际对应滑块约 28% 的亮度
BRIGHTNESS_GAMMA = 2.2

# 调暗前记录的原始亮度与自动亮度模式（None 表示尚未记录）
_original_brightness: int | None = None
_original_auto_mode: str | None = None

def _shell(device, command: str) -> str:
    """执行 adb shell 命令并返回去除空白后的输出。"""
    return str(device.shell(command)).strip()

def dim_screen():
    """调暗手机屏幕：记住当前亮度后降到配置的百分比。"""
    global _original_brightness, _original_auto_mode
    device = get_adb_device()
    if isinstance(device, Exception):
        LOGGER.write(LogType.Error, "Dim screen failed: no adb device.")
        return

    # 目标亮度：百分比按感知曲线（gamma≈2.2）换算，与系统滑块位置/体感一致，最低保 1
    dim_percent = get_config().dim_brightness
    ratio = min(max(dim_percent, 0), 100) / 100
    target = max(1, round(BRIGHTNESS_MAX * ratio ** BRIGHTNESS_GAMMA))
    try:
        # 仅首次记录原亮度，避免反复关闭时覆盖真实值
        if _original_brightness is None:
            output = _shell(device, "settings get system screen_brightness")
            if output.isdigit():
                _original_brightness = int(output)
        # 仅首次记录自动亮度模式（1=开启 0=关闭）
        if _original_auto_mode is None:
            _original_auto_mode = _shell(device, "settings get system screen_brightness_mode")
        # 当前亮度已低于目标时不调亮（用户原本开得很暗，保持原值）
        if _original_brightness is not None:
            target = min(target, _original_brightness)
        # 关闭自动亮度，否则手动设置亮度不生效
        _shell(device, "settings put system screen_brightness_mode 0")
        _shell(device, f"settings put system screen_brightness {target}")
        LOGGER.write(LogType.Info, f"Screen dimmed to {dim_percent}% ({target}/255, original {_original_brightness}).")
    except Exception as e:
        LOGGER.write(LogType.Error, "Dim screen failed: " + str(e))

def restore_screen():
    """恢复手机屏幕亮度（若之前调暗过）。"""
    global _original_brightness, _original_auto_mode
    if _original_brightness is None:
        return  # 未调暗过，无需恢复
    device = get_adb_device()
    if isinstance(device, Exception):
        LOGGER.write(LogType.Error, "Restore screen failed: no adb device.")
        return
    try:
        _shell(device, f"settings put system screen_brightness {_original_brightness}")
        # 恢复自动亮度模式
        if _original_auto_mode in ("0", "1"):
            _shell(device, f"settings put system screen_brightness_mode {_original_auto_mode}")
        LOGGER.write(LogType.Info, "Screen brightness restored.")
    except Exception as e:
        LOGGER.write(LogType.Error, "Restore screen failed: " + str(e))
    finally:
        # 用后清空记录，下次调暗时重新记录
        _original_brightness = None
        _original_auto_mode = None
