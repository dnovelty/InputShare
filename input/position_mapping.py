"""Proportional (fraction-based) cursor mapping between the PC and Android screens.

Port of deskflow ``Server::mapToFraction`` / ``Server::mapToPixel`` for
InputShare's single-device layout. The Android cursor is tracked as a virtual
absolute coordinate (deskflow tracks ``m_x``/``m_y`` on secondary screens the
same way), so the exit fraction can be mapped back to the PC when switching.
"""

import threading

# Direction strings must match `input.edge_portal.EdgeDirection` values.
LEFT = "left"
RIGHT = "right"
TOP = "top"
BOTTOM = "bottom"

# The Android return-edge strip (`SideLineOverlay`) is 8dp wide. When warping the
# cursor on switch, enter this many pixels *inside* the screen so the cursor does
# not start ON that strip; otherwise the strip's one-shot hover trigger fires
# immediately (and is swallowed by the toggle debounce), latching it until the
# cursor exits and re-enters. Mirrors deskflow's `avoidJumpZone`. 48px > 8dp on
# any density up to 6x.
JUMP_ZONE_PX = 48

_android_width = 0
_android_height = 0

_lock = threading.Lock()
_android_x = 0
_android_y = 0

# Registered by `input.callbacks` so the Android pointer can be moved to an
# absolute position (scrcpy InjectTouchEvent hover move).
_warp_callback = None


def set_android_screen_size(width: int, height: int):
    global _android_width, _android_height
    _android_width = width
    _android_height = height
    # Android places a freshly connected mouse pointer at the screen center.
    set_android_cursor(width // 2, height // 2)


def get_android_screen_size() -> tuple[int, int]:
    return _android_width, _android_height


def set_android_cursor(x: int, y: int):
    global _android_x, _android_y
    with _lock:
        _android_x = x
        _android_y = y


def move_android_cursor_by(dx: int, dy: int):
    """Accumulate a relative HID move, clamped to the screen bounds so the
    tracked position stays aligned with Android's own edge clamping."""
    global _android_x, _android_y
    with _lock:
        _android_x = max(0, min(_android_width - 1, _android_x + dx))
        _android_y = max(0, min(_android_height - 1, _android_y + dy))


def get_android_cursor() -> tuple[int, int]:
    with _lock:
        return _android_x, _android_y


def register_warp_callback(callback):
    global _warp_callback
    _warp_callback = callback


def warp_android_pointer(x: int, y: int):
    """Move the Android pointer to absolute ``(x, y)`` and update tracking."""
    set_android_cursor(x, y)
    if _warp_callback is not None:
        _warp_callback(x, y)


def map_pc_to_android(
    pc_x: int, pc_y: int,
    direction: str,
    pc_w: int, pc_h: int,
    android_w: int, android_h: int,
) -> tuple[int, int]:
    """Map the PC cursor's exit position to the Android entry position.

    Port of deskflow ``mapToFraction`` + ``mapToPixel`` for the single-device
    case. ``direction`` is where the Android device sits relative to the PC.
    """
    if direction in (LEFT, RIGHT):
        t = (pc_y + 0.5) / pc_h
        android_y = int(t * android_h)
        android_x = 0 if direction == RIGHT else android_w - 1
    else:  # TOP / BOTTOM
        t = (pc_x + 0.5) / pc_w
        android_x = int(t * android_w)
        android_y = android_h - 1 if direction == TOP else 0

    android_x = max(0, min(android_w - 1, android_x))
    android_y = max(0, min(android_h - 1, android_y))
    return android_x, android_y


def avoid_jump_zone(
    android_x: int, android_y: int,
    direction: str,
    android_w: int, android_h: int,
) -> tuple[int, int]:
    """Move the entry position inward so it is not on the return-edge strip.

    Port of deskflow ``Server::avoidJumpZone``, applied to the Android screen
    (whose switch-back is driven by the edge strip rather than server-side
    position tracking). Keeps the fraction-mapped coordinate along the edge and
    only shifts the perpendicular one inward.
    """
    if direction == RIGHT:
        android_x = JUMP_ZONE_PX
    elif direction == LEFT:
        android_x = android_w - 1 - JUMP_ZONE_PX
    elif direction == TOP:
        android_y = android_h - 1 - JUMP_ZONE_PX
    elif direction == BOTTOM:
        android_y = JUMP_ZONE_PX

    android_x = max(0, min(android_w - 1, android_x))
    android_y = max(0, min(android_h - 1, android_y))
    return android_x, android_y


def map_android_to_pc(
    android_x: int, android_y: int,
    direction: str,
    pc_w: int, pc_h: int,
    android_w: int, android_h: int,
    margin: int,
) -> tuple[int, int]:
    """Map the Android cursor's exit position to the PC entry position."""
    if direction in (LEFT, RIGHT):
        t = (android_y + 0.5) / android_h
        pc_y = int(t * pc_h)
        pc_x = pc_w - 1 - margin if direction == RIGHT else margin
    else:  # TOP / BOTTOM
        t = (android_x + 0.5) / android_w
        pc_x = int(t * pc_w)
        pc_y = margin if direction == TOP else pc_h - 1 - margin

    pc_x = max(0, min(pc_w - 1, pc_x))
    pc_y = max(0, min(pc_h - 1, pc_y))
    return pc_x, pc_y
