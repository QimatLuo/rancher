#!/usr/bin/env python3
import datetime
import os
import re
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np


class BlackFrameError(RuntimeError):
    pass


def capture_frame_with_screenrecord(out_dir: Path, size: str) -> tuple[Path, Path, str]:
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    raw_video_path = out_dir / f"screenrecord_{ts}.mp4"
    raw_path = out_dir / f"screenrecord_frame_{ts}.png"
    remote_video_path = f"/data/local/tmp/screenrecord_{ts}.mp4"

    subprocess.check_call(["adb", "shell", "rm", "-f", remote_video_path])
    subprocess.check_call(
        [
            "adb",
            "shell",
            "screenrecord",
            "--time-limit",
            "1",
            "--size",
            size,
            remote_video_path,
        ]
    )
    subprocess.check_call(["adb", "pull", remote_video_path, str(raw_video_path)])
    subprocess.check_call(["adb", "shell", "rm", "-f", remote_video_path])

    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_video_path),
            "-frames:v",
            "1",
            str(raw_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return raw_path, raw_video_path, ts


def find_best_template_match(
    screenshot: np.ndarray,
    template: np.ndarray,
    coarse_step: int = 8,
    threshold: float = 12.0,
) -> tuple[bool, tuple[int, int, int, int] | None, float]:
    sh, sw = screenshot.shape[:2]
    th, tw = template.shape[:2]
    if tw > sw or th > sh:
        return False, None, 255.0

    screenshot_gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    # TM_CCOEFF_NORMED is sensitive to missing narrow peaks; use full-resolution
    # result map to avoid missing tiny templates during coarse subsampling.
    _ = coarse_step  # kept for backward-compatible signature
    result = cv2.matchTemplate(screenshot_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, best_score, _, best_xy = cv2.minMaxLoc(result)
    best_score = float(best_score)

    # Backward compatibility: historical thresholds used a 0-255 diff-like scale
    # (default 12.0). Convert legacy (>1) values to normalized similarity space.
    similarity_threshold = threshold if threshold <= 1.0 else (1.0 - (threshold / 100.0))
    similarity_threshold = max(0.5, min(0.99, similarity_threshold))

    found = best_score >= similarity_threshold
    if not found:
        return False, None, best_score

    x, y = best_xy
    return True, (x, y, x + tw, y + th), best_score


def is_all_black(image: np.ndarray) -> bool:
    return int(image.max()) == 0


def remove_red_overlay(image: np.ndarray) -> np.ndarray:
    fixed = image.copy()
    height, width = fixed.shape[:2]

    for y in range(height):
        for x in range(width):
            blue, green, red = fixed[y, x]
            if red >= 240 and green <= 30 and blue <= 30:
                if x > 0:
                    fixed[y, x] = fixed[y, x - 1]
                elif y > 0:
                    fixed[y, x] = fixed[y - 1, x]
                else:
                    fixed[y, x] = (0, 0, 0)

    return fixed


def get_device_screen_size() -> tuple[int, int]:
    output = subprocess.check_output(["adb", "shell", "wm", "size"], text=True)
    for line in output.splitlines():
        match = re.search(r"(\d+)x(\d+)", line)
        if match:
            return int(match.group(1)), int(match.group(2))
    raise RuntimeError("unable to parse device screen size from `adb shell wm size`")


def scale_point(
    x: int,
    y: int,
    source_size: tuple[int, int],
    target_size: tuple[int, int],
) -> tuple[int, int]:
    source_w, source_h = source_size
    target_w, target_h = target_size
    if source_w <= 0 or source_h <= 0:
        return x, y

    scaled_x = int(round(x * target_w / source_w))
    scaled_y = int(round(y * target_h / source_h))
    scaled_x = max(0, min(target_w - 1, scaled_x))
    scaled_y = max(0, min(target_h - 1, scaled_y))
    return scaled_x, scaled_y


def find_match_from_templates(
    screenshot: np.ndarray,
    templates: list[tuple[Path, np.ndarray]],
    threshold: float,
) -> tuple[bool, Path | None, tuple[int, int, int, int] | None, float, list[tuple[str, float, bool]]]:
    best_path: Path | None = None
    best_box: tuple[int, int, int, int] | None = None
    best_score = -1.0
    found_any = False
    score_logs: list[tuple[str, float, bool]] = []

    for template_path, template_image in templates:
        found, box, score = find_best_template_match(screenshot, template_image, threshold=threshold)
        score_logs.append((template_path.name, score, found))
        if found and (not found_any or score > best_score):
            found_any = True
            best_score = score
            best_box = box
            best_path = template_path
        elif not found_any and score > best_score:
            # Keep best non-match score for diagnostics.
            best_score = score
            best_box = box
            best_path = template_path

    if found_any:
        return True, best_path, best_box, best_score, score_logs
    return False, None, None, best_score, score_logs


def capture_and_mark_template_once(
    out_dir: Path,
    size: str,
    templates: list[tuple[Path, np.ndarray]],
    template_dir: Path,
    threshold: float,
) -> tuple[
    Path,
    Path,
    Path,
    bool,
    float,
    tuple[int, int, int, int] | None,
    str,
    list[tuple[str, float, bool]],
]:
    raw_path, raw_video_path, ts = capture_frame_with_screenrecord(out_dir, size)
    report_path = out_dir / f"screencap_template_report_{ts}.txt"

    image = cv2.imread(str(raw_path), cv2.IMREAD_COLOR)
    if image is None:
        raw_video_path.unlink(missing_ok=True)
        raw_path.unlink(missing_ok=True)
        raise RuntimeError(f"failed to read captured frame: {raw_path}")

    if is_all_black(image):
        raw_video_path.unlink(missing_ok=True)
        raw_path.unlink(missing_ok=True)
        raise BlackFrameError("captured frame is all black")

    found, matched_template_path, box, score, score_logs = find_match_from_templates(
        image, templates, threshold
    )

    box_text = "none" if box is None else f"{box[0]},{box[1]},{box[2]},{box[3]}"
    matched_template_text = "none" if matched_template_path is None else str(matched_template_path)
    scores_text = ";".join([f"{name}:{value:.4f}" for name, value, _ in score_logs])
    report_path.write_text(
        "\n".join(
            [
                f"timestamp={ts}",
                f"size={size}",
                f"template_dir={template_dir}",
                f"matched_template={matched_template_text}",
                f"raw_video={raw_video_path}",
                f"raw={raw_path}",
                f"matched={str(found).lower()}",
                f"match_score={score:.4f}",
                f"threshold={threshold:.4f}",
                f"box={box_text}",
                f"scores={scores_text}",
                f"note=best-match template search against {template_dir}/*.png; any template similarity >= threshold is a match",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return (
        raw_video_path,
        raw_path,
        report_path,
        found,
        score,
        box,
        matched_template_text,
        score_logs,
    )


def main() -> None:
    out_dir = Path("/tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    template_dir = Path(os.getenv("MATCH_TEMPLATE_DIR", "/sample_click/tv"))
    if not template_dir.exists():
        raise FileNotFoundError(f"template dir not found: {template_dir}")

    template_paths = sorted(template_dir.glob("*.png"))
    if not template_paths:
        raise FileNotFoundError(f"no png templates found in {template_dir}")

    templates: list[tuple[Path, np.ndarray]] = []
    for template_path in template_paths:
        template_image = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template_image is None:
            raise RuntimeError(f"failed to read template image: {template_path}")
        templates.append((template_path, template_image))

    # Lower resolution improves capture speed.
    size = os.getenv("MATCH_SIZE", "640x360")
    threshold = float(os.getenv("MATCH_THRESHOLD", "12.0"))
    local_image_path = os.getenv("MATCH_LOCAL_IMAGE", "").strip()
    retry_interval = float(os.getenv("MATCH_RETRY_INTERVAL", "0.1"))
    max_attempts = int(os.getenv("MATCH_MAX_ATTEMPTS", "0"))
    after_tap_sleep = float(os.getenv("MATCH_AFTER_TAP_SLEEP", "5.0"))
    max_cycles = int(os.getenv("MATCH_MAX_CYCLES", "0"))
    action_mode = os.getenv("MATCH_ACTION", "tap").strip().lower()

    # Local one-shot debug mode for threshold tuning without adb/device actions.
    if local_image_path:
        local_path = Path(local_image_path)
        if not local_path.exists():
            raise FileNotFoundError(f"MATCH_LOCAL_IMAGE not found: {local_path}")

        image = cv2.imread(str(local_path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"failed to read local image: {local_path}")
        if is_all_black(image):
            raise BlackFrameError("local image is all black")

        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        report_path = out_dir / f"screencap_template_report_{ts}.txt"

        found, matched_template_path, box, score, score_logs = find_match_from_templates(
            image, templates, threshold
        )

        box_text = "none" if box is None else f"{box[0]},{box[1]},{box[2]},{box[3]}"
        matched_template_text = "none" if matched_template_path is None else str(matched_template_path)
        scores_text = ";".join([f"{name}:{value:.4f}" for name, value, _ in score_logs])
        report_path.write_text(
            "\n".join(
                [
                    f"timestamp={ts}",
                    f"size={size}",
                    f"template_dir={template_dir}",
                    f"matched_template={matched_template_text}",
                    "raw_video=none",
                    f"raw={local_path}",
                    f"matched={str(found).lower()}",
                    f"match_score={score:.4f}",
                    f"threshold={threshold:.4f}",
                    f"box={box_text}",
                    f"scores={scores_text}",
                    "note=local one-shot template search; actions are disabled when MATCH_LOCAL_IMAGE is set",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        for template_name, template_score, template_found in score_logs:
            print(
                f"cycle=1 attempt=1 local=true template={template_name} "
                f"score={template_score:.4f} matched={str(template_found).lower()}"
            )

        print("cycle=1 attempt=1 local=true")
        print("raw_video=none")
        print(f"raw={local_path}")
        print(f"report={report_path}")
        print(f"matched={str(found).lower()}")
        print(f"matched_template={matched_template_text}")
        print(f"match_score={score:.4f}")
        print(f"threshold={threshold:.4f}")
        print(f"box={box_text}")
        print("action=none (local mode)")
        return

    if action_mode not in {"tap", "dpad_center", "keyevent_dpad_center"}:
        raise ValueError(
            "invalid MATCH_ACTION, use one of: tap, dpad_center, keyevent_dpad_center"
        )

    screen_w, screen_h = get_device_screen_size()

    cycle = 0
    while True:
        cycle += 1
        attempt = 0
        while True:
            attempt += 1
            try:
                (
                    raw_video_path,
                    raw_path,
                    report_path,
                    found,
                    score,
                    box,
                    matched_template,
                    score_logs,
                ) = capture_and_mark_template_once(
                    out_dir,
                    size,
                    templates,
                    template_dir,
                    threshold,
                )
            except BlackFrameError:
                print(f"cycle={cycle} attempt={attempt} skipped=all_black_frame")
                if max_attempts > 0 and attempt >= max_attempts:
                    raise RuntimeError("template was not found within MATCH_MAX_ATTEMPTS")
                if retry_interval > 0:
                    time.sleep(retry_interval)
                continue

            for template_name, template_score, template_found in score_logs:
                print(
                    f"cycle={cycle} attempt={attempt} template={template_name} "
                    f"score={template_score:.4f} matched={str(template_found).lower()}"
                )

            if found:
                print(f"cycle={cycle} attempt={attempt}")
                print(f"raw_video={raw_video_path}")
                print(f"raw={raw_path}")
                print(f"report={report_path}")
                print("matched=true")
                print(f"matched_template={matched_template}")
                print(f"match_score={score:.4f}")
                print(f"threshold={threshold:.4f}")
                if box is not None:
                    cx = (box[0] + box[2]) // 2
                    cy = (box[1] + box[3]) // 2
                    print(f"box={box[0]},{box[1]},{box[2]},{box[3]}")
                    if action_mode == "tap":
                        captured_frame = cv2.imread(str(raw_path), cv2.IMREAD_COLOR)
                        if captured_frame is None:
                            raise RuntimeError(f"failed to read captured frame for tap: {raw_path}")
                        capture_h, capture_w = captured_frame.shape[:2]
                        tap_x, tap_y = scale_point(
                            cx,
                            cy,
                            (capture_w, capture_h),
                            (screen_w, screen_h),
                        )
                        print(
                            f"action=tap at={tap_x},{tap_y} "
                            f"(from={cx},{cy} capture={capture_w}x{capture_h} screen={screen_w}x{screen_h})"
                        )
                        subprocess.check_call(
                            ["adb", "shell", "input", "tap", str(tap_x), str(tap_y)]
                        )
                    else:
                        print("action=keyevent DPAD_CENTER")
                        subprocess.check_call(["adb", "shell", "input", "keyevent", "DPAD_CENTER"])
                else:
                    print("box=none")

                if max_cycles > 0 and cycle >= max_cycles:
                    return

                if after_tap_sleep > 0:
                    print(f"sleep={after_tap_sleep:.1f}s")
                    time.sleep(after_tap_sleep)
                break

            # Keep streaming until matched; preserve miss artifacts for inspection.
            print(
                f"cycle={cycle} attempt={attempt} matched=false score={score:.4f} threshold={threshold:.4f}"
            )
            print(f"raw_video={raw_video_path}")
            print(f"raw={raw_path}")
            print(f"report={report_path}")

            if max_attempts > 0 and attempt >= max_attempts:
                raise RuntimeError("template was not found within MATCH_MAX_ATTEMPTS")

            if retry_interval > 0:
                time.sleep(retry_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted by user (Ctrl+C), exiting cleanly")
