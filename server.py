import argparse
import json
import math
from collections import Counter
from multiprocessing import get_context
import mimetypes
import numpy as np
import os
import threading
import traceback
import time
import uuid
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8888"))
WORKERS = int(os.environ.get("WORKERS", "0"))
COCO_URL = os.environ.get(
    "COCO_URL",
    "https://huggingface.co/buckets/hamimmahmud0/SAM_COCO_v1_b2_3024/resolve/annotate/annotations/instances.json",
)
IMAGE_URL = os.environ.get(
    "IMAGE_URL",
    "https://huggingface.co/buckets/hamimmahmud0/SAM_COCO_v1_b2_3024/resolve/annotate/images/{filename}",
)
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
_lock = threading.Lock()
_jobs_lock = threading.Lock()
_dataset = None
_jobs = {}
_shape_jobs = {}
_shape_references = {}


def load_dataset():
    global _dataset
    with _lock:
        if _dataset is not None:
            return _dataset
        request = urllib.request.Request(COCO_URL, headers={"User-Agent": "coco-browser/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
        data = json.loads(payload)
        data["images"] = sorted(data.get("images", []), key=lambda item: (item.get("file_name", ""), item.get("id", 0)))
        annotation_index = {}
        for annotation in data.get("annotations", []):
            annotation_index.setdefault(annotation["image_id"], []).append(annotation)
        categories = data.get("categories", [])
        category_colors = {}
        palette = ["#f43f5e", "#22d3ee", "#a3e635", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]
        for offset, category in enumerate(categories):
            category_colors[category["id"]] = palette[offset % len(palette)]
        image_index = {image["id"]: image for image in data["images"]}
        summary = []
        for image in data["images"]:
            annotations = annotation_index.get(image["id"], [])
            summary.append(
                {
                    "id": image["id"],
                    "file_name": image["file_name"],
                    "width": image.get("width", 0),
                    "height": image.get("height", 0),
                    "annotation_count": len(annotations),
                    "annotation_ids": [annotation["id"] for annotation in annotations],
                    "category_counts": {
                        str(category_id): sum(1 for annotation in annotations if annotation.get("category_id") == category_id)
                        for category_id in category_colors
                    },
                }
            )
        _dataset = {
            "info": data.get("info", {}),
            "images": data["images"],
            "categories": categories,
            "annotations": data.get("annotations", []),
            "annotation_index": annotation_index,
            "image_index": image_index,
            "category_colors": category_colors,
            "summary": summary,
        }
        return _dataset


def decode_compressed_counts(value):
    counts = []
    position = 0
    while position < len(value):
        number = 0
        shift = 0
        code = 0
        more = True
        while more and position < len(value):
            code = ord(value[position]) - 48
            position += 1
            number |= (code & 0x1F) << (5 * shift)
            more = code & 0x20
            shift += 1
        if not more and code & 0x10:
            number |= -1 << (5 * shift)
        if len(counts) > 2:
            number += counts[-2]
        counts.append(number)
    return counts


def count_mask_islands(segmentation):
    if not segmentation:
        return 0
    if isinstance(segmentation, list):
        return sum(1 for polygon in segmentation if isinstance(polygon, list) and len(polygon) >= 6)
    counts = segmentation.get("counts")
    height, width = segmentation.get("size", [0, 0])
    if not counts or not height or not width:
        return 0
    if isinstance(counts, str):
        counts = decode_compressed_counts(counts)
    columns = {}
    position = 0
    total = height * width
    for run_index, count in enumerate(counts):
        if run_index % 2 == 0:
            position += count
            continue
        remaining = count
        while remaining > 0 and position < total:
            column, row = divmod(position, height)
            length = min(remaining, height - row)
            columns.setdefault(column, []).append((row, row + length))
            position += length
            remaining -= length
        position += remaining
    parent = []

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parent[right] = left

    previous_column = None
    previous = []
    for column in sorted(columns):
        current = []
        adjacent_previous = previous_column == column - 1
        for start, end in columns[column]:
            node = len(parent)
            parent.append(node)
            if adjacent_previous:
                for previous_start, previous_end, previous_node in previous:
                    if start < previous_end and end > previous_start:
                        union(node, previous_node)
            current.append((start, end, node))
        previous_column = column
        previous = current
    return len({find(node) for node in range(len(parent))})


SHAPE_DESCRIPTORS = {
    "aspect_ratio": "Aspect ratio",
    "compactness": "Compactness / circularity",
    "solidity": "Solidity",
    "convexity": "Convexity",
    "eccentricity": "Eccentricity",
    "normalized_perimeter": "Normalized perimeter",
    "hu_moments": "Hu moments",
    "zernike_moments": "Zernike moments",
    "fourier_descriptors": "Fourier descriptors",
    "hausdorff_distance": "Hausdorff distance",
    "chamfer_distance": "Chamfer distance",
}
_shape_references = {}


def mask_array(segmentation):
    if not isinstance(segmentation, dict):
        return None
    counts = segmentation.get("counts", [])
    if isinstance(counts, str):
        counts = decode_compressed_counts(counts)
    size = segmentation.get("size", [])
    if len(size) != 2 or not counts:
        return None
    height, width = size
    flat = np.zeros(height * width, dtype=np.uint8)
    position = 0
    for run_index, count in enumerate(counts):
        if run_index % 2:
            end = min(len(flat), position + count)
            flat[position:end] = 1
        position += count
    return flat.reshape((height, width), order="F").astype(bool)


def cropped_mask(annotation):
    mask = mask_array(annotation.get("segmentation"))
    if mask is None or not mask.any():
        return None
    rows, columns = np.where(mask)
    x0, x1 = int(columns.min()), int(columns.max()) + 1
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    return mask[y0:y1, x0:x1], (x0, y0)


def boundary_points(mask):
    padded = np.pad(mask, 1)
    return np.argwhere((padded[1:-1, 1:-1] & ~padded[:-2, 1:-1]) | (padded[1:-1, 1:-1] & ~padded[2:, 1:-1]) | (padded[1:-1, 1:-1] & ~padded[1:-1, :-2]) | (padded[1:-1, 1:-1] & ~padded[1:-1, 2:]))


def convex_hull(points):
    points = sorted({(int(x), int(y)) for x, y in points})
    if len(points) <= 2:
        return points
    def cross(origin, first, second):
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (first[1] - origin[1]) * (second[0] - origin[0])
    lower = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def polygon_area(points):
    if len(points) < 3:
        return 0.0
    return abs(sum(points[index][0] * points[(index + 1) % len(points)][1] - points[(index + 1) % len(points)][0] * points[index][1] for index in range(len(points)))) / 2


def polygon_perimeter(points):
    return sum(math.hypot(points[(index + 1) % len(points)][0] - point[0], points[(index + 1) % len(points)][1] - point[1]) for index, point in enumerate(points)) if len(points) > 1 else 0.0


def hu_moments(mask):
    rows, columns = np.where(mask)
    y = rows.astype(float)
    x = columns.astype(float)
    total = float(len(x))
    cy, cx = y.mean(), x.mean()
    y, x = y - cy, x - cx
    mu20, mu02, mu11 = (x * x).mean() * total, (y * y).mean() * total, (x * y).mean() * total
    mu30, mu03, mu21, mu12 = (x ** 3).mean() * total, (y ** 3).mean() * total, (x * x * y).mean() * total, (x * y * y).mean() * total
    norm = total ** 2
    nu20, nu02, nu11, nu30, nu03, nu21, nu12 = mu20 / norm, mu02 / norm, mu11 / norm, mu30 / norm**1.5, mu03 / norm**1.5, mu21 / norm**1.5, mu12 / norm**1.5
    hu1 = nu20 + nu02
    hu2 = (nu20 - nu02) ** 2 + 4 * nu11**2
    hu3 = (nu30 - 3 * nu12) ** 2 + (3 * nu21 - nu03) ** 2
    hu4 = (nu30 + nu12) ** 2 + (nu21 + nu03) ** 2
    hu5 = (nu30 - 3 * nu12) * (nu30 + nu12) * ((nu30 + nu12) ** 2 - 3 * (nu21 + nu03) ** 2) + (3 * nu21 - nu03) * (nu21 + nu03) * (3 * (nu30 + nu12) ** 2 - (nu21 + nu03) ** 2)
    hu6 = (nu20 - nu02) * ((nu30 + nu12) ** 2 - (nu21 + nu03) ** 2) + 4 * nu11 * (nu30 + nu12) * (nu21 + nu03)
    hu7 = (3 * nu21 - nu03) * (nu30 + nu12) * ((nu30 + nu12) ** 2 - 3 * (nu21 + nu03) ** 2) - (nu30 - 3 * nu12) * (nu21 + nu03) * (3 * (nu30 + nu12) ** 2 - (nu21 + nu03) ** 2)
    return [hu1, hu2, hu3, hu4, hu5, hu6, hu7]


def zernike_moments(mask):
    rows, columns = np.where(mask)
    y = rows.astype(float)
    x = columns.astype(float)
    center_y, center_x = y.mean(), x.mean()
    scale = math.sqrt((len(x) * len(x)) / math.pi)
    if scale == 0:
        return [0.0] * 9
    y = (y - center_y) / scale
    x = (x - center_x) / scale
    radius = np.sqrt(x * x + y * y)
    valid = radius <= 1
    x, y, radius = x[valid], y[valid], radius[valid]
    moments = []
    for order, terms in ((0, [0]), (1, [-1, 1]), (2, [-2, 0, 2]), (3, [-3, -1, 1, 3]), (4, [-4, -2, 0, 2, 4])):
        for power in terms:
            polynomial = sum(((-1) ** k) * math.factorial(order - k) / (math.factorial(k) * math.factorial((order + power) // 2) * math.factorial((order - power) // 2)) * radius ** (order - 2 * k) for k in range((order - abs(power)) // 2 + 1))
            value = np.sum((x + 1j * y) ** power * polynomial) / max(1, len(x))
            moments.append(float(abs(value)))
    return moments[:9]


def fourier_descriptors(mask):
    points = boundary_points(mask)
    if len(points) < 2:
        return [0.0] * 16
    center_y, center_x = points[:, 0].mean(), points[:, 1].mean()
    angles = np.arctan2(points[:, 0] - center_y, points[:, 1] - center_x)
    ordered = points[np.argsort(angles)]
    radius = np.hypot(ordered[:, 0] - center_y, ordered[:, 1] - center_x)
    spectrum = np.abs(np.fft.rfft(radius - radius.mean())) / max(1, len(radius))
    return [float(value) for value in spectrum[1:17]]


def reference_distances(mask, reference, distance):
    points = boundary_points(mask).astype(float)
    other = boundary_points(reference).astype(float)
    if len(points) > 128:
        points = points[np.linspace(0, len(points) - 1, 128).astype(int)]
    if len(other) > 128:
        other = other[np.linspace(0, len(other) - 1, 128).astype(int)]
    if not len(points) or not len(other):
        return 0.0
    forward = np.sqrt(((points[:, None, :] - other[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
    backward = np.sqrt(((other[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
    return float(forward.max() if distance == "hausdorff_distance" else (forward.mean() + backward.mean()) / 2)


def rle_columns(segmentation):
    if not isinstance(segmentation, dict):
        return {}, 0
    counts = segmentation.get("counts", [])
    if isinstance(counts, str):
        counts = decode_compressed_counts(counts)
    height, width = segmentation.get("size", [0, 0])
    if not counts or not height or not width:
        return {}, 0
    columns = {}
    area = 0
    position = 0
    for run_index, count in enumerate(counts):
        if run_index % 2 == 0:
            position += count
            continue
        area += count
        remaining = count
        while remaining > 0 and position < height * width:
            column, row = divmod(position, height)
            length = min(remaining, height - row)
            columns.setdefault(column, []).append((row, row + length))
            position += length
            remaining -= length
        position += remaining
    return columns, area


def interval_symmetric_length(left, right):
    total = 0
    for start, end in left:
        for other_start, other_end in right:
            total += max(0, min(end, other_end) - max(start, other_start))
    return sum(end - start for start, end in left) + sum(end - start for start, end in right) - 2 * total


def rle_perimeter(columns):
    perimeter = 2 * sum(len(intervals) for intervals in columns.values())
    ordered = sorted(columns)
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + 1:
            perimeter += interval_symmetric_length(columns[previous], columns[current])
    return float(perimeter)


def rle_hu_moments(columns, area):
    sums = np.zeros(9, dtype=float)
    for column, intervals in columns.items():
        for start, end in intervals:
            count = end - start
            y1, y0 = end - 1, start - 1
            sy = (y1 * (y1 + 1) - y0 * (y0 + 1)) / 2
            sy2 = (y1 * (y1 + 1) * (2 * y1 + 1) - y0 * (y0 + 1) * (2 * y0 + 1)) / 6
            sy3 = (y1 * (y1 + 1) / 2) ** 2 - (y0 * (y0 + 1) / 2) ** 2
            sums += [count, column * count, column**2 * count, column**3 * count, column * sy, column**2 * sy, column * sy2, sy3, column * sy2]
    total = float(area)
    cx, cy = sums[1] / total, sums[2] / total
    x, y = sums[3] - 3 * cx * sums[1] + 3 * cx**2 * total - cx**3 * total, sums[7] - 3 * cy * sums[2] + 3 * cy**2 * total - cy**3 * total
    x2y = sums[5] - 2 * cx * sums[4] + cx**2 * sums[2] - cx**3 * total
    xy2 = sums[6] - 2 * cy * sums[4] + cy**2 * sums[1] - cy**3 * total
    mu20, mu02, mu11 = x / total, y / total, (sums[4] - cx * sums[2] - cy * sums[1] + cx * cy * total) / total
    mu30, mu03, mu21, mu12 = x2y / total, xy2 / total, (sums[6] - cx * sums[5] - 2 * cy * sums[4] + 2 * cx * cy * sums[2] + cy**2 * sums[1] - cx * cy**2 * total) / total, (sums[8] - cy * sums[7] - 2 * cx * sums[4] + 2 * cx * cy * sums[2] + cx**2 * sums[2] - cx**2 * cy * total) / total
    norm = total**2
    nu20, nu02, nu11 = mu20 / norm, mu02 / norm, mu11 / norm
    nu30, nu03, nu21, nu12 = mu30 / norm**1.5, mu03 / norm**1.5, mu21 / norm**1.5, mu12 / norm**1.5
    hu1 = nu20 + nu02
    hu2 = (nu20 - nu02) ** 2 + 4 * nu11**2
    hu3 = (nu30 - 3 * nu12) ** 2 + (3 * nu21 - nu03) ** 2
    hu4 = (nu30 + nu12) ** 2 + (nu21 + nu03) ** 2
    hu5 = (nu30 - 3 * nu12) * (nu30 + nu12) * ((nu30 + nu12) ** 2 - 3 * (nu21 + nu03) ** 2) + (3 * nu21 - nu03) * (nu21 + nu03) * (3 * (nu30 + nu12) ** 2 - (nu21 + nu03) ** 2)
    hu6 = (nu20 - nu02) * ((nu30 + nu12) ** 2 - (nu21 + nu03) ** 2) + 4 * nu11 * (nu30 + nu12) * (nu21 + nu03)
    hu7 = (3 * nu21 - nu03) * (nu30 + nu12) * ((nu30 + nu12) ** 2 - 3 * (nu21 + nu03) ** 2) - (nu30 - 3 * nu12) * (nu21 + nu03) * (3 * (nu30 + nu12) ** 2 - (nu21 + nu03) ** 2)
    return [hu1, hu2, hu3, hu4, hu5, hu6, hu7]


def shape_values(annotation, descriptors, reference=None):
    columns, area = rle_columns(annotation.get("segmentation"))
    if not area:
        return {}
    bbox = annotation.get("bbox", [0, 0, 1, 1])
    width, height = max(1, bbox[2]), max(1, bbox[3])
    perimeter = rle_perimeter(columns)
    values = {}
    for descriptor in descriptors:
        if descriptor == "aspect_ratio":
            values[descriptor] = float(width / height)
        elif descriptor == "compactness":
            values[descriptor] = float(4 * math.pi * area / max(1.0, perimeter**2))
        elif descriptor == "normalized_perimeter":
            values[descriptor] = float(perimeter / math.sqrt(area))
        elif descriptor == "hu_moments":
            values[descriptor] = rle_hu_moments(columns, area)
    advanced = set(descriptors) - {"aspect_ratio", "compactness", "normalized_perimeter", "hu_moments"}
    if not advanced:
        return values
    cropped = cropped_mask(annotation)
    if cropped is None:
        return values
    mask, _ = cropped
    points = boundary_points(mask)
    for descriptor in advanced:
        if descriptor == "solidity":
            hull = convex_hull(points[:, ::-1])
            values[descriptor] = float(area / max(1.0, polygon_area(hull)))
        elif descriptor == "convexity":
            hull = convex_hull(points[:, ::-1])
            values[descriptor] = float(min(1.0, polygon_perimeter(hull) / max(1.0, perimeter)))
        elif descriptor == "eccentricity":
            rows, columns_pixels = np.where(mask)
            covariance = np.cov(np.vstack([columns_pixels, rows])) if len(rows) > 1 else np.zeros((2, 2))
            eigenvalues = np.linalg.eigvalsh(covariance)
            major, minor = max(eigenvalues), max(0.0, min(eigenvalues))
            values[descriptor] = float(math.sqrt(max(0.0, 1 - minor / major))) if major else 0.0
        elif descriptor == "zernike_moments":
            values[descriptor] = zernike_moments(mask)
        elif descriptor == "fourier_descriptors":
            values[descriptor] = fourier_descriptors(mask)
        elif descriptor in {"hausdorff_distance", "chamfer_distance"} and reference is not None:
            values[descriptor] = reference_distances(mask, reference, descriptor)
    return values


def apply_mask_strokes(segmentation, strokes):
    if not segmentation or not strokes:
        return segmentation
    size = segmentation.get("size", [])
    if len(size) != 2:
        return segmentation
    height, width = size
    counts = segmentation.get("counts", [])
    if isinstance(counts, str):
        counts = decode_compressed_counts(counts)
    mask = bytearray(height * width)
    position = 0
    for run_index, count in enumerate(counts):
        if run_index % 2:
            end = min(len(mask), position + count)
            mask[position:end] = b"\x01" * (end - position)
        position += count
    for stroke in strokes:
        points = stroke.get("points", [])
        radius = max(1, int(stroke.get("radius", 1)))
        operation = stroke.get("operation", "add")
        for index, point in enumerate(points):
            previous = points[index - 1] if index else point
            distance = max(1, int(((point["x"] - previous["x"]) ** 2 + (point["y"] - previous["y"]) ** 2) ** 0.5))
            steps = max(1, int(distance / max(1, radius / 2)))
            for step in range(steps + 1):
                ratio = step / steps
                x = int(previous["x"] + (point["x"] - previous["x"]) * ratio)
                y = int(previous["y"] + (point["y"] - previous["y"]) * ratio)
                for yy in range(max(0, y - radius), min(height, y + radius + 1)):
                    for xx in range(max(0, x - radius), min(width, x + radius + 1)):
                        if (xx - x) ** 2 + (yy - y) ** 2 <= radius**2:
                            mask[xx * height + yy] = 0 if operation == "remove" else 1
    encoded = []
    current = 0
    run = 0
    for value in mask:
        if value == current:
            run += 1
        else:
            encoded.append(run)
            current = 1 - current
            run = 1
    encoded.append(run)
    return {"size": [height, width], "counts": encoded}


def build_island_summary(values, elapsed):
    total_instances = len(values)
    total_islands = sum(values)
    ordered = sorted(values)
    midpoint = total_instances // 2
    median = ordered[midpoint] if total_instances % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    frequency = {}
    for value in values:
        frequency[value] = frequency.get(value, 0) + 1
    mode = max(frequency, key=frequency.get) if frequency else 0
    distribution = [
        {
            "islands": value,
            "instances": count,
            "percentage": round(count * 100 / total_instances, 4) if total_instances else 0,
        }
        for value, count in sorted(frequency.items())
    ]
    return {
        "instances": total_instances,
        "total_islands": total_islands,
        "mean_islands": round(total_islands / total_instances, 4) if total_instances else 0,
        "median_islands": median,
        "mode_islands": mode,
        "minimum_islands": ordered[0] if ordered else 0,
        "maximum_islands": ordered[-1] if ordered else 0,
        "single_island_instances": frequency.get(1, 0),
        "multi_island_instances": sum(count for value, count in frequency.items() if value > 1),
        "zero_island_instances": frequency.get(0, 0),
        "elapsed_seconds": round(elapsed, 3),
        "distribution": distribution,
    }


def update_job(job_id, **values):
    with _jobs_lock:
        _jobs[job_id].update(values)


def available_cpu_count():
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def count_island_range(bounds):
    start, end = bounds
    data = _dataset or load_dataset()
    overall = Counter()
    categories = {}
    instances = {}
    for annotation in data["annotations"][start:end]:
        islands = count_mask_islands(annotation.get("segmentation"))
        overall[islands] += 1
        category = str(annotation.get("category_id"))
        categories.setdefault(category, Counter())[islands] += 1
        instances.setdefault(str(islands), []).append(
            {
                "annotation_id": annotation["id"],
                "image_id": annotation["image_id"],
                "category_id": annotation.get("category_id"),
                "score": annotation.get("score"),
            }
        )
    return {
        "overall": dict(overall),
        "categories": {key: dict(value) for key, value in categories.items()},
        "instances": instances,
        "processed": end - start,
    }


def merge_counters(target, source):
    for value, count in source.items():
        target[int(value)] += count


def run_island_frequency(job_id, workers):
    started = time.monotonic()
    try:
        data = load_dataset()
        annotations = data["annotations"]
        total = len(annotations)
        worker_count = max(1, min(available_cpu_count(), workers or available_cpu_count(), total or 1))
        chunk_target = worker_count * 4
        chunk_size = max(1, (total + chunk_target - 1) // chunk_target)
        bounds = [(start, min(start + chunk_size, total)) for start in range(0, total, chunk_size)]
        update_job(job_id, status="running", total=total, processed=0, progress=0, workers=worker_count, available_cpus=available_cpu_count())
        overall = Counter()
        category_counters = {}
        instance_groups = {}
        processed = 0
        pool = get_context("fork").Pool(processes=worker_count)
        try:
            for result in pool.imap_unordered(count_island_range, bounds):
                merge_counters(overall, result["overall"])
                for category, values in result["categories"].items():
                    merge_counters(category_counters.setdefault(category, Counter()), values)
                for island_count, instances in result["instances"].items():
                    instance_groups.setdefault(int(island_count), []).extend(instances)
                processed += result["processed"]
                update_job(
                    job_id,
                    processed=processed,
                    progress=round(processed * 100 / total, 3) if total else 100,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
        finally:
            pool.terminate()
            pool.join()
        overall_values = [value for value, count in overall.items() for _ in range(count)]
        categories = [
            {
                "id": category["id"],
                "name": category["name"],
                "summary": build_island_summary(
                    [value for value, count in category_counters.get(str(category["id"]), Counter()).items() for _ in range(count)],
                    time.monotonic() - started,
                ),
            }
            for category in data["categories"]
        ]
        update_job(
            job_id,
            status="completed",
            processed=total,
            progress=100,
            result={
                "summary": build_island_summary(overall_values, time.monotonic() - started),
                "categories": categories,
                "inspection_groups": [
                    {"islands": island_count, "instances": sorted(instances, key=lambda item: item["annotation_id"])}
                    for island_count, instances in sorted(instance_groups.items())
                ],
                "workers": worker_count,
                "available_cpus": available_cpu_count(),
            },
        )
    except Exception as error:
        update_job(job_id, status="error", error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))


def start_island_frequency_job(workers=0):
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] in {"queued", "running"}:
                return job["id"]
        job_id = uuid.uuid4().hex
        if len(_jobs) >= 20:
            completed = sorted(
                (job for job in _jobs.values() if job["status"] in {"completed", "error"}),
                key=lambda job: job["created_at"],
            )
            for job in completed[: len(_jobs) - 19]:
                _jobs.pop(job["id"], None)
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "processed": 0,
            "total": 0,
            "progress": 0,
            "workers": 0,
            "created_at": time.time(),
            "started_at": None,
        }
    threading.Thread(target=run_island_frequency, args=(job_id, workers), daemon=True).start()
    return job_id


def descriptor_items(values):
    items = {}
    for descriptor, value in values.items():
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                items[f"{descriptor}_{index}"] = float(item)
        else:
            items[descriptor] = float(value)
    return items


def build_shape_summary(values, elapsed):
    grouped = {}
    vector_descriptors = {"hu_moments", "zernike_moments", "fourier_descriptors"}
    for class_values in values.values():
        for descriptor, raw_values in class_values.items():
            if descriptor in vector_descriptors:
                for vector in raw_values:
                    for index, value in enumerate(vector, start=1):
                        grouped.setdefault(f"{descriptor}_{index}", []).append(float(value))
            else:
                grouped.setdefault(descriptor, []).extend(float(value) for value in raw_values)
    descriptors = {}
    for key, entries in grouped.items():
        array = np.asarray([float(value) for value in entries if value is not None and math.isfinite(float(value))], dtype=float)
        if not len(array):
            continue
        base = next((name for name in SHAPE_DESCRIPTORS if key == name or key.startswith(f"{name}_")), key)
        if float(np.min(array)) == float(np.max(array)):
            edges = np.asarray([float(np.min(array)), float(np.min(array)) + 1])
            counts = np.asarray([len(array)])
        else:
            counts, edges = np.histogram(array, bins=min(20, max(1, len(array))))
        descriptors[key] = {
            "label": SHAPE_DESCRIPTORS.get(base, base),
            "count": len(array),
            "mean": float(np.mean(array)),
            "std": float(np.std(array)),
            "min": float(np.min(array)),
            "p05": float(np.percentile(array, 5)),
            "median": float(np.percentile(array, 50)),
            "p95": float(np.percentile(array, 95)),
            "max": float(np.max(array)),
            "histogram": [[float(edges[index]), float(edges[index + 1]), int(counts[index])] for index in range(len(counts))],
        }
    return {"elapsed_seconds": round(elapsed, 3), "descriptors": descriptors}


def shape_range(arguments):
    indices, descriptors, class_ids = arguments
    data = _dataset or load_dataset()
    class_values = {}
    skipped = 0
    for index in indices:
        annotation = data["annotations"][index]
        category_id = str(annotation.get("category_id"))
        values = shape_values(annotation, descriptors, _shape_references.get(category_id))
        if not values:
            skipped += 1
            continue
        target = class_values.setdefault(category_id, {})
        for key, value in values.items():
            target.setdefault(key, []).append(value)
    return {"class_values": class_values, "processed": len(indices), "skipped": skipped}


def run_shape_descriptors(job_id, descriptors, class_ids, workers):
    global _shape_references
    started = time.monotonic()
    try:
        data = load_dataset()
        selected = [annotation for annotation in data["annotations"] if not class_ids or str(annotation.get("category_id")) in class_ids]
        selected_indices = [index for index, annotation in enumerate(data["annotations"]) if not class_ids or str(annotation.get("category_id")) in class_ids]
        total = len(selected_indices)
        worker_count = max(1, min(available_cpu_count(), workers or available_cpu_count(), total or 1))
        _shape_references = {}
        if "hausdorff_distance" in descriptors or "chamfer_distance" in descriptors:
            for category in data["categories"]:
                category_id = str(category["id"])
                if class_ids and category_id not in class_ids:
                    continue
                reference = next((annotation for annotation in selected if str(annotation.get("category_id")) == category_id), None)
                if reference:
                    cropped = cropped_mask(reference)
                    if cropped:
                        _shape_references[category_id] = cropped[0]
        chunk_target = worker_count * 4
        chunk_size = max(1, (total + chunk_target - 1) // chunk_target)
        bounds = [selected_indices[start : start + chunk_size] for start in range(0, total, chunk_size)]
        update_shape_job(job_id, status="running", total=total, processed=0, progress=0, workers=worker_count, available_cpus=available_cpu_count())
        merged = {}
        skipped = 0
        processed = 0
        pool = get_context("fork").Pool(processes=worker_count)
        try:
            arguments = [(bound, descriptors, class_ids) for bound in bounds]
            for result in pool.imap_unordered(shape_range, arguments):
                processed += result["processed"]
                skipped += result["skipped"]
                for category_id, values in result["class_values"].items():
                    target = merged.setdefault(category_id, {})
                    for descriptor, descriptor_values in values.items():
                        target.setdefault(descriptor, []).extend(descriptor_values)
                update_shape_job(job_id, processed=processed, progress=round(processed * 100 / total, 3) if total else 100, elapsed_seconds=round(time.monotonic() - started, 3))
        finally:
            pool.terminate()
            pool.join()
        categories = [
            {"id": category["id"], "name": category["name"], "summary": build_shape_summary({category["id"]: merged.get(str(category["id"]), {})}, time.monotonic() - started)}
            for category in data["categories"]
            if not class_ids or str(category["id"]) in class_ids
        ]
        update_shape_job(job_id, status="completed", processed=total, progress=100, result={"descriptors": descriptors, "class_ids": class_ids, "categories": categories, "skipped": skipped, "processed": total, "workers": worker_count})
    except Exception:
        update_shape_job(job_id, status="error", error=traceback.format_exc(), elapsed_seconds=round(time.monotonic() - started, 3))


def update_shape_job(job_id, **values):
    with _jobs_lock:
        _shape_jobs[job_id].update(values)


def start_shape_descriptor_job(payload):
    descriptors = payload.get("descriptors", [])
    class_ids = [str(value) for value in payload.get("class_ids", [])]
    invalid = [descriptor for descriptor in descriptors if descriptor not in SHAPE_DESCRIPTORS]
    if not descriptors or invalid:
        raise ValueError("Select at least one valid descriptor")
    with _jobs_lock:
        for job in _shape_jobs.values():
            if job["status"] in {"queued", "running"}:
                return job["id"]
        job_id = uuid.uuid4().hex
        _shape_jobs[job_id] = {"id": job_id, "status": "queued", "processed": 0, "total": 0, "progress": 0, "workers": 0, "created_at": time.time(), "descriptors": descriptors, "class_ids": class_ids}
    threading.Thread(target=run_shape_descriptors, args=(job_id, descriptors, class_ids, WORKERS), daemon=True).start()
    return job_id


def get_shape_descriptor_job(job_id):
    with _jobs_lock:
        job = _shape_jobs.get(job_id)
        return dict(job) if job else None


def get_island_frequency_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/tools/island-frequency/start":
            self.send_json({"job_id": start_island_frequency_job(WORKERS)})
            return
        if path == "/api/tools/shape-descriptors/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json({"job_id": start_shape_descriptor_job(payload)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path != "/api/export":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10_000_000:
                raise ValueError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            body = self.build_export(payload).encode("utf-8")
            self.send_bytes(
                body,
                "application/json",
                {"Content-Disposition": 'attachment; filename="instances-reviewed.json"'},
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, str(error))

    def build_export(self, payload):
        data = load_dataset()
        reviews = payload.get("reviews", {})
        edits = payload.get("edits", {})
        mask_edits = payload.get("mask_edits", {})
        created = payload.get("created", [])
        image_ids = set(payload.get("image_ids", []))
        annotations = []
        for annotation in data["annotations"]:
            if image_ids and annotation["image_id"] not in image_ids:
                continue
            review = reviews.get(str(annotation["id"]))
            if review == "remove":
                continue
            output = dict(annotation)
            edit = edits.get(str(annotation["id"]))
            if edit:
                output.update(edit)
            mask_strokes = mask_edits.get(str(annotation["id"]))
            if mask_strokes:
                output["segmentation"] = apply_mask_strokes(annotation.get("segmentation"), mask_strokes)
            if review:
                output["review"] = review
            annotations.append(output)
        annotations.extend(annotation for annotation in created if not image_ids or annotation.get("image_id") in image_ids)
        images = data["images"] if not image_ids else [image for image in data["images"] if image["id"] in image_ids]
        return json.dumps(
            {
                "info": data["info"],
                "images": images,
                "annotations": annotations,
                "categories": data["categories"],
            },
            separators=(",", ":"),
        )

    def handle_request(self, send_body=True):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/dataset":
                data = load_dataset()
                body = {
                    "info": data["info"],
                    "categories": data["categories"],
                    "category_colors": data["category_colors"],
                    "images": data["summary"],
                    "annotation_count": len(data["annotations"]),
                    "max_annotation_id": max((annotation["id"] for annotation in data["annotations"]), default=0),
                }
                self.send_json(body, send_body)
                return
            if path.startswith("/api/media/"):
                image_id = int(path.removeprefix("/api/media/"))
                self.proxy_image(image_id, send_body)
                return
            if path.startswith("/api/image/"):
                image_id = int(path.removeprefix("/api/image/"))
                data = load_dataset()
                image = data["image_index"].get(image_id)
                if image is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = {
                    "image": image,
                    "image_url": f"/api/media/{image_id}",
                    "source_url": IMAGE_URL.format(filename=image["file_name"]),
                    "annotations": data["annotation_index"].get(image_id, []),
                    "categories": data["categories"],
                    "category_colors": data["category_colors"],
                }
                self.send_json(body, send_body)
                return
            if path == "/api/tools/shape-descriptors/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                job = get_shape_descriptor_job(job_id)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Shape Descriptor job not found")
                    return
                self.send_json(job, send_body)
                return
            if path == "/api/tools/island-frequency/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                job = get_island_frequency_job(job_id)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Island Frequency job not found")
                    return
                self.send_json(job, send_body)
                return
            if path == "/api/health":
                self.send_json({"status": "ok"}, send_body)
                return
            self.serve_static(path, send_body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            self.send_error(HTTPStatus.BAD_GATEWAY, f"Unable to load dataset: {error}")

    def proxy_image(self, image_id, send_body=True):
        data = load_dataset()
        image = data["image_index"].get(image_id)
        if image is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        source = IMAGE_URL.format(filename=image["file_name"])
        headers = {"User-Agent": "coco-browser/1.0", "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        request = urllib.request.Request(source, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                self.send_response(response.getcode())
                self.send_header("Content-Type", response.headers.get_content_type())
                if response.headers.get("Content-Length"):
                    self.send_header("Content-Length", response.headers["Content-Length"])
                self.send_header("Accept-Ranges", response.headers.get("Accept-Ranges", "bytes"))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                if send_body:
                    while chunk := response.read(256 * 1024):
                        self.wfile.write(chunk)
        except urllib.error.HTTPError as error:
            self.send_error(error.code, f"Image source returned {error.code}")

    def send_json(self, data, send_body=True):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", send_body=send_body)

    def send_bytes(self, body, content_type, extra=None, send_body=True):
        headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
        if extra:
            headers.update(extra)
        self.send_response(HTTPStatus.OK)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def serve_static(self, path, send_body=True):
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (STATIC / relative).resolve()
        if STATIC.resolve() not in candidate.parents or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}", flush=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Host the COCO 1.0 annotation browser.")
    parser.add_argument("--host", default=HOST, help=f"interface to bind (default: {HOST})")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to bind (default: {PORT})")
    parser.add_argument("--coco-url", default=COCO_URL, help="COCO instances JSON URL")
    parser.add_argument("--image-url", default=IMAGE_URL, help="image URL template containing {filename}")
    parser.add_argument("--workers", type=int, default=WORKERS, help="Island Frequency workers; 0 uses all available CPUs")
    return parser


def main():
    global COCO_URL, IMAGE_URL, WORKERS
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.workers < 0:
        parser.error("--workers must be 0 or greater")
    if "{filename}" not in args.image_url:
        parser.error("--image-url must contain {filename}")
    COCO_URL = args.coco_url
    IMAGE_URL = args.image_url
    WORKERS = args.workers
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"COCO browser listening on http://{args.host}:{args.port}", flush=True)
    print(f"COCO source: {COCO_URL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
