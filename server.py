import argparse
import json
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8888"))
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
_dataset = None


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_POST(self):
        if urlparse(self.path).path != "/api/export":
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
        created = payload.get("created", [])
        annotations = []
        for annotation in data["annotations"]:
            review = reviews.get(str(annotation["id"]))
            if review == "remove":
                continue
            output = dict(annotation)
            edit = edits.get(str(annotation["id"]))
            if edit:
                output.update(edit)
            if review:
                output["review"] = review
            annotations.append(output)
        annotations.extend(created)
        return json.dumps(
            {
                "info": data["info"],
                "images": data["images"],
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
    return parser


def main():
    global COCO_URL, IMAGE_URL
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if "{filename}" not in args.image_url:
        parser.error("--image-url must contain {filename}")
    COCO_URL = args.coco_url
    IMAGE_URL = args.image_url
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
