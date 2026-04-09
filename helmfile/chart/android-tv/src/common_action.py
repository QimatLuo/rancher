#!/usr/bin/env python3
from typing import Callable


ActionStep = tuple[str, tuple[int, int] | str, float]
TriangleTuple = tuple[int, int, int]


TRIANGLE_TAP_OFFSET_PX = 20


ITEM_CONFIRM_TAP_BY_TEXT: dict[str, tuple[int, int]] = {
    "bait": (269, 96),
    "button": (319, 140),
    "picnic": (264, 125),
    "repair": (319, 100),
}


def use_item(search_text: str, final_wait_sec: float = 5.0) -> list[ActionStep]:
    normalized_text = search_text.lower()
    confirm_tap = ITEM_CONFIRM_TAP_BY_TEXT.get(normalized_text, (319, 101))
    if normalized_text in ("repair", "bait"):
        pre_confirm_tap = (380, 88)
    else:
        pre_confirm_tap = (435, 88)
    steps: list[ActionStep] = [
        ("tap", (601, 69), 2.0),
        ("tap", (575, 44), 2.0),
        ("tap", (301, 135), 2.0),
        ("text", search_text, 2.0),
        ("tap", (614, 147), 2.0),
        ("tap", (433, 133), 2.0),
        ("tap", pre_confirm_tap, 2.0),
        ("tap", confirm_tap, 0.0),
    ]

    if normalized_text in ("repair", "bait"):
        steps[-1] = ("tap", confirm_tap, final_wait_sec)
    else:
        steps.append(("tap", (603, 17), final_wait_sec))

    return steps


RED_LINE_ACTION_STEPS: list[ActionStep] = use_item("picnic")


DEFAULT_ACTION_STEPS: list[ActionStep] = use_item("repair", final_wait_sec=17.0)


def triangle_point_to_tap_point(x: int, y: int, offset_px: int = TRIANGLE_TAP_OFFSET_PX) -> tuple[int, int]:
    return x, max(0, y - offset_px)


def triangles_to_tap_points(
    triangles: list[TriangleTuple],
    offset_px: int = TRIANGLE_TAP_OFFSET_PX,
) -> list[tuple[int, int]]:
    return [triangle_point_to_tap_point(x, y, offset_px=offset_px) for x, y, _base_len in triangles]


def tap_points(action_steps: list[ActionStep]) -> list[tuple[int, int]]:
    return [value for action, value, _wait_sec in action_steps if action == "tap"]


def run_action_steps(
    action_steps: list[ActionStep],
    publish_active_step: Callable[[int], None],
    tap: Callable[[int, int], bool],
    input_text: Callable[[str], bool],
    wait: Callable[[float], bool],
    start_tap_order: int,
) -> bool:
    tap_order = start_tap_order
    for action, value, wait_sec in action_steps:
        if action == "tap":
            publish_active_step(tap_order)
            tap_x, tap_y = value
            tap(tap_x, tap_y)
            tap_order += 1
        else:
            publish_active_step(0)
            input_text(str(value))

        if wait_sec > 0 and wait(wait_sec):
            return True

    return False
