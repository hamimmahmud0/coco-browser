import argparse
import json
from collections import Counter
from multiprocessing import get_context
import mimetypes
import os
import threading
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
