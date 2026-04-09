#!/usr/bin/env python3
from functools import lru_cache
import os
from dataclasses import dataclass

import cv2
import numpy as np


TRIANGLE_TARGET_COLOR_HEX = "F0F6CA"
TRIANGLE_TARGET_BASE_LEN = 10
TRIANGLE_BASE_LEN_TOLERANCE = 6
TRIANGLE_MIN_AREA = 18.0
TRIANGLE_MAX_AREA = 80000.0
TRIANGLE_MIN_HEIGHT = 2.0
TRIANGLE_MAX_COLOR_DIST = 45.0
TRIANGLE_TEMPLATE_MIN_FG_BG_CONTRAST = 28.0
TRIANGLE_TEMPLATE_MATCH_THRESHOLD = 0.62
TRIANGLE_TEMPLATE_MAX_DETECTIONS = 20


def scale_point_from_reference(
    frame_w: int,
    frame_h: int,
    ref_x: int,
    ref_y: int,
    *,
    ref_w: int = 640,
    ref_h: int = 360,
) -> tuple[int, int]:
    if frame_w <= 0 or frame_h <= 0:
        return 0, 0

    sx = frame_w / float(ref_w)
    sy = frame_h / float(ref_h)
    x = int(round(ref_x * sx))
    y = int(round(ref_y * sy))
    x = max(0, min(frame_w - 1, x))
    y = max(0, min(frame_h - 1, y))
    return x, y


def sample_rgb_from_frame(frame_bgr: np.ndarray, x: int, y: int) -> tuple[int, int, int] | None:
    h, w = frame_bgr.shape[:2]
    if x < 0 or y < 0 or x >= w or y >= h:
        return None

    b, g, r = frame_bgr[y, x]
    return int(r), int(g), int(b)


def hex_to_rgb(color_hex: str) -> tuple[int, int, int]:
    text = color_hex.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"invalid color hex: {color_hex}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def is_color_close(
    got_rgb: tuple[int, int, int],
    expected_rgb: tuple[int, int, int],
    tolerance: int,
) -> bool:
    dr = abs(got_rgb[0] - expected_rgb[0])
    dg = abs(got_rgb[1] - expected_rgb[1])
    db = abs(got_rgb[2] - expected_rgb[2])
    return dr <= tolerance and dg <= tolerance and db <= tolerance


@dataclass(frozen=True)
class Triangle:
    p1: tuple[int, int]
    p2: tuple[int, int]
    p3: tuple[int, int]

    @property
    def points(self) -> np.ndarray:
        return np.array([self.p1, self.p2, self.p3], dtype=np.int32)

    @property
    def center(self) -> tuple[int, int]:
        pts = self.points
        cx = int(round(float((pts[:, 0].min() + pts[:, 0].max()) * 0.5)))
        cy = int(round(float((pts[:, 1].min() + pts[:, 1].max()) * 0.5)))
        return cx, cy


@dataclass(frozen=True)
class TriangleMetrics:
    area: float
    base_len: float
    height: float
    color_h: float
    color_s: float
    color_v: float
    color_dist: float
    fill_ratio: float


def _dedupe_triangles(triangles: list[Triangle], min_center_dist: float = 8.0) -> list[Triangle]:
    if not triangles:
        return []

    picked: list[Triangle] = []
    for t in triangles:
        tx, ty = t.center
        keep = True
        for p in picked:
            px, py = p.center
            if np.hypot(tx - px, ty - py) < min_center_dist:
                keep = False
                break
        if keep:
            picked.append(t)
    return picked


def _hex_to_bgr(hex_color: str) -> np.ndarray:
    rgb = np.array(
        [
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        ],
        dtype=np.float32,
    )
    return rgb[::-1]


def _build_color_mask(frame_bgr: np.ndarray, target_bgr: np.ndarray, *, max_color_dist: float) -> np.ndarray:
    bgr = frame_bgr.astype(np.float32)
    dists = np.linalg.norm(bgr - target_bgr.reshape(1, 1, 3), axis=2)
    mask = np.zeros(frame_bgr.shape[:2], dtype=np.uint8)
    mask[dists <= float(max_color_dist)] = 255
    return mask


def _resolve_triangle_template_path() -> str:
    return os.path.abspath(
        os.path.join(
            "/",
            "sample_click",
            "triangle.png",
        )
    )


@lru_cache(maxsize=1)
def _load_triangle_template_gray() -> np.ndarray:
    path = _resolve_triangle_template_path()
    tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise FileNotFoundError(f"triangle template not found or unreadable: {path}")
    if tpl.size == 0:
        raise ValueError(f"triangle template is empty: {path}")
    return tpl


@lru_cache(maxsize=1)
def _load_triangle_template_color_profile() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    path = _resolve_triangle_template_path()
    tpl_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    if tpl_bgr is None:
        raise FileNotFoundError(f"triangle template not found or unreadable: {path}")

    # Treat near-black pixels as background so color stats focus on triangle fill.
    fg_mask = np.any(tpl_bgr > 12, axis=2)
    if not np.any(fg_mask):
        fg_mask = np.ones(tpl_bgr.shape[:2], dtype=bool)

    mean_bgr = tpl_bgr[fg_mask].astype(np.float32).mean(axis=0)
    bg_mask = ~fg_mask
    return mean_bgr, fg_mask.astype(np.uint8), bg_mask.astype(np.uint8)


def _candidate_color_distance(
    patch_bgr: np.ndarray,
    template_fg_mask: np.ndarray,
    template_mean_bgr: np.ndarray,
) -> float:
    if patch_bgr.shape[:2] != template_fg_mask.shape[:2]:
        return 1e9

    fg = template_fg_mask > 0
    if not np.any(fg):
        return 1e9

    cand_mean_bgr = patch_bgr[fg].astype(np.float32).mean(axis=0)
    return float(np.linalg.norm(cand_mean_bgr - template_mean_bgr))


def _candidate_fg_bg_contrast(
    patch_bgr: np.ndarray,
    template_fg_mask: np.ndarray,
    template_bg_mask: np.ndarray,
) -> float:
    if patch_bgr.shape[:2] != template_fg_mask.shape[:2]:
        return 0.0
    if template_bg_mask.shape[:2] != template_fg_mask.shape[:2]:
        return 0.0

    fg = template_fg_mask > 0
    bg = template_bg_mask > 0
    if not np.any(fg) or not np.any(bg):
        return 0.0

    fg_mean_bgr = patch_bgr[fg].astype(np.float32).mean(axis=0)
    bg_mean_bgr = patch_bgr[bg].astype(np.float32).mean(axis=0)
    return float(np.linalg.norm(fg_mean_bgr - bg_mean_bgr))


def _detect_triangles_by_template_matching(
    frame_bgr: np.ndarray,
    *,
    match_threshold: float,
    max_detections: int,
    max_color_dist: float,
    min_fg_bg_contrast: float,
) -> list[Triangle]:
    tpl = _load_triangle_template_gray()
    template_mean_bgr, template_fg_mask, template_bg_mask = _load_triangle_template_color_profile()
    th, tw = tpl.shape[:2]
    fh, fw = frame_bgr.shape[:2]

    if tw <= 0 or th <= 0 or fw < tw or fh < th:
        return []

    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    score_map = cv2.matchTemplate(frame_gray, tpl, cv2.TM_CCOEFF_NORMED)
    work = score_map.copy()

    triangles: list[Triangle] = []
    min_center_dist = max(8.0, float(max(tw, th)) * 0.5)

    for _ in range(max_detections):
        _min_v, max_v, _min_l, max_l = cv2.minMaxLoc(work)
        if float(max_v) < float(match_threshold):
            break

        x1, y1 = int(max_l[0]), int(max_l[1])
        x2, y2 = x1 + tw - 1, y1 + th - 1

        patch = frame_bgr[y1 : y2 + 1, x1 : x2 + 1]
        color_dist = _candidate_color_distance(
            patch,
            template_fg_mask,
            template_mean_bgr,
        )
        fg_bg_contrast = _candidate_fg_bg_contrast(
            patch,
            template_fg_mask,
            template_bg_mask,
        )
        if color_dist <= float(max_color_dist) and fg_bg_contrast >= float(min_fg_bg_contrast):
            triangles.append(Triangle(p1=(x1, y1), p2=(x2, y1), p3=(x1, y2)))

        sx1 = max(0, x1 - tw // 2)
        sy1 = max(0, y1 - th // 2)
        sx2 = min(work.shape[1], x1 + tw // 2 + 1)
        sy2 = min(work.shape[0], y1 + th // 2 + 1)
        work[sy1:sy2, sx1:sx2] = -1.0

    triangles = _dedupe_triangles(triangles, min_center_dist=min_center_dist)
    triangles = sorted(triangles, key=lambda t: (t.center[1], t.center[0]))
    return triangles


def _contour_to_bbox_triangle(cnt: np.ndarray) -> tuple[Triangle, int, int, int, int] | None:
    x, y, w, h = cv2.boundingRect(cnt)
    if w <= 0 or h <= 0:
        return None
    x2 = x + w - 1
    y2 = y + h - 1
    tri = Triangle(p1=(x, y), p2=(x2, y), p3=(x, y2))
    return tri, x, y, x2, y2


def _passes_blob_gate(
    *,
    area: float,
    box_h: int,
    min_area: float,
    max_area: float,
    min_height: float,
) -> bool:
    if area < min_area or area > max_area:
        return False
    if float(box_h) < min_height:
        return False
    return True


def _blob_rank_score(
    bbox: tuple[int, int, int, int],
    *,
    roi_shape: tuple[int, int, int],
) -> float:
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) * 0.5
    cy = (y1 + y2) * 0.5
    roi_h, roi_w = roi_shape[:2]
    # Prefer color blobs near ROI center to reduce edge noise.
    return float(np.hypot(cx - (roi_w - 1) * 0.5, cy - (roi_h - 1) * 0.5))


def _detect_color_blobs_from_mask(
    frame_bgr: np.ndarray,
    mask: np.ndarray,
    *,
    min_area: float,
    max_area: float,
    min_height: float,
) -> list[Triangle]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    ranked: list[tuple[float, Triangle]] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        maybe = _contour_to_bbox_triangle(cnt)
        if maybe is None:
            continue
        tri, x1, y1, x2, y2 = maybe

        if not _passes_blob_gate(
            area=area,
            box_h=(y2 - y1 + 1),
            min_area=min_area,
            max_area=max_area,
            min_height=min_height,
        ):
            continue

        score = _blob_rank_score((x1, y1, x2, y2), roi_shape=frame_bgr.shape)
        ranked.append((score, tri))

    ranked.sort(key=lambda it: it[0])
    return [tri for _score, tri in ranked]


def detect_yellow_inverted_triangles(
    frame_bgr: np.ndarray,
    *,
    target_base_len: int = TRIANGLE_TARGET_BASE_LEN,
    base_len_tolerance: int = TRIANGLE_BASE_LEN_TOLERANCE,
    min_area: float = TRIANGLE_MIN_AREA,
    max_area: float = TRIANGLE_MAX_AREA,
    min_height: float = TRIANGLE_MIN_HEIGHT,
    max_color_dist: float = TRIANGLE_MAX_COLOR_DIST,
    template_min_fg_bg_contrast: float = TRIANGLE_TEMPLATE_MIN_FG_BG_CONTRAST,
    template_match_threshold: float = TRIANGLE_TEMPLATE_MATCH_THRESHOLD,
    template_max_detections: int = TRIANGLE_TEMPLATE_MAX_DETECTIONS,
) -> list[Triangle]:
    _ = target_base_len
    _ = base_len_tolerance
    _ = min_area
    _ = max_area
    _ = min_height
    _ = max_color_dist
    return _detect_triangles_by_template_matching(
        frame_bgr,
        match_threshold=template_match_threshold,
        max_detections=template_max_detections,
        max_color_dist=max_color_dist,
        min_fg_bg_contrast=template_min_fg_bg_contrast,
    )


def detect_yellow_inverted_triangle_tuples(
    frame_bgr: np.ndarray,
    *,
    target_base_len: int = TRIANGLE_TARGET_BASE_LEN,
    base_len_tolerance: int = TRIANGLE_BASE_LEN_TOLERANCE,
    min_area: float = TRIANGLE_MIN_AREA,
    max_area: float = TRIANGLE_MAX_AREA,
    min_height: float = TRIANGLE_MIN_HEIGHT,
    max_color_dist: float = TRIANGLE_MAX_COLOR_DIST,
    template_min_fg_bg_contrast: float = TRIANGLE_TEMPLATE_MIN_FG_BG_CONTRAST,
    template_match_threshold: float = TRIANGLE_TEMPLATE_MATCH_THRESHOLD,
    template_max_detections: int = TRIANGLE_TEMPLATE_MAX_DETECTIONS,
) -> list[tuple[int, int, int]]:
    triangles = detect_yellow_inverted_triangles(
        frame_bgr,
        target_base_len=target_base_len,
        base_len_tolerance=base_len_tolerance,
        min_area=min_area,
        max_area=max_area,
        min_height=min_height,
        max_color_dist=max_color_dist,
        template_min_fg_bg_contrast=template_min_fg_bg_contrast,
        template_match_threshold=template_match_threshold,
        template_max_detections=template_max_detections,
    )

    out: list[tuple[int, int, int]] = []
    for t in triangles:
        pts = t.points
        base_len = int(pts[:, 0].max() - pts[:, 0].min() + 1)
        if base_len <= 0:
            base_len = target_base_len
        cx, cy = t.center
        out.append((cx, cy, base_len))
    return out


