import argparse
import getpass
import hashlib
import json
import math
from collections import Counter
from multiprocessing import get_context
import mimetypes
import numpy as np
import os
import re
import secrets
import random
import sys
import threading
import traceback
import time
import uuid
import urllib.error
import urllib.request
import yaml
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from auth_store import AuthStore

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
DEFAULT_HF_SOURCE = "hf://buckets/hamimmahmud0/SAM_COCO_v1_b2_3024/annotate"
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CONFIG_PATH = ROOT / "config.yaml"


def load_app_config():
    try:
        with CONFIG_PATH.open(encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    return data if isinstance(data, dict) else {}


APP_CONFIG = load_app_config()
APP_NAME = str(APP_CONFIG.get("app", {}).get("name", "coco-browser"))
PROJECT_CONFIG = APP_CONFIG.get("project", {}) if isinstance(APP_CONFIG.get("project", {}), dict) else {}
PROJECT_CACHE_DIR = Path(str(APP_CONFIG.get("app", {}).get("cache_dir", f"~/.cache/{APP_NAME}"))).expanduser()
PROJECT_NAME = os.environ.get("PROJECT_NAME", str(PROJECT_CONFIG.get("name", "default")))
PROJECT_DIR = PROJECT_CACHE_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "-", PROJECT_NAME).strip("-") or "default")
DATASET_DIR = PROJECT_DIR / "dataset"
AUTH_STORE = AuthStore(PROJECT_CACHE_DIR / "auth.sqlite3")
PROJECT_SOURCE = ""
PROJECT_TOKEN = None
PROJECT_AUTOSAVE = True
PROJECT_ID = None
USE_LOCAL_DATASET = True
DATASET_SOURCE = ""
_lock = threading.Lock()
_jobs_lock = threading.Lock()
_dataset = None
_jobs = {}
_shape_jobs = {}
_cleanup_jobs = {}
_island_cleanup_jobs = {}
_project_save_jobs = {}
_project_save_lock = threading.Lock()
_collaboration_lock = threading.Lock()
_shape_references = {}
_job_cancel_events = {}
_active_pools = {}


def local_dataset_annotation_path():
    return DATASET_DIR / "annotations" / "instances.json"


def local_dataset_image_path(image):
    return DATASET_DIR / "images" / Path(image["file_name"]).name


def dataset_image_path(dataset_dir, image):
    return dataset_dir / "images" / Path(image["file_name"]).name


def validate_dataset_directory(dataset_dir):
    annotation_path = dataset_dir / "annotations" / "instances.json"
    images_dir = dataset_dir / "images"
    if not annotation_path.is_file() or not images_dir.is_dir():
        return False
    try:
        with annotation_path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("images")) and all(dataset_image_path(dataset_dir, image).is_file() for image in data["images"])


def validate_local_dataset():
    return validate_dataset_directory(DATASET_DIR)


def normalize_hf_source(source):
    source = source.strip()
    if source.startswith("hf://buckets/"):
        return source
    if source.startswith("hf://"):
        raise ValueError("HF source must use hf://buckets/<owner>/<bucket>/<prefix>")
    return f"hf://buckets/{source.lstrip('/')}"


def sync_hf_directory(source, destination, token=None, include=None):
    try:
        from huggingface_hub import sync_bucket
    except ImportError as error:
        raise RuntimeError("huggingface_hub is required to synchronize Hugging Face buckets") from error
    source = str(source) if isinstance(source, Path) else normalize_hf_source(source)
    sync_bucket(
        source=source,
        dest=str(destination),
        delete=False,
        include=include,
        token=token or os.environ.get("HF_TOKEN"),
        quiet=False,
    )
    return source


def sync_hf_dataset(source, token=None):
    source = sync_hf_directory(source, DATASET_DIR, token)
    if not validate_local_dataset():
        raise RuntimeError(f"Dataset synchronization completed but {local_dataset_annotation_path()} is missing or invalid")
    return source


def project_manifest_path():
    return PROJECT_DIR / "project.json"


def read_project_manifest():
    try:
        with project_manifest_path().open(encoding="utf-8") as file:
            document = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) and document.get("schema_version") == 1 else None


def project_bucket_id(source):
    normalized = normalize_hf_source(source)
    bucket_path = normalized.removeprefix("hf://buckets/").strip("/")
    parts = [part for part in bucket_path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("Project bucket must be owner/bucket without a prefix")
    return "/".join(parts)


def ensure_project_bucket(source, token=None, allow_override=True):
    from huggingface_hub import bucket_info, create_bucket, get_bucket_file_metadata
    from huggingface_hub.errors import BucketNotFoundError, EntryNotFoundError

    bucket_id = project_bucket_id(source)
    normalized = f"hf://buckets/{bucket_id}"
    try:
        bucket_info(bucket_id, token=token or None)
    except BucketNotFoundError:
        create_bucket(bucket_id, private=bool(PROJECT_CONFIG.get("private", True)), exist_ok=True, token=token or None)
        return normalized, False
    try:
        get_bucket_file_metadata(bucket_id, "project.json", token=token or None)
    except EntryNotFoundError:
        if allow_override and sys.stdin.isatty():
            if input(f"Bucket {bucket_id} exists but is not a project. Override it? [y/N]: ").strip().lower() not in {"y", "yes"}:
                raise RuntimeError(f"Bucket {bucket_id} is not a project bucket")
        elif allow_override:
            raise RuntimeError(f"Bucket {bucket_id} exists but is not a project; use an interactive terminal to confirm override")
        else:
            raise RuntimeError(f"Bucket {bucket_id} exists but is not a project bucket")
        return normalized, False
    return normalized, True


def prepare_local_project_dataset(token=None):
    global DATASET_DIR
    if validate_dataset_directory(DATASET_DIR):
        return
    legacy_dataset = ROOT / "dataset"
    if APP_CONFIG.get("app", {}).get("adopt_workspace_dataset", True) and validate_dataset_directory(legacy_dataset):
        DATASET_DIR.parent.mkdir(parents=True, exist_ok=True)
        if not DATASET_DIR.exists():
            DATASET_DIR.symlink_to(legacy_dataset, target_is_directory=True)
        if validate_dataset_directory(DATASET_DIR):
            return
    dataset_source = PROJECT_CONFIG.get("dataset_source") or DEFAULT_HF_SOURCE
    sync_hf_directory(dataset_source, DATASET_DIR, token)
    if not validate_dataset_directory(DATASET_DIR):
        raise RuntimeError(f"Dataset import completed but {local_dataset_annotation_path()} is missing or invalid")


def prepare_project(project_dir=None, project_name=None, project_source=None, project_token=None, no_prompt=False):
    global DATASET_DIR, PROJECT_DIR, PROJECT_SOURCE, PROJECT_TOKEN, PROJECT_ID, PROJECT_AUTOSAVE, USE_LOCAL_DATASET, DATASET_SOURCE, PROJECT_NAME
    if project_name:
        PROJECT_NAME = project_name
    if project_dir:
        PROJECT_DIR = Path(project_dir).expanduser().resolve()
    else:
        PROJECT_DIR = PROJECT_CACHE_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "-", PROJECT_NAME).strip("-") or "default")
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    DATASET_DIR = PROJECT_DIR / "dataset"
    PROJECT_TOKEN = project_token or os.environ.get(str(PROJECT_CONFIG.get("token_env", "HF_TOKEN")))
    manifest = read_project_manifest()
    project_metadata = manifest.get("project", {}) if manifest else {}
    source = project_source or os.environ.get("HF_PROJECT_BUCKET") or os.environ.get("HF_PROJECT_SOURCE") or PROJECT_CONFIG.get("bucket") or project_metadata.get("bucket")
    if not source and not no_prompt and sys.stdin.isatty():
        source = input("Project bucket ID [owner/bucket]: ").strip()
    if source and not PROJECT_TOKEN and not no_prompt and sys.stdin.isatty():
        PROJECT_TOKEN = getpass.getpass("HF token: ").strip() or None
    if source and PROJECT_TOKEN:
        PROJECT_SOURCE, remote_project = ensure_project_bucket(source, PROJECT_TOKEN, allow_override=not no_prompt)
        if remote_project:
            if not manifest or not validate_dataset_directory(DATASET_DIR):
                print(f"Project bucket: {PROJECT_SOURCE}; importing project workspace...", flush=True)
                sync_hf_directory(PROJECT_SOURCE, PROJECT_DIR, PROJECT_TOKEN)
                manifest = read_project_manifest()
            if not manifest or not validate_dataset_directory(DATASET_DIR):
                raise RuntimeError("Project bucket is missing project.json or dataset/")
        else:
            prepare_local_project_dataset(PROJECT_TOKEN)
    elif source and not (no_prompt and manifest and validate_dataset_directory(DATASET_DIR)):
        raise ValueError("An HF token is required for project bucket access")
    else:
        if not manifest:
            if no_prompt or not sys.stdin.isatty():
                prepare_local_project_dataset(PROJECT_TOKEN)
            else:
                raise RuntimeError("A project bucket is required on first run")
        elif not validate_dataset_directory(DATASET_DIR):
            prepare_local_project_dataset(PROJECT_TOKEN)
    manifest = read_project_manifest() or {}
    project_metadata = manifest.get("project", {})
    PROJECT_ID = project_metadata.get("id")
    PROJECT_SOURCE = PROJECT_SOURCE or project_metadata.get("bucket", "")
    PROJECT_AUTOSAVE = bool(project_metadata.get("autosave", True))
    if not validate_dataset_directory(DATASET_DIR):
        raise RuntimeError(f"Project dataset is missing from {DATASET_DIR}")
    USE_LOCAL_DATASET = True
    DATASET_SOURCE = str(local_dataset_annotation_path())
    return True


def save_project_document(document, autosave=False):
    global PROJECT_SOURCE, PROJECT_TOKEN, PROJECT_AUTOSAVE, PROJECT_ID
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Unsupported project document")
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    metadata = document.setdefault("project", {})
    PROJECT_ID = metadata.get("id") or str(uuid.uuid4())
    metadata["id"] = PROJECT_ID
    metadata.setdefault("name", "COCO project")
    metadata["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    PROJECT_AUTOSAVE = bool(metadata.get("autosave", PROJECT_AUTOSAVE))
    metadata["autosave"] = PROJECT_AUTOSAVE
    if PROJECT_SOURCE:
        metadata["bucket"] = PROJECT_SOURCE
    temporary_path = project_manifest_path().with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    temporary_path.replace(project_manifest_path())
    remote = bool(PROJECT_SOURCE and PROJECT_TOKEN)
    if remote:
        read_collaboration()
        sync_hf_directory(DATASET_DIR, f"{PROJECT_SOURCE}/dataset", PROJECT_TOKEN)
        sync_hf_directory(PROJECT_DIR, PROJECT_SOURCE, PROJECT_TOKEN, include=["project.json", "collaboration.json", "analysis-cache.json"])
    return {"saved": True, "project_id": PROJECT_ID, "autosave": autosave, "remote": remote}


def configure_project(bucket, token, autosave=True):
    global PROJECT_SOURCE, PROJECT_TOKEN, PROJECT_AUTOSAVE
    if not bucket:
        raise ValueError("Project bucket is required")
    if not token:
        raise ValueError("HF token is required for project sync")
    PROJECT_SOURCE, _ = ensure_project_bucket(bucket, token, allow_override=False)
    PROJECT_TOKEN = token
    PROJECT_AUTOSAVE = bool(autosave)
    return {"configured": True, "bucket": PROJECT_SOURCE, "autosave": PROJECT_AUTOSAVE}


def list_project_buckets(token=None, namespace=None):
    from huggingface_hub import get_bucket_file_metadata, list_buckets
    from huggingface_hub.errors import EntryNotFoundError

    projects = []
    for bucket in list_buckets(namespace=namespace or None, token=token or None):
        try:
            get_bucket_file_metadata(bucket.id, "project.json", token=token or None)
            is_project = True
        except EntryNotFoundError:
            is_project = False
        projects.append({
            "id": bucket.id,
            "private": bool(getattr(bucket, "private", False)),
            "size": int(getattr(bucket, "size", 0) or 0),
            "total_files": int(getattr(bucket, "total_files", 0) or 0),
            "is_project": is_project,
        })
    return projects


def open_project(bucket, token):
    global PROJECT_SOURCE, PROJECT_TOKEN, PROJECT_ID, PROJECT_AUTOSAVE, DATASET_DIR, USE_LOCAL_DATASET, DATASET_SOURCE, _dataset
    if not token:
        raise ValueError("An HF token is required to open a project")
    source, remote_project = ensure_project_bucket(bucket, token, allow_override=False)
    if not remote_project:
        raise ValueError(f"Bucket {project_bucket_id(bucket)} is not a project bucket")
    sync_hf_directory(source, PROJECT_DIR, token)
    manifest = read_project_manifest()
    if not manifest or not validate_dataset_directory(PROJECT_DIR / "dataset"):
        raise RuntimeError("Project bucket is missing project.json or dataset/")
    PROJECT_SOURCE = source
    PROJECT_TOKEN = token
    metadata = manifest.get("project", {})
    PROJECT_ID = metadata.get("id")
    PROJECT_AUTOSAVE = bool(metadata.get("autosave", True))
    DATASET_DIR = PROJECT_DIR / "dataset"
    DATASET_SOURCE = str(local_dataset_annotation_path())
    USE_LOCAL_DATASET = True
    _dataset = None
    return {"opened": True, "source": source, "project": manifest}


def reset_project():
    global PROJECT_SOURCE, PROJECT_TOKEN, PROJECT_AUTOSAVE, PROJECT_ID
    PROJECT_SOURCE = ""
    PROJECT_TOKEN = None
    PROJECT_AUTOSAVE = True
    PROJECT_ID = None
    return {"reset": True}


def update_project_save_job(job_id, **values):
    with _jobs_lock:
        _project_save_jobs[job_id].update(values)


def run_project_save_job(job_id, document, autosave):
    try:
        with _project_save_lock:
            update_project_save_job(job_id, status="running", started=time.monotonic())
            result = save_project_document(document, autosave)
        update_project_save_job(
            job_id,
            status="completed",
            result=result,
            elapsed_seconds=round(time.monotonic() - _project_save_jobs[job_id]["started"], 3),
        )
    except Exception as error:
        update_project_save_job(job_id, status="error", error=str(error))


def start_project_save_job(document, autosave=False):
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _project_save_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "autosave": bool(autosave),
            "queued_at": time.time(),
        }
    threading.Thread(target=run_project_save_job, args=(job_id, document, autosave), daemon=True).start()
    return job_id


def get_project_save_job(job_id):
    with _jobs_lock:
        job = _project_save_jobs.get(job_id)
        return dict(job) if job else None


def prepare_dataset(dataset_dir, remote_coco_url=None, remote_image_url=None, hf_source=None, hf_token=None, no_prompt=False):
    global DATASET_DIR, USE_LOCAL_DATASET, COCO_URL, IMAGE_URL, DATASET_SOURCE
    DATASET_DIR = Path(dataset_dir).expanduser().resolve()
    if remote_coco_url or remote_image_url:
        USE_LOCAL_DATASET = False
        COCO_URL = remote_coco_url or os.environ.get("COCO_URL") or COCO_URL
        IMAGE_URL = remote_image_url or os.environ.get("IMAGE_URL") or IMAGE_URL
        DATASET_SOURCE = COCO_URL
        return
    if validate_local_dataset():
        USE_LOCAL_DATASET = True
        DATASET_SOURCE = str(local_dataset_annotation_path())
        return
    source = hf_source or os.environ.get("HF_DATASET_SOURCE") or DEFAULT_HF_SOURCE
    token = hf_token or os.environ.get("HF_TOKEN")
    if not no_prompt:
        if not sys.stdin.isatty():
            raise RuntimeError("No valid dataset found and stdin is not interactive; use --no-prompt with HF_DATASET_SOURCE/HF_TOKEN")
        source = input(f"Hugging Face bucket path [{source}]: ").strip() or source
        if token is None:
            token = getpass.getpass("HF token (optional): ").strip() or None
    print(f"Dataset not found in {DATASET_DIR}; syncing {source}...", flush=True)
    DATASET_SOURCE = sync_hf_dataset(source, token)
    USE_LOCAL_DATASET = True


def load_dataset():
    global _dataset
    with _lock:
        if _dataset is not None:
            return _dataset
        if USE_LOCAL_DATASET:
            with local_dataset_annotation_path().open(encoding="utf-8") as file:
                data = json.load(file)
        else:
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


HU_MOMENT_LABELS = {f"hu_moments_{index}": f"φ{index}" for index in range(1, 8)}
FOURIER_DESCRIPTOR_LABELS = {f"fourier_descriptors_{index}": f"FD{index}" for index in range(1, 17)}

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


def _mask_component_intervals(segmentation):
    if not isinstance(segmentation, dict):
        return 0, 0, []
    counts = segmentation.get("counts", [])
    if isinstance(counts, str):
        counts = decode_compressed_counts(counts)
    height, width = segmentation.get("size", [0, 0])
    if not counts or not height or not width:
        return 0, 0, []
    columns = {}
    position = 0
    for run_index, count in enumerate(counts):
        if run_index % 2 == 0:
            position += count
            continue
        remaining = count
        while remaining > 0 and position < height * width:
            column, row = divmod(position, height)
            length = min(remaining, height - row)
            columns.setdefault(column, []).append((row, row + length))
            position += length
            remaining -= length
        position += remaining
    parent = []
    component_intervals = []

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    previous_column = None
    previous = []
    for column in sorted(columns):
        current = []
        for start, end in columns[column]:
            node = len(parent)
            parent.append(node)
            if previous_column == column - 1:
                for previous_start, previous_end, previous_node in previous:
                    if start < previous_end and end > previous_start:
                        union(node, previous_node)
            current.append((start, end, node))
            component_intervals.append((start, end, column, node))
        previous_column, previous = column, current
    return height, width, [(start, end, column, find(node)) for start, end, column, node in component_intervals]


def mask_component_masks(segmentation):
    height, width, component_intervals = _mask_component_intervals(segmentation)
    if not component_intervals:
        return []
    roots = []
    for start, end, column, root in component_intervals:
        if root not in roots:
            roots.append(root)
    masks = []
    for root in roots:
        flat = np.zeros(height * width, dtype=bool)
        for start, end, column, component_root in component_intervals:
            if component_root == root:
                flat[column * height + start : column * height + end] = True
        masks.append(flat.reshape((height, width), order="F"))
    return masks


def mask_component_areas(segmentation):
    _height, _width, component_intervals = _mask_component_intervals(segmentation)
    if not component_intervals:
        return []
    areas = {}
    for start, end, column, root in component_intervals:
        areas[root] = areas.get(root, 0) + end - start
    return list(areas.values())


def encode_mask(mask):
    flat = np.asarray(mask, dtype=bool).reshape(-1, order="F")
    encoded = []
    current = False
    run = 0
    for value in flat:
        value = bool(value)
        if value == current:
            run += 1
        else:
            encoded.append(run)
            current = not current
            run = 1
    encoded.append(run)
    return encoded


def encode_component_intervals(height, width, component_intervals, kept_roots):
    columns = {}
    for start, end, column, root in component_intervals:
        if root in kept_roots:
            columns.setdefault(column, []).append((start, end))
    counts = []
    current = False
    position = 0

    def add_run(length, foreground):
        nonlocal current
        if length <= 0:
            return
        if not counts:
            if foreground and not current:
                counts.append(0)
            counts.append(length)
            current = foreground
        elif current == foreground:
            counts[-1] += length
        else:
            counts.append(length)
            current = foreground

    for column in sorted(columns):
        column_start = column * height
        for start, end in sorted(columns[column]):
            add_run(column_start + start - position, False)
            add_run(end - start, True)
            position = column_start + end
    add_run(height * width - position, False)
    return counts


def apply_mask_drops(segmentation, drop_indices):
    if not isinstance(segmentation, dict) or not drop_indices:
        return segmentation
    height, width, component_intervals = _mask_component_intervals(segmentation)
    roots = []
    for start, end, column, root in component_intervals:
        if root not in roots:
            roots.append(root)
    drops = {int(index) for index in drop_indices}
    if not drops or min(drops) < 0 or max(drops) >= len(roots):
        raise ValueError("Invalid island drop index")
    if len(drops) >= len(roots):
        return None
    kept_roots = {root for index, root in enumerate(roots) if index not in drops}
    return {"size": segmentation["size"], "counts": encode_component_intervals(height, width, component_intervals, kept_roots)}


def mask_geometry(segmentation):
    mask = mask_array(segmentation)
    if mask is None or not mask.any():
        return None
    rows, columns = np.where(mask)
    x0, x1 = int(columns.min()), int(columns.max()) + 1
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    return [x0, y0, x1 - x0, y1 - y0, int(mask.sum())]


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
        if values.get("status") == "running" and _jobs[job_id].get("status") == "cancelling":
            return
        _jobs[job_id].update(values)


def available_cpu_count():
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def working_project_clean_indices(data):
    collaboration = read_collaboration()
    cleans = collaboration.get("island_cleans", {})
    if not isinstance(cleans, dict):
        return {}
    applied = {}
    for index, annotation in enumerate(data["annotations"]):
        clean = cleans.get(str(annotation.get("id")))
        if not isinstance(clean, dict) or not clean.get("applied"):
            continue
        drop_indices = clean.get("drop_indices", [])
        if isinstance(drop_indices, list) and drop_indices:
            applied[index] = drop_indices
    return applied


def count_island_range(bounds, clean_indices=None):
    start, end = bounds
    data = _dataset or load_dataset()
    clean_indices = clean_indices or {}
    overall = Counter()
    categories = {}
    instances = {}
    for index in range(start, end):
        annotation = data["annotations"][index]
        segmentation = annotation.get("segmentation")
        if index in clean_indices:
            segmentation = apply_mask_drops(segmentation, clean_indices[index])
            if segmentation is None:
                continue
        islands = count_mask_islands(segmentation)
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
        applied_clean_indices = working_project_clean_indices(data)
        total = len(annotations)
        worker_count = max(1, min(available_cpu_count(), workers or available_cpu_count(), total or 1))
        chunk_target = worker_count * 4
        chunk_size = max(1, (total + chunk_target - 1) // chunk_target)
        bounds = [(start, min(start + chunk_size, total)) for start in range(0, total, chunk_size)]
        cancel_event = _job_cancel_events.setdefault(job_id, threading.Event())
        update_job(job_id, status="running", total=total, processed=0, progress=0, workers=worker_count, available_cpus=available_cpu_count())
        overall = Counter()
        category_counters = {}
        instance_groups = {}
        processed = 0
        pool = get_context("fork").Pool(processes=worker_count)
        _active_pools[job_id] = pool
        try:
            pending_jobs = [
                pool.apply_async(
                    count_island_range,
                    (bound, {index: applied_clean_indices[index] for index in range(bound[0], bound[1]) if index in applied_clean_indices}),
                )
                for bound in bounds
            ]
            while pending_jobs:
                for pending in list(pending_jobs):
                    if not pending.ready():
                        continue
                    result = pending.get()
                    pending_jobs.remove(pending)
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
                if pending_jobs and cancel_event.wait(0.05):
                    update_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
                    return
        finally:
            pool.terminate()
            pool.join()
            _active_pools.pop(job_id, None)
        if cancel_event.is_set():
            update_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
            return
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
        result = {
            "summary": build_island_summary(overall_values, time.monotonic() - started),
            "categories": categories,
            "inspection_groups": [
                {"islands": island_count, "instances": sorted(instances, key=lambda item: item["annotation_id"])}
                for island_count, instances in sorted(instance_groups.items())
            ],
            "workers": worker_count,
            "available_cpus": available_cpu_count(),
        }
        cache_analysis_result("island-frequency", {}, result)
        update_job(job_id, status="completed", processed=total, progress=100, result=result)
    except Exception as error:
        if _job_cancel_events.get(job_id) and _job_cancel_events[job_id].is_set():
            update_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
        else:
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
        _job_cancel_events[job_id] = threading.Event()
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
            "label": HU_MOMENT_LABELS.get(key, FOURIER_DESCRIPTOR_LABELS.get(key, SHAPE_DESCRIPTORS.get(base, base))),
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


def run_shape_descriptors(job_id, descriptors, class_ids, sample_count, workers):
    global _shape_references
    started = time.monotonic()
    try:
        data = load_dataset()
        selected_indices_by_class = {}
        for index, annotation in enumerate(data["annotations"]):
            category_id = str(annotation.get("category_id"))
            if not class_ids or category_id in class_ids:
                selected_indices_by_class.setdefault(category_id, []).append(index)
        randomizer = random.Random()
        selected_indices = []
        for category_indices in selected_indices_by_class.values():
            if sample_count and len(category_indices) > sample_count:
                selected_indices.extend(randomizer.sample(category_indices, sample_count))
            else:
                selected_indices.extend(category_indices)
        selected = [data["annotations"][index] for index in selected_indices]
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
        chunk_target = worker_count * 16
        chunk_size = max(1, (total + chunk_target - 1) // chunk_target)
        bounds = [selected_indices[start : start + chunk_size] for start in range(0, total, chunk_size)]
        cancel_event = _job_cancel_events.setdefault(job_id, threading.Event())
        update_shape_job(job_id, status="running", total=total, processed=0, progress=0, workers=worker_count, available_cpus=available_cpu_count())
        merged = {}
        skipped = 0
        processed = 0
        pool = get_context("fork").Pool(processes=worker_count)
        _active_pools[job_id] = pool
        try:
            pending_jobs = [pool.apply_async(shape_range, ((bound, descriptors, class_ids),)) for bound in bounds]
            while pending_jobs:
                for pending in list(pending_jobs):
                    if not pending.ready():
                        continue
                    result = pending.get()
                    pending_jobs.remove(pending)
                    processed += result["processed"]
                    skipped += result["skipped"]
                    for category_id, values in result["class_values"].items():
                        target = merged.setdefault(category_id, {})
                        for descriptor, descriptor_values in values.items():
                            target.setdefault(descriptor, []).extend(descriptor_values)
                    update_shape_job(job_id, processed=processed, progress=round(processed * 100 / total, 3) if total else 100, elapsed_seconds=round(time.monotonic() - started, 3))
                if pending_jobs and cancel_event.wait(0.05):
                    update_shape_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
                    return
        finally:
            pool.terminate()
            pool.join()
            _active_pools.pop(job_id, None)
        if cancel_event.is_set():
            update_shape_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
            return
        categories = [
            {"id": category["id"], "name": category["name"], "summary": build_shape_summary({category["id"]: merged.get(str(category["id"]), {})}, time.monotonic() - started)}
            for category in data["categories"]
            if not class_ids or str(category["id"]) in class_ids
        ]
        result = {"descriptors": descriptors, "class_ids": class_ids, "sample_count": sample_count, "categories": categories, "skipped": skipped, "processed": total, "workers": worker_count}
        cache_analysis_result("shape-descriptors", {"descriptors": descriptors, "class_ids": class_ids, "sample_count": sample_count}, result)
        update_shape_job(job_id, status="completed", processed=total, progress=100, result=result)
    except Exception:
        if _job_cancel_events.get(job_id) and _job_cancel_events[job_id].is_set():
            update_shape_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
        else:
            update_shape_job(job_id, status="error", error=traceback.format_exc(), elapsed_seconds=round(time.monotonic() - started, 3))


def update_shape_job(job_id, **values):
    with _jobs_lock:
        if values.get("status") == "running" and _shape_jobs[job_id].get("status") == "cancelling":
            return
        _shape_jobs[job_id].update(values)


def start_shape_descriptor_job(payload):
    descriptors = payload.get("descriptors", [])
    class_ids = [str(value) for value in payload.get("class_ids", [])]
    sample_count = max(0, int(payload.get("sample_count", 0)))
    invalid = [descriptor for descriptor in descriptors if descriptor not in SHAPE_DESCRIPTORS]
    if not descriptors or invalid:
        raise ValueError("Select at least one valid descriptor")
    with _jobs_lock:
        for job in _shape_jobs.values():
            if job["status"] in {"queued", "running"}:
                return job["id"]
        job_id = uuid.uuid4().hex
        _job_cancel_events[job_id] = threading.Event()
        _shape_jobs[job_id] = {"id": job_id, "status": "queued", "processed": 0, "total": 0, "progress": 0, "workers": 0, "created_at": time.time(), "descriptors": descriptors, "class_ids": class_ids, "sample_count": sample_count}
    threading.Thread(target=run_shape_descriptors, args=(job_id, descriptors, class_ids, sample_count, WORKERS), daemon=True).start()
    return job_id


def cancel_analysis_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id) or _shape_jobs.get(job_id) or _cleanup_jobs.get(job_id) or _island_cleanup_jobs.get(job_id)
        if job is None or job["status"] not in {"queued", "running", "cancelling"}:
            return False
        job["status"] = "cancelling"
        event = _job_cancel_events.get(job_id)
        if event:
            event.set()
        return True


def cleanup_range(arguments):
    indices, filters = arguments
    data = _dataset or load_dataset()
    candidates = []
    for index in indices:
        annotation = data["annotations"][index]
        areas = mask_component_areas(annotation.get("segmentation"))
        island_count = len(areas)
        if island_count < 2 or not filters["island_range"]:
            if not (filters["island_range"] and filters["min_islands"] <= island_count <= filters["max_islands"]):
                continue
        if filters["island_range"] and not filters["min_islands"] <= island_count <= filters["max_islands"]:
            continue
        largest = max(areas)
        if filters["area_ratio"]:
            drop_indices = [offset for offset, area in enumerate(areas) if area / largest <= filters["max_area_ratio"]]
            if not drop_indices:
                continue
        else:
            drop_indices = []
        candidates.append({"annotation_id": annotation["id"], "image_id": annotation["image_id"], "category_id": annotation.get("category_id"), "island_count": island_count, "component_areas": areas, "drop_indices": drop_indices})
    return candidates, len(indices)


def island_cleanup_parameters(payload):
    try:
        parameters = {
            "min_islands": int(payload.get("min_islands", 2)),
            "min_largest_other_ratio": float(payload.get("min_largest_other_ratio", 3.0)),
            "evidence_enabled": bool(payload.get("evidence_enabled", False)),
            "evidence_sample_count": max(0, int(payload.get("evidence_sample_count", 0))),
            "evidence_tolerance": float(payload.get("evidence_tolerance", 0.25)),
        }
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid Island Cleanup parameters") from error
    if parameters["min_islands"] < 2:
        raise ValueError("Minimum island count must be at least 2")
    if not math.isfinite(parameters["min_largest_other_ratio"]) or parameters["min_largest_other_ratio"] <= 0:
        raise ValueError("Minimum largest-to-other area ratio must be greater than 0")
    if not math.isfinite(parameters["evidence_tolerance"]) or not 0 <= parameters["evidence_tolerance"] <= 1:
        raise ValueError("Evidence tolerance must be between 0 and 1")
    if parameters["evidence_enabled"] and parameters["evidence_sample_count"] < 1:
        raise ValueError("Evidence sample count must be at least 1 when enabled")
    return parameters


def island_cleanup_evidence(data):
    evidence = {}
    for annotation in data["annotations"]:
        areas = mask_component_areas(annotation.get("segmentation"))
        if len(areas) != 1:
            continue
        key = (annotation.get("image_id"), annotation.get("category_id"))
        evidence.setdefault(key, []).append({"annotation_id": annotation["id"], "area": areas[0]})
    for values in evidence.values():
        values.sort(key=lambda item: item["annotation_id"])
    return evidence


def cleanup_candidate(annotation, areas, evidence, parameters):
    island_count = len(areas)
    if island_count < parameters["min_islands"]:
        return None
    largest_index = max(range(island_count), key=lambda index: (areas[index], -index))
    largest = areas[largest_index]
    other_area = sum(areas) - largest
    ratio = largest / other_area if other_area else math.inf
    if ratio < parameters["min_largest_other_ratio"]:
        return None
    evidence_ids = []
    if parameters["evidence_enabled"]:
        candidates = evidence.get((annotation.get("image_id"), annotation.get("category_id")), [])
        matched = [
            item
            for item in candidates
            if 1 - parameters["evidence_tolerance"] <= item["area"] / largest <= 1 + parameters["evidence_tolerance"]
        ]
        if len(matched) < parameters["evidence_sample_count"]:
            return None
        evidence_ids = [item["annotation_id"] for item in random.sample(matched, parameters["evidence_sample_count"])]
    return {
        "annotation_id": annotation["id"],
        "image_id": annotation["image_id"],
        "category_id": annotation.get("category_id"),
        "island_count": island_count,
        "component_areas": areas,
        "largest_index": largest_index,
        "largest_area": largest,
        "other_area": other_area,
        "largest_other_ratio": ratio,
        "drop_indices": [index for index in range(island_count) if index != largest_index],
        "evidence_annotation_ids": evidence_ids,
    }


def island_cleanup_range(arguments):
    start, end, parameters, evidence = arguments
    data = _dataset or load_dataset()
    candidates = []
    for index in range(start, end):
        annotation = data["annotations"][index]
        areas = mask_component_areas(annotation.get("segmentation"))
        candidate = cleanup_candidate(annotation, areas, evidence, parameters)
        if candidate:
            candidates.append(candidate)
    return candidates, end - start


def update_island_cleanup_job(job_id, **values):
    with _jobs_lock:
        _island_cleanup_jobs[job_id].update(values)


def run_island_cleanup_job(job_id, parameters):
    started = time.monotonic()
    try:
        data = load_dataset()
        total = len(data["annotations"])
        evidence = island_cleanup_evidence(data) if parameters["evidence_enabled"] else {}
        cancel_event = _job_cancel_events.setdefault(job_id, threading.Event())
        update_island_cleanup_job(job_id, status="running", total=total, processed=0, progress=0, workers=1, available_cpus=available_cpu_count())
        candidates = []
        processed = 0
        for start in range(0, total, 250):
            if cancel_event.is_set():
                update_island_cleanup_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
                return
            result, count = island_cleanup_range((start, min(start + 250, total), parameters, evidence))
            candidates.extend(result)
            processed += count
            update_island_cleanup_job(job_id, processed=processed, progress=round(processed * 100 / total, 3) if total else 100, elapsed_seconds=round(time.monotonic() - started, 3))
        result = {
            "parameters": parameters,
            "candidates": sorted(candidates, key=lambda item: item["annotation_id"]),
            "processed": total,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        cache_analysis_result("island-cleanup", parameters, result)
        update_island_cleanup_job(job_id, status="completed", processed=total, progress=100, result=result)
    except Exception as error:
        if _job_cancel_events.get(job_id) and _job_cancel_events[job_id].is_set():
            update_island_cleanup_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
        else:
            update_island_cleanup_job(job_id, status="error", error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))


def start_island_cleanup_job(payload):
    parameters = island_cleanup_parameters(payload)
    with _jobs_lock:
        for job in _island_cleanup_jobs.values():
            if job["status"] in {"queued", "running"}:
                return job["id"]
        job_id = uuid.uuid4().hex
        _job_cancel_events[job_id] = threading.Event()
        _island_cleanup_jobs[job_id] = {"id": job_id, "status": "queued", "processed": 0, "total": 0, "progress": 0, "created_at": time.time(), "parameters": parameters}
    threading.Thread(target=run_island_cleanup_job, args=(job_id, parameters), daemon=True).start()
    return job_id


def confirm_island_cleans(user, payload):
    parameters = island_cleanup_parameters(payload.get("parameters", {}))
    entries = payload.get("entries", payload.get("candidates", []))
    if not isinstance(entries, list):
        raise ValueError("Island Cleanup entries must be a list")
    if not entries:
        raise ValueError("Island Cleanup confirmation requires at least one entry")
    data = load_dataset()
    annotations = {annotation["id"]: annotation for annotation in data["annotations"]}
    evidence = island_cleanup_evidence(data) if parameters["evidence_enabled"] else {}
    validated = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid Island Cleanup entry")
        annotation = annotations.get(entry.get("annotation_id"))
        if annotation is None:
            raise ValueError(f"Unknown annotation ID: {entry.get('annotation_id')}")
        areas = mask_component_areas(annotation.get("segmentation"))
        raw_indices = entry.get("drop_indices", [])
        if not raw_indices or not isinstance(raw_indices, list):
            raise ValueError("Each Island Cleanup entry requires drop indices")
        try:
            drop_indices = sorted({int(index) for index in raw_indices})
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid Island Cleanup drop index") from error
        if len(drop_indices) != len(raw_indices) or any(index < 0 or index >= len(areas) for index in drop_indices) or len(drop_indices) >= len(areas):
            raise ValueError("Invalid Island Cleanup drop indices")
        candidate = cleanup_candidate(annotation, areas, evidence, parameters)
        if candidate is None or set(drop_indices) != set(candidate["drop_indices"]):
            raise ValueError(f"Island Cleanup criteria no longer match annotation {annotation['id']}")
        validated[str(annotation["id"])] = {
            "annotation_id": annotation["id"],
            "drop_indices": drop_indices,
            "component_areas": areas,
            "parameters": parameters,
            "confirmed_at": time.time(),
            "confirmed_by": user["user_id"],
        }
    collaboration = read_collaboration()
    collaboration.setdefault("island_cleans", {}).update(validated)
    collaboration["revision"] = int(collaboration.get("revision", 0)) + 1
    write_collaboration(collaboration)
    return {"confirmed": len(validated), "island_cleans": collaboration["island_cleans"], "revision": collaboration["revision"]}


def apply_island_cleans(user, payload):
    scope = payload.get("scope", "current")
    if scope not in {"current", "all"}:
        raise ValueError("Island Cleanup apply scope must be current or all")
    try:
        image_id = int(payload["image_id"]) if payload.get("image_id") is not None else None
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid Island Cleanup image ID") from error
    if image_id is None:
        raise ValueError("Island Cleanup apply requires an image ID")
    data = load_dataset()
    annotations = {annotation["id"]: annotation for annotation in data["annotations"]}
    collaboration = read_collaboration()
    cleans = collaboration.get("island_cleans", {})
    if not isinstance(cleans, dict) or not cleans:
        raise ValueError("No confirmed Island Cleanup entries to apply")
    now = time.time()
    applied = 0
    cleaned_annotations = []
    for key, clean in cleans.items():
        if not isinstance(clean, dict):
            continue
        annotation = annotations.get(clean.get("annotation_id"))
        if annotation is None:
            continue
        if scope == "all" and annotation.get("image_id") != image_id:
            clean["applied"] = True
            clean["applied_at"] = now
            clean["applied_by"] = user["user_id"]
            clean["apply_scope"] = scope
            applied += 1
            continue
        if scope == "current" and annotation.get("image_id") != image_id:
            continue
        segmentation = apply_mask_drops(annotation.get("segmentation"), clean.get("drop_indices", []))
        if segmentation is None:
            raise ValueError(f"Island Cleanup would remove annotation {annotation['id']} completely")
        geometry = mask_geometry(segmentation)
        clean["applied"] = True
        clean["applied_at"] = now
        clean["applied_by"] = user["user_id"]
        clean["apply_scope"] = scope
        applied += 1
        output = dict(annotation)
        output["segmentation"] = segmentation
        output["bbox"] = geometry[:4]
        output["area"] = geometry[4]
        cleaned_annotations.append(output)
    if not applied:
        raise ValueError("No confirmed Island Cleanup entries match the current image")
    collaboration["revision"] = int(collaboration.get("revision", 0)) + 1
    write_collaboration(collaboration)
    return {"applied": applied, "scope": scope, "annotations": cleaned_annotations, "island_cleans": collaboration["island_cleans"], "revision": collaboration["revision"]}


def update_cleanup_job(job_id, **values):
    with _jobs_lock:
        _cleanup_jobs[job_id].update(values)


def run_excess_island_job(job_id, filters):
    started = time.monotonic()
    try:
        data = load_dataset()
        indices = list(range(len(data["annotations"])))
        total = len(indices)
        cancel_event = _job_cancel_events.setdefault(job_id, threading.Event())
        update_cleanup_job(job_id, status="running", total=total, processed=0, progress=0, workers=available_cpu_count(), available_cpus=available_cpu_count())
        candidates = []
        processed = 0
        pool = get_context("fork").Pool(processes=available_cpu_count())
        try:
            pending_jobs = [pool.apply_async(cleanup_range, ((bound, filters),)) for bound in [indices[start : start + 250] for start in range(0, total, 250)]]
            while pending_jobs:
                for pending in list(pending_jobs):
                    if not pending.ready():
                        continue
                    result, count = pending.get()
                    candidates.extend(result)
                    processed += count
                    pending_jobs.remove(pending)
                    update_cleanup_job(job_id, processed=processed, progress=round(processed * 100 / total, 3) if total else 100, elapsed_seconds=round(time.monotonic() - started, 3))


                if pending_jobs and cancel_event.wait(0.05):
                    update_cleanup_job(job_id, status="cancelled", elapsed_seconds=round(time.monotonic() - started, 3))
                    return
        finally:
            pool.terminate()
            pool.join()
        result = {"filters": filters, "candidates": sorted(candidates, key=lambda item: item["annotation_id"]), "elapsed_seconds": round(time.monotonic() - started, 3), "workers": available_cpu_count()}
        cache_analysis_result("excess-islands", filters, result)
        update_cleanup_job(job_id, status="completed", processed=total, progress=100, result=result)
    except Exception as error:
        update_cleanup_job(job_id, status="error", error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))


def start_excess_island_job(payload):
    filters = {
        "island_range": bool(payload.get("island_range", True)),
        "min_islands": max(2, int(payload.get("min_islands", 2))),
        "max_islands": max(2, int(payload.get("max_islands", 100000))),
        "area_ratio": bool(payload.get("area_ratio", False)),
        "max_area_ratio": float(payload.get("max_area_ratio", 0.25)),
    }
    if not filters["island_range"] and not filters["area_ratio"]:
        raise ValueError("Enable island range or area ratio")
    if filters["min_islands"] > filters["max_islands"]:
        raise ValueError("Minimum island count cannot exceed maximum")
    if not 0 < filters["max_area_ratio"] <= 1:
        raise ValueError("Maximum area ratio must be between 0 and 1")
    with _jobs_lock:
        for job in _cleanup_jobs.values():
            if job["status"] in {"queued", "running"}:
                return job["id"]
        job_id = uuid.uuid4().hex
        _job_cancel_events[job_id] = threading.Event()
        _cleanup_jobs[job_id] = {"id": job_id, "status": "queued", "processed": 0, "total": 0, "progress": 0, "created_at": time.time()}
    threading.Thread(target=run_excess_island_job, args=(job_id, filters), daemon=True).start()
    return job_id


def get_excess_island_job(job_id):
    with _jobs_lock:
        job = _cleanup_jobs.get(job_id)
        return dict(job) if job else None


def get_island_cleanup_job(job_id):
    with _jobs_lock:
        job = _island_cleanup_jobs.get(job_id)
        return dict(job) if job else None


def get_shape_descriptor_job(job_id):
    with _jobs_lock:
        job = _shape_jobs.get(job_id)
        return dict(job) if job else None


def get_island_frequency_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def public_user(user):
    return {"id": user["user_id"], "username": user["username"], "role": user["role"], "csrf_token": user["csrf_token"]}


def request_user(handler):
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    session_cookie = cookie.get("session_id")
    return AUTH_STORE.get_session(session_cookie.value if session_cookie else "")


def require_user(handler, manager=False):
    user = request_user(handler)
    if not user:
        handler.send_error(HTTPStatus.UNAUTHORIZED, "Login required")
        return None
    if manager and user["role"] != "manager":
        handler.send_error(HTTPStatus.FORBIDDEN, "Manager access required")
        return None
    handler.current_user = user
    return user


def require_csrf(handler, user):
    if handler.headers.get("X-CSRF-Token") != user["csrf_token"]:
        handler.send_error(HTTPStatus.FORBIDDEN, "Invalid CSRF token")
        return False
    return True


def collaboration_path():
    return PROJECT_DIR / "collaboration.json"


def analysis_cache_path():
    return PROJECT_DIR / "analysis-cache.json"


def analysis_cache_key(tool, parameters=None):
    normalized = json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"))
    return f"{tool}:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def read_analysis_cache():
    try:
        with analysis_cache_path().open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def get_cached_analysis(tool, parameters=None, latest=False):
    cache = read_analysis_cache()
    if latest:
        for key, entry in reversed(list(cache.items())):
            if key.startswith(f"{tool}:"):
                return entry.get("result")
        return None
    entry = cache.get(analysis_cache_key(tool, parameters))
    return entry.get("result") if entry else None


def cache_analysis_result(tool, parameters, result):
    analysis_cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache = read_analysis_cache()
    cache[analysis_cache_key(tool, parameters)] = {"parameters": parameters, "result": result, "cached_at": time.time()}
    temporary = analysis_cache_path().with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
    temporary.replace(analysis_cache_path())
    return result


def read_collaboration():
    data = None
    try:
        with collaboration_path().open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("frames"), dict):
        data = {"revision": 1, "frames": {}, "workspaces": {}, "accounts": {}, "island_cleans": {}}
    data.setdefault("workspaces", {})
    data.setdefault("accounts", {})
    data.setdefault("island_cleans", {})
    for image in load_dataset()["images"]:
        data["frames"].setdefault(str(image["id"]), {"state": "waiting", "assigned_to": None, "updated_at": time.time()})
    return data


def write_collaboration(data):
    collaboration_path().parent.mkdir(parents=True, exist_ok=True)
    with _collaboration_lock:
        temporary = collaboration_path().with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        temporary.replace(collaboration_path())
    return data


def can_access_image(user, image_id):
    if user["role"] == "manager":
        return True
    frame = read_collaboration()["frames"].get(str(image_id), {})
    return frame.get("assigned_to") == user["user_id"]


def frame_records(user):
    data = load_dataset()
    collaboration = read_collaboration()
    records = []
    for image in data["summary"]:
        frame = collaboration["frames"].get(str(image["id"]), {"state": "waiting", "assigned_to": None})
        if user["role"] == "annotator" and frame.get("assigned_to") != user["user_id"]:
            continue
        assigned = AUTH_STORE.get_user(frame["assigned_to"]) if frame.get("assigned_to") else None
        records.append({**image, "frame_state": frame.get("state", "waiting"), "assigned_to": frame.get("assigned_to"), "assigned_name": assigned["username"] if assigned else None})
    return records


def account_payload(user):
    return {key: user[key] for key in ("id", "username", "role", "active", "created_at", "updated_at")}


def sync_project_accounts():
    data = read_collaboration()
    data["accounts"] = {user["id"]: account_payload(user) for user in AUTH_STORE.list_users()}
    return write_collaboration(data)


def create_account(payload):
    role = payload.get("role", "annotator")
    user = AUTH_STORE.create_user(payload.get("username", ""), payload.get("password", ""), role, bool(payload.get("active", True)))
    sync_project_accounts()
    return account_payload(user)


def update_account(payload):
    user = AUTH_STORE.get_user(payload.get("user_id", ""))
    if not user:
        raise ValueError("Account not found")
    if "active" in payload:
        user = AUTH_STORE.set_active(user["id"], bool(payload["active"]))
        if not user["active"]:
            AUTH_STORE.delete_user_sessions(user["id"])
    if payload.get("password"):
        user = AUTH_STORE.set_password(user["id"], payload["password"])
        AUTH_STORE.delete_user_sessions(user["id"])
    sync_project_accounts()
    return account_payload(user)


def assign_frames(image_ids, annotator_id):
    annotator = AUTH_STORE.get_user(annotator_id)
    if not annotator or not annotator["active"] or annotator["role"] != "annotator":
        raise ValueError("Active annotator not found")
    data = read_collaboration()
    valid_ids = {str(image["id"]) for image in load_dataset()["images"]}
    for image_id in image_ids:
        if str(image_id) not in valid_ids:
            raise ValueError(f"Unknown image ID: {image_id}")
        data["frames"][str(image_id)].update({"assigned_to": annotator_id, "state": "waiting", "updated_at": time.time(), "updated_by": None})
    data["revision"] = int(data.get("revision", 0)) + 1
    return write_collaboration(data)


def set_frame_state(user, image_id, state):
    if state not in {"waiting", "annotated", "reviewed"}:
        raise ValueError("Invalid frame state")
    data = read_collaboration()
    frame = data["frames"].get(str(image_id))
    if not frame:
        raise ValueError("Unknown image ID")
    if user["role"] == "annotator":
        if frame.get("assigned_to") != user["user_id"] or state != "annotated":
            raise PermissionError("Annotators can only mark their assigned frame annotated")
    elif state == "reviewed" and frame.get("state") != "annotated":
        raise ValueError("Only annotated frames can be reviewed")
    frame.update({"state": state, "updated_at": time.time(), "updated_by": user["user_id"]})
    data["revision"] = int(data.get("revision", 0)) + 1
    return write_collaboration(data)


def save_frame_workspace(user, image_id, workspace):
    if not can_access_image(user, image_id):
        raise PermissionError("Frame is not assigned to this annotator")
    data = read_collaboration()
    key = str(image_id)
    if key not in data["frames"]:
        raise ValueError("Unknown image ID")
    data["workspaces"][key] = {
        "workspace": workspace,
        "updated_at": time.time(),
        "updated_by": user["user_id"],
    }
    data["revision"] = int(data.get("revision", 0)) + 1
    return write_collaboration(data)


def frame_workspace(user, image_id):
    if not can_access_image(user, image_id):
        raise PermissionError("Frame is not assigned to this annotator")
    return read_collaboration()["workspaces"].get(str(image_id), {"workspace": {}})


def ensure_bootstrap_manager():
    if AUTH_STORE.has_users():
        sync_project_accounts()
        return
    username = os.environ.get("MANAGER_USERNAME", "manager")
    password = os.environ.get("MANAGER_PASSWORD")
    generated = False
    if not password and sys.stdin.isatty():
        password = getpass.getpass(f"Create manager account '{username}' password: ")
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    user = AUTH_STORE.create_user(username, password, "manager")
    sync_project_accounts()
    if generated:
        print(f"Bootstrap manager: {user['username']} / {password}", flush=True)
    else:
        print(f"Created manager account: {user['username']}", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 100_000:
                    raise ValueError("Login request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                user = AUTH_STORE.authenticate(payload.get("username", ""), payload.get("password", ""))
                if not user:
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Invalid username or password")
                    return
                token, csrf_token = AUTH_STORE.create_session(user["id"])
                body = json.dumps({"user": {"id": user["id"], "username": user["username"], "role": user["role"]}, "csrf_token": csrf_token}, separators=(",", ":")).encode("utf-8")
                self.send_bytes(body, "application/json; charset=utf-8", {"Set-Cookie": f"session_id={token}; HttpOnly; SameSite=Lax; Path=/"})
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/auth/logout":
            user = request_user(self)
            if user:
                AUTH_STORE.delete_session(user["token_hash"])
            self.send_bytes(b'{"logged_out":true}', "application/json; charset=utf-8", {"Set-Cookie": "session_id=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/"})
            return
        manager_paths = {"/api/project/configure", "/api/project/save", "/api/project/reset", "/api/projects/list", "/api/project/open", "/api/accounts", "/api/accounts/update", "/api/project/assign", "/api/tools/island-frequency/start", "/api/tools/excess-islands/start", "/api/tools/shape-descriptors/start", "/api/tools/island-cleanup/start", "/api/tools/island-cleanup/confirm", "/api/tools/island-cleanup/apply", "/api/tools/island-frequency/cancel", "/api/tools/excess-islands/cancel", "/api/tools/shape-descriptors/cancel", "/api/tools/island-cleanup/cancel", "/api/export"}
        if path.startswith("/api/"):
            user = require_user(self, manager=path in manager_paths)
            if not user:
                return
            if not require_csrf(self, user):
                return
        if path in {"/api/frame-workspace"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 20_000_000:
                    raise ValueError("Frame workspace request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = save_frame_workspace(request_user(self), payload.get("image_id"), payload.get("workspace", {}))
                self.send_json({"saved": True, "revision": result.get("revision", 0)})
            except PermissionError as error:
                self.send_error(HTTPStatus.FORBIDDEN, str(error))
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path in {"/api/accounts", "/api/accounts/update", "/api/project/assign", "/api/frame-state"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1_000_000:
                    raise ValueError("Account request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/accounts":
                    result = create_account(payload) if self.command == "POST" else {"users": [account_payload(user) for user in AUTH_STORE.list_users()]}
                elif path == "/api/accounts/update":
                    result = update_account(payload)
                elif path == "/api/project/assign":
                    assign_frames(payload.get("image_ids", []), payload.get("user_id"))
                    result = {"assigned": True, "frames": frame_records(request_user(self))}
                else:
                    result = set_frame_state(request_user(self), payload.get("image_id"), payload.get("state"))
                self.send_json(result)
            except PermissionError as error:
                self.send_error(HTTPStatus.FORBIDDEN, str(error))
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path in {"/api/project/configure", "/api/project/save", "/api/project/reset", "/api/projects/list", "/api/project/open"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 50_000_000:
                    raise ValueError("Project request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/projects/list":
                    result = {"projects": list_project_buckets(payload.get("token"), payload.get("namespace"))}
                    self.send_json(result)
                elif path == "/api/project/open":
                    result = open_project(payload.get("bucket", ""), payload.get("token"))
                    self.send_json(result)
                elif path == "/api/project/reset":
                    result = reset_project()
                    self.send_json(result)
                elif path == "/api/project/configure":
                    result = configure_project(payload.get("bucket", ""), payload.get("token"), payload.get("autosave", True))
                    self.send_json(result)
                else:
                    job_id = start_project_save_job(payload.get("project", payload), bool(payload.get("autosave", False)))
                    self.send_json({"job_id": job_id})
                return
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/tools/island-frequency/start":
            self.send_json({"job_id": start_island_frequency_job(WORKERS)})
            return
        if path == "/api/tools/excess-islands/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json({"job_id": start_excess_island_job(payload)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/tools/shape-descriptors/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json({"job_id": start_shape_descriptor_job(payload)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/tools/island-cleanup/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json({"job_id": start_island_cleanup_job(payload)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/tools/island-cleanup/confirm":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 20_000_000:
                    raise ValueError("Island Cleanup confirmation is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(confirm_island_cleans(request_user(self), payload))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/tools/island-cleanup/apply":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.send_json(apply_island_cleans(request_user(self), payload))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path in {"/api/tools/island-frequency/cancel", "/api/tools/shape-descriptors/cancel", "/api/tools/excess-islands/cancel", "/api/tools/island-cleanup/cancel"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                cancelled = cancel_analysis_job(payload.get("job_id", ""))
                self.send_json({"cancelled": cancelled})
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
        island_cleans = payload.get("island_cleans") if isinstance(payload.get("island_cleans"), dict) else read_collaboration().get("island_cleans", {})
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
            clean = island_cleans.get(str(annotation["id"]))
            segmentation = annotation.get("segmentation")
            if clean:
                segmentation = apply_mask_drops(segmentation, clean.get("drop_indices", []))
                if segmentation is None:
                    continue
            mask_strokes = mask_edits.get(str(annotation["id"]))
            if mask_strokes:
                segmentation = apply_mask_strokes(segmentation, mask_strokes)
            if isinstance(segmentation, dict):
                geometry = mask_geometry(segmentation)
                if geometry is None:
                    continue
                output["segmentation"] = segmentation
                output["bbox"] = geometry[:4]
                output["area"] = geometry[4]
            elif segmentation is not None:
                output["segmentation"] = segmentation
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
        if path.startswith("/api/") and path != "/api/health":
            if path == "/api/auth/me":
                user = request_user(self)
                if not user:
                    self.send_error(HTTPStatus.UNAUTHORIZED, "Login required")
                    return
                self.send_json({"user": {"id": user["user_id"], "username": user["username"], "role": user["role"]}, "csrf_token": user["csrf_token"]}, send_body)
                return
            if path == "/api/accounts":
                user = require_user(self, manager=True)
                if not user:
                    return
                self.send_json({"users": [account_payload(item) for item in AUTH_STORE.list_users()]}, send_body)
                return
            manager_paths = {"/api/project", "/api/project/config", "/api/project/collaboration", "/api/project/save/status", "/api/tools/excess-islands/status", "/api/tools/shape-descriptors/status", "/api/tools/island-frequency/status", "/api/tools/island-cleanup/status", "/api/tools/excess-islands/result", "/api/tools/shape-descriptors/result", "/api/tools/island-frequency/result", "/api/tools/island-cleanup/result"}
            user = require_user(self, manager=path in manager_paths)
            if not user:
                return
            if path in {"/api/tools/excess-islands/result", "/api/tools/shape-descriptors/result", "/api/tools/island-frequency/result", "/api/tools/island-cleanup/result"}:
                tool = path.removeprefix("/api/tools/").removesuffix("/result")
                result = get_cached_analysis(tool, latest=True)
                if result is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "No cached analysis result")
                else:
                    self.send_json({"result": result}, send_body)
                return
            if path == "/api/project/frames":
                self.send_json({"frames": frame_records(user)}, send_body)
                return
            if path == "/api/frame-workspace":
                try:
                    image_id = int(parse_qs(parsed.query).get("image_id", [""])[0])
                    self.send_json(frame_workspace(user, image_id), send_body)
                except PermissionError as error:
                    self.send_error(HTTPStatus.FORBIDDEN, str(error))
                except (TypeError, ValueError) as error:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            if path.startswith("/api/media/") or path.startswith("/api/image/"):
                try:
                    image_id = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not can_access_image(user, image_id):
                    self.send_error(HTTPStatus.FORBIDDEN, "Frame is not assigned to this annotator")
                    return
        try:
            if path == "/api/project/save/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                job = get_project_save_job(job_id)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Project save job not found")
                else:
                    self.send_json(job, send_body)
                return
            if path == "/api/project/config":
                self.send_json({"bucket": PROJECT_SOURCE, "token_configured": bool(PROJECT_TOKEN), "autosave": PROJECT_AUTOSAVE}, send_body)
                return
            if path == "/api/project/collaboration":
                self.send_json({"island_cleans": read_collaboration().get("island_cleans", {})}, send_body)
                return
            if path == "/api/project":
                document = read_project_manifest()
                if document is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "No saved project")
                else:
                    self.send_json(document, send_body)
                return
            if path == "/api/dataset":
                data = load_dataset()
                body = {
                    "info": data["info"],
                    "categories": data["categories"],
                    "category_colors": data["category_colors"],
                    "images": frame_records(user),
                    "annotation_count": len(data["annotations"]),
                    "max_annotation_id": max((annotation["id"] for annotation in data["annotations"]), default=0),
                    "source": DATASET_SOURCE,
                    "project_source": PROJECT_SOURCE,
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
                    "source_url": None if USE_LOCAL_DATASET else IMAGE_URL.format(filename=image["file_name"]),
                    "annotations": data["annotation_index"].get(image_id, []),
                    "max_annotation_id": max((annotation["id"] for annotation in data["annotations"]), default=0),
                    "categories": data["categories"],
                    "category_colors": data["category_colors"],
                }
                self.send_json(body, send_body)
                return
            if path == "/api/tools/excess-islands/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                job = get_excess_island_job(job_id)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Excess Island job not found")
                    return
                self.send_json(job, send_body)
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
            if path == "/api/tools/island-cleanup/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                job = get_island_cleanup_job(job_id)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Island Cleanup job not found")
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
        if USE_LOCAL_DATASET:
            self.send_local_image(local_dataset_image_path(image), send_body)
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

    def send_local_image(self, path, send_body):
        try:
            size = path.stat().st_size
            start = 0
            end = size - 1
            status = HTTPStatus.OK
            range_value = self.headers.get("Range", "")
            if range_value.startswith("bytes="):
                start_text, separator, end_text = range_value[6:].partition("-")
                if not separator:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                start = int(start_text) if start_text else 0
                end = int(end_text) if end_text else size - 1
                if start < 0 or start >= size or end < start:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=3600")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if send_body and size:
                with path.open("rb") as file:
                    file.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = file.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)


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
    parser.add_argument("--coco-url", default=None, help="remote COCO instances JSON URL; overrides local dataset")
    parser.add_argument("--image-url", default=None, help="remote image URL template containing {filename}")
    parser.add_argument("--workers", type=int, default=WORKERS, help="Island Frequency workers; 0 uses all available CPUs")
    parser.add_argument("--dataset-dir", default=None, help="local dataset directory override")
    parser.add_argument("--hf-source", default=os.environ.get("HF_DATASET_SOURCE", str(PROJECT_CONFIG.get("dataset_source", DEFAULT_HF_SOURCE))), help="dataset bucket path or hf://buckets URI")
    parser.add_argument("--hf-token", default=None, help="optional Hugging Face dataset token; prefer HF_TOKEN")
    parser.add_argument("--project-dir", default=os.environ.get("PROJECT_DIR"), help="local project workspace override")
    parser.add_argument("--project-name", default=None, help="project name used under the app cache directory")
    parser.add_argument("--project-bucket", default=None, help="project bucket ID to create or import")
    parser.add_argument("--project-source", default=None, help="existing project bucket path to import")
    parser.add_argument("--project-token", default=None, help="HF token for project bucket access; prefer HF_TOKEN")
    parser.add_argument("--no-prompt", action="store_true", help="do not prompt when the local dataset is missing")
    return parser


def main():
    global WORKERS
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.workers < 0:
        parser.error("--workers must be 0 or greater")
    if args.image_url and "{filename}" not in args.image_url:
        parser.error("--image-url must contain {filename}")
    WORKERS = args.workers
    ensure_bootstrap_manager()
    try:
        remote_coco_url = args.coco_url or os.environ.get("COCO_URL")
        remote_image_url = args.image_url or os.environ.get("IMAGE_URL")
        project_loaded = False
        if not remote_coco_url and not remote_image_url:
            project_loaded = prepare_project(
                project_dir=args.project_dir,
                project_name=args.project_name,
                project_source=args.project_bucket or args.project_source,
                project_token=args.project_token,
                no_prompt=args.no_prompt,
            )
        if not project_loaded:
            prepare_dataset(
                dataset_dir=args.dataset_dir or str(PROJECT_DIR / "dataset"),
                remote_coco_url=remote_coco_url,
                remote_image_url=remote_image_url,
                hf_source=args.hf_source,
                hf_token=args.hf_token,
                no_prompt=args.no_prompt,
            )
        data = load_dataset()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"COCO browser listening on http://{args.host}:{args.port}", flush=True)
    print(f"Dataset source: {DATASET_SOURCE}", flush=True)
    print(f"Images: {len(data['images'])}, annotations: {len(data['annotations'])}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
