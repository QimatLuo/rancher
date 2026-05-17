#!/usr/bin/env python3
import importlib
import subprocess
import sys
import time
from typing import Callable, Tuple, TypedDict

Coord = Tuple[float, float]
Target = TypedDict("Target", {
    "size": Tuple[int, int],
    "start": Tuple[int, int],
    "end": Tuple[int, int],
})

SERIAL = ""
SLEEP = 0.1

def target_map() -> dict[str, Target]:
    return {
        "R_16_9": {
            "size": (150, 84),
            "start": (372, 262),
            "end": (1506, 897),
        },
        "R_4_3": {
            "size": (150, 114),
            "start": (506, 250),
            "end": (1373, 909),
        },
        "R_1_1": {
            "size": (150, 150),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "R_3_4": {
            "size": (114, 150),
            "start": (689, 250),
            "end": (1186, 909),
        },
        "R_9_16": {
            "size": (84, 150),
            "start": (755, 250),
            "end": (1123, 909),
        },
        "T-Shirt_F": {
            "size": (64, 80),
            "start": (676, 250),
            "end": (1203, 909),
        },
        "T-Shirt_L": {
            "size": (64, 48),
            "start": (500, 250),
            "end": (1378, 909),
        },
        "TopTank": {
            "size": (64, 64),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "MiniSkirt": {
            "size": (128, 64),
            "start": (372, 296),
            "end": (1506, 863),
        },
        "Shorts": {
            "size": (102, 64),
            "start": (414, 250),
            "end": (1464, 909),
        },
        "BucketHat_F": {
            "size": (126, 78),
            "start": (407, 250),
            "end": (1472, 909),
        },
        "BucketHat_C": {
            "size": (100, 100),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "Sweatshirt_F": {
            "size": (80, 88),
            "start": (640, 250),
            "end": (1239, 909),
        },
        "Sweatshirt_S": {
            "size": (130, 70),
            "start": (372, 274),
            "end": (1506, 885),
        },
        "Pants": {
            "size": (80, 116),
            "start": (712, 250),
            "end": (1166, 909),
        },
        "Dress_F": {
            "size": (102, 154),
            "start": (721, 250),
            "end": (1157, 909),
        },
        "Dress_B": {
            "size": (76, 154),
            "start": (777, 250),
            "end": (1102, 909),
        },
        "Dress_I": {
            "size": (168, 102),
            "start": (397, 250),
            "end": (1482, 909),
        },
        "BaseballCap_F": {
            "size": (54, 60),
            "start": (643, 250),
            "end": (1236, 909),
        },
        "BaseballCap_S": {
            "size": (128, 62),
            "start": (372, 305),
            "end": (1506, 854),
        },
        "BaseballCap_B": {
            "size": (62, 52),
            "start": (546, 250),
            "end": (1332, 909),
        },
        "CanvasShoes_U": {
            "size": (100, 90),
            "start": (583, 250),
            "end": (1305, 909),
        },
        "CanvasShoes_T": {
            "size": (120, 60),
            "start": (372, 296),
            "end": (1506, 863),
        },
        "CanvasShoes_S": {
            "size": (60, 60),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "MaryJanes_U": {
            "size": (78, 64),
            "start": (538, 250),
            "end": (1341, 909),
        },
        "MaryJanes_S": {
            "size": (108, 64),
            "start": (384, 250),
            "end": (1495, 909),
        },
        "JoinerySingleBed_Q": {
            "size": (200, 130),
            "start": (432, 250),
            "end": (1446, 909),
        },
        "JoinerySingleBed_B": {
            "size": (200, 98),
            "start": (372, 302),
            "end": (1506, 857),
        },
        "JoineryDoubleBed_Q": {
            "size": (256, 128),
            "start": (372, 296),
            "end": (1506, 863),
        },
        "JoineryDoubleBed_B": {
            "size": (256, 110),
            "start": (372, 336),
            "end": (1506, 823),
        },
        "JoineryCloset": {
            "size": (128, 128),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "JoineryNightstand": {
            "size": (128, 128),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "JoineryDeskLamp_F": {
            "size": (80, 46),
            "start": (372, 253),
            "end": (1506, 905),
        },
        "JoineryDeskLamp_B": {
            "size": (128, 28),
            "start": (372, 456),
            "end": (1506, 703),
        },
        "JoineryChair_F": {
            "size": (54, 28),
            "start": (372, 286),
            "end": (1506, 873),
        },
        "JoineryChair_S": {
            "size": (54, 54),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "JoineryTeaTable": {
            "size": (128, 128),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "Joinery2-SeatSofa": {
            "size": (200, 110),
            "start": (372, 268),
            "end": (1506, 891),
        },
        "JoinerySingleSofa_B": {
            "size": (60, 60),
            "start": (610, 250),
            "end": (1269, 909),
        },
        "JoinerySingleSofa_S": {
            "size": (60, 70),
            "start": (657, 250),
            "end": (1221, 909),
        },
        "JoineryFloorLamp_F": {
            "size": (80, 52),
            "start": (432, 250),
            "end": (1446, 909),
        },
        "JoineryFloorLamp_B": {
            "size": (128, 16),
            "start": (372, 509),
            "end": (1506, 650),
        },
    }

colors = [
'B6B3A7FF',
'404040FF',
'808080FF',
'BFBFBFFF',
# 'FFFFFFFF',
# 'F8F4ECFF',
'D02F48FF',
'EF6C70FF',
'A61D39FF',
'F6ABA5FF',
'CA8281FF',
'A35A5CFF',
'692934FF',
'E7D5D4FF',
'C0ABAAFF',
'745C5CFF',
'E95B23FF',
'FA8155FF',
'AB3D1DFF',
'FFB99EFF',
'DA927AFF',
'AF6954FF',
'75362AFF',
'E9D5CFFF',
'C1ABA5FF',
'755C57FF',
'F49D00FF',
'FFAD36FF',
'B16C00FF',
'FFCE90FF',
'DBA66BFF',
'B37F46FF',
'794D1CFF',
'F6E3CEFF',
'CEBBA8FF',
'806C5CFF',
'EEC900FF',
'FAD831FF',
'B39300FF',
'FBE68FFF',
'D3BD6CFF',
'AB9446FF',
'74601FFF',
'EFE6C6FF',
'C6BEA1FF',
'786F57FF',
'A8BB00FF',
'B7C82BFF',
'748400FF',
'D8DF92FF',
'ADB66BFF',
'858F46FF',
'525B20FF',
'E6E9C6FF',
'BDC1A2FF',
'6E725AFF',
'00A15AFF',
'41B879FF',
'007243FF',
'9CD9ACFF',
'76B18AFF',
'4F8766FF',
'23523AFF',
'C4E0CBFF',
'9DB6A5FF',
'53665AFF',
'00857FFF',
'00AA9FFF',
'006664FF',
'7ECCC1FF',
'54A39BFF',
'2A7B76FF',
'004746FF',
'BFE0D9FF',
'98B6B1FF',
'4E6764FF',
'00709BFF',
'0098B9FF',
'005476FF',
'79BACAFF',
'5192A4FF',
'246A7DFF',
'004558FF',
'C6DDE2FF',
'9EB4B9FF',
'4F656CFF',
'005BA5FF',
'2981C0FF',
'004280FF',
'83A7C8FF',
'5D7EA0FF',
'34597DFF',
'123452FF',
'C2CCD5FF',
'9BA5AFFF',
'4C5765FF',
'534AA0FF',
'7574BCFF',
'3E337BFF',
'A29FC7FF',
'7878A0FF',
'54527CFF',
'322D51FF',
'C9CAD5FF',
'A2A2AFFF',
'565566FF',
'81378AFF',
'A165A8FF',
'612469FF',
'B89AB8FF',
'907194FF',
'6C4A71FF',
'422746FF',
'D0C8D1FF',
'ABA0ABFF',
'605262FF',
'AD2E6CFF',
'D0678EFF',
'861D55FF',
'DAA0B3FF',
'B4788BFF',
'8B4F65FF',
'612D46FF',
'E4D5D9FF',
'BDACB0FF',
'C2282BFF',
]

def adb_tap(point: Coord) -> None:
    x, y = point
    cmd = ["adb"]
    if SERIAL:
        cmd.extend(["-s", SERIAL])
    cmd.extend(["shell", "input", "tap", str(x), str(y)])
    cmd_str = " ".join(cmd)
    print(f"{cmd_str}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        print(f"adb_tap failed rc={exc.returncode} stdout={stdout!r} stderr={stderr!r}")
        raise


def _map_canvas_index_to_screen(index: int, start: int, end: int, count: int) -> float:
    if count < 2:
        raise ValueError("count must be >= 2")
    if not (0 <= index < count):
        raise ValueError(f"index out of range: {index}, expected 0..{count - 1}")

    moves = count - 1
    delta = end - start

    # Fractional mapping keeps sub-pixel precision until adb_tap quantization.
    offset = (index * delta) / moves
    return start + offset

def canvas_to_adb(point: Coord, t: Target) -> Coord:
    col, row = point
    x1, y1 = t["start"]
    x2, y2 = t["end"]
    width, height = t["size"]
    x = _map_canvas_index_to_screen(col, x1, x2, width)
    y = _map_canvas_index_to_screen(row, y1, y2, height)
    return (x, y)

def find_canvas_points_by_color(
    images: list[list[str]],
    color: str,
    paint_mask: list[list[bool]],
) -> list[Coord]:
    target = color[:6].upper()
    points: list[Coord] = []

    for row, image_row in enumerate(images):
        for col, image_color in enumerate(image_row):
            if not paint_mask[row][col]:
                continue
            if image_color.upper() == target:
                points.append((col, row))

    return points


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color6 = color[:6]
    return (int(color6[0:2], 16), int(color6[2:4], 16), int(color6[4:6], 16))


def _nearest_palette_color(
    rgb: tuple[int, int, int],
    palette_rgb: list[tuple[int, int, int]],
    palette_hex: list[str],
) -> str:
    r, g, b = rgb
    best_index = 0
    best_distance = float("inf")

    for idx, (pr, pg, pb) in enumerate(palette_rgb):
        dr = r - pr
        dg = g - pg
        db = b - pb
        distance = dr * dr + dg * dg + db * db
        if distance < best_distance:
            best_distance = distance
            best_index = idx

    return palette_hex[best_index]


def images_from_file(size: Coord) -> tuple[list[list[str]], list[list[bool]]]:
    file_path = "/sample_click/a"
    try:
        cv2 = importlib.import_module("cv2")
        np = importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError("opencv-python and numpy are required. Install with: pip install opencv-python numpy") from exc

    with open(file_path, "rb") as f:
        image_bytes = f.read()

    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(image_array, cv2.IMREAD_UNCHANGED)  # Use IMREAD_UNCHANGED to preserve alpha channel
    if image_bgr is None:
        raise RuntimeError(f"Failed to decode image file: {file_path}")

    # Check if the image has an alpha channel
    has_alpha = image_bgr.shape[2] == 4 if len(image_bgr.shape) == 3 else False

    # Convert to RGB (ignore alpha channel if present)
    if has_alpha:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGRA2RGB)
        alpha_channel = image_bgr[:, :, 3]  # Extract alpha channel
    else:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        alpha_channel = None

    # Calculate the new size while maintaining aspect ratio
    original_height, original_width = image_rgb.shape[:2]
    target_width, target_height = size
    scale = min(target_width / original_width, target_height / original_height)
    new_width = int(original_width * scale)
    new_height = int(original_height * scale)

    # Resize the image with the new dimensions
    resized_image = cv2.resize(image_rgb, (new_width, new_height), interpolation=cv2.INTER_AREA)

    # Resize alpha channel if present
    if alpha_channel is not None:
        resized_alpha = cv2.resize(alpha_channel, (new_width, new_height), interpolation=cv2.INTER_AREA)
    else:
        resized_alpha = None

    # Create a blank canvas with the target size and paste the resized image at the center
    canvas = np.zeros((target_height, target_width, 3), dtype=np.uint8)
    y_offset = (target_height - new_height) // 2
    x_offset = (target_width - new_width) // 2
    canvas[y_offset:y_offset + new_height, x_offset:x_offset + new_width] = resized_image

    # Create a mask for transparent pixels if alpha channel is present
    if resized_alpha is not None:
        alpha_mask = resized_alpha > 0  # Non-transparent pixels
    else:
        alpha_mask = np.ones((new_height, new_width), dtype=bool)  # All pixels are valid

    palette_hex = [c[:6].upper() for c in colors]
    palette_rgb = [_hex_to_rgb(c) for c in palette_hex]

    images: list[list[str]] = []
    paint_mask: list[list[bool]] = []
    nearest_cache: dict[tuple[int, int, int], str] = {}

    for y in range(target_height):
        row: list[str] = []
        mask_row: list[bool] = []
        for x in range(target_width):
            if y_offset <= y < y_offset + new_height and x_offset <= x < x_offset + new_width:
                # Check if the pixel is valid (not transparent)
                local_y = y - y_offset
                local_x = x - x_offset
                if alpha_mask[local_y, local_x]:
                    pixel = canvas[y, x]
                    rgb = (int(pixel[0]), int(pixel[1]), int(pixel[2]))
                    mapped = nearest_cache.get(rgb)
                    if mapped is None:
                        mapped = _nearest_palette_color(rgb, palette_rgb, palette_hex)
                        nearest_cache[rgb] = mapped
                    row.append(mapped)
                    mask_row.append(True)
                    continue
            row.append("000000")  # Default to black for skipped pixels
            mask_row.append(False)
        images.append(row)
        paint_mask.append(mask_row)

    return images, paint_mask


def save_mapped_image_preview(
    images: list[list[str]],
    paint_mask: list[list[bool]],
    output_path: str = "/tmp/paint_nearest_palette.png",
) -> None:
    if not images or not images[0]:
        raise ValueError("images must be a non-empty 2D array")

    try:
        cv2 = importlib.import_module("cv2")
        np = importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError("opencv-python and numpy are required. Install with: pip install opencv-python numpy") from exc

    height = len(images)
    width = len(images[0])
    rgba_image = np.zeros((height, width, 4), dtype=np.uint8)
    rgb_cache: dict[str, tuple[int, int, int]] = {}

    for y, row in enumerate(images):
        if len(row) != width:
            raise ValueError("all image rows must have the same width")
        if len(paint_mask[y]) != width:
            raise ValueError("all paint_mask rows must have the same width as images")

        for x, color in enumerate(row):
            if not paint_mask[y][x]:
                rgba_image[y, x] = (0, 0, 0, 0)
                continue

            color6 = color[:6].upper()
            rgb = rgb_cache.get(color6)
            if rgb is None:
                rgb = _hex_to_rgb(color6)
                rgb_cache[color6] = rgb
            rgba_image[y, x] = (rgb[0], rgb[1], rgb[2], 255)

    bgra_image = cv2.cvtColor(rgba_image, cv2.COLOR_RGBA2BGRA)
    ok = cv2.imwrite(output_path, bgra_image)
    if not ok:
        raise RuntimeError(f"failed to write mapped preview image: {output_path}")
    print(f"mapped preview saved: {output_path}")

def paint_with_palette(images: list[list[str]], on_color: Callable[[str], None]) -> None:
    base_x = 1730
    base_y = 400
    x_step = 130
    y_step = 100
    color_index = 0

    for palette in range(13):
        if palette != 0:
            adb_tap((1833, 330))
            time.sleep(0.3)

        row_count = 2 if palette == 0 else 5

        for row in range(row_count):
            color_y = base_y + row * y_step
            for col in range(2):
                color_x = base_x + col * x_step
                adb_tap((color_x, color_y))
                time.sleep(0.3)

                if color_index >= len(colors):
                    raise IndexError(f"color_index out of range: {color_index}")

                on_color(colors[color_index])
                color_index += 1

def draw_image(target: dict[str, Coord]) -> None:
    images, paint_mask = images_from_file(target["size"])
    save_mapped_image_preview(images, paint_mask)

    def on_color(color: str) -> None:
        matched_points = find_canvas_points_by_color(images, color, paint_mask)

        for canvas_point in matched_points:
            adb_tap(canvas_to_adb(canvas_point, target))
            time.sleep(SLEEP)

    paint_with_palette(images, on_color)


def test_size(target: dict[str, Coord]) -> None:
    size = target["size"]
    width, height = size
    for col in range(0, width, 2):
        adb_tap(canvas_to_adb((col, 0), target))
        time.sleep(SLEEP)
        adb_tap(canvas_to_adb((col, height - 1), target))
        time.sleep(SLEEP)
    for row in range(1, height - 1, 2):
        adb_tap(canvas_to_adb((0, row), target))
        time.sleep(SLEEP)
        adb_tap(canvas_to_adb((width - 1, row), target))
        time.sleep(SLEEP)

    adb_tap((1727, 444))
    for col in range(1, width, 2):
        adb_tap(canvas_to_adb((col, 0), target))
        time.sleep(SLEEP)
        adb_tap(canvas_to_adb((col, height - 1), target))
        time.sleep(SLEEP)
    for row in range(2, height - 1, 2):
        adb_tap(canvas_to_adb((0, row), target))
        time.sleep(SLEEP)
        adb_tap(canvas_to_adb((width - 1, row), target))
        time.sleep(SLEEP)


def _resolve_target_from_argv(argv: list[str]) -> Target:
    targets = target_map()
    targets_upper = {name.upper(): target for name, target in targets.items()}

    if len(argv) < 2:
        available = ", ".join(targets.keys())
        raise SystemExit(f"Missing target argument. Usage: {argv[0]} <target>. Available: {available}")

    target_name = argv[1].strip().upper()
    target = targets_upper.get(target_name)
    if target is None:
        available = ", ".join(targets.keys())
        raise SystemExit(f"Unknown target: {argv[1]!r}. Available: {available}")

    return target

def main() -> None:
    target = _resolve_target_from_argv(sys.argv)
    draw_image(target)

if __name__ == "__main__":
    main()
