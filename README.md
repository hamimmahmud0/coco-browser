# COCO 1.0 Annotation Browser

A dependency-free web browser for reviewing and editing COCO 1.0 instance annotations stored in a Hugging Face Storage Bucket.

The default dataset is:

- Bucket: `hamimmahmud0/SAM_COCO_v1_b2_3024`
- Prefix: `annotate`
- Images: 414
- Annotations: 157,921
- Categories: 8

## Features

- Streams images through the local server instead of loading them directly from the bucket CDN.
- Displays COCO bounding boxes and compressed RLE segmentation masks.
- Filters the canvas, object list, and mask rendering using the left category checkboxes.
- Selects objects from the canvas or fixed right-hand object sidebar.
- Recenters and adaptively zooms to an object selected from the sidebar.
- Always keeps the selected bounding box visible, even when **Boxes** is unchecked.
- Supports drawing, moving, resizing, reviewing, and removing bounding boxes.
- Stores edits in browser `localStorage` and exports valid COCO 1.0 JSON.
- Uses only the Python standard library for the server.

## Requirements

- Python 3.10 or newer
- A modern browser with Canvas support
- Network access to the configured COCO JSON and image URLs

No package installation is required.

## Quick Start

```bash
cd /root/work/coco-browser
python server.py
```

Open:

```text
http://localhost:8888
```

The server binds to `0.0.0.0:8888` by default, so it is also reachable from another machine on the same network.

## Command-Line Options

```text
--host HOST          Interface to bind
--port PORT          Port to bind
--coco-url URL       Direct URL to a COCO instances JSON file
--image-url TEMPLATE Image URL template containing {filename}
```

Show all options:

```bash
python server.py --help
```

### Custom Port

```bash
python server.py --host 127.0.0.1 --port 9090
```

### Custom COCO Dataset

```bash
python server.py \
  --coco-url "https://example.com/dataset/instances.json" \
  --image-url "https://example.com/images/{filename}"
```

The image URL must contain the literal `{filename}` placeholder. The server substitutes each COCO image's `file_name` when proxying media.

### Environment Variables

The defaults can also be set with environment variables:

```bash
HOST=127.0.0.1 \
PORT=9090 \
COCO_URL="https://example.com/dataset/instances.json" \
IMAGE_URL="https://example.com/images/{filename}" \
python server.py
```

Command-line arguments override environment defaults.

## Controls

| Action | Control |
| --- | --- |
| Previous image | `A` or Left Arrow |
| Next image | `D` or Right Arrow |
| Previous object | Up Arrow |
| Next object | Down Arrow |
| Toggle boxes | `B` |
| Toggle masks | `M` |
| Toggle pan mode | `H` |
| Pan temporarily | Hold `Space` and drag |
| Zoom | Mouse wheel |
| Zoom in/out | `+` / `-` |
| Reset zoom and pan | `Fit` or zoom percentage |
| Select active category | `1` through `8` |
| Mark selected object | `Keep`, `Fix`, or `Remove` |
| Mark for removal | `Delete` or `Backspace` |
| Draw a bbox | Enable **Draw bbox**, hold `Shift`, or press `B`, then drag |
| Deselect object | `Escape` |

## Editing Model

### Existing annotations

- Drag a bounding box to move it.
- Drag a corner or edge handle to resize it.
- Use the active category selector to change its category.
- Use **Keep**, **Fix**, or **Remove** to assign a review state.
- Press `Delete` to mark an annotation for removal.

### New annotations

1. Enable **Draw bbox** or hold `Shift`.
2. Select a category.
3. Drag a box over the image.
4. Export the current image or full dataset.

New boxes contain no segmentation mask. Their COCO segmentation field is an empty list until a mask is added by an external process.

## Persistence and Export

Edits are stored in the browser under these keys:

- `coco-reviews`
- `coco-edits`
- `coco-created`

Clearing site data removes these local changes.

Use:

- **Export image** to download a COCO file containing the current image and its visible annotations.
- **Export full COCO** to rebuild the full dataset with edits, review fields, and removed annotations omitted.

The application does not write back to the Hugging Face bucket. Uploading the exported JSON is an explicit manual step.

## Cloudflare Tunnel

To expose a local instance through a temporary Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://localhost:8888
```

The application has no authentication. Do not expose it publicly without placing an authentication layer in front of it.

## API Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Server health check |
| `GET` | `/api/dataset` | Dataset metadata, images, categories, and counts |
| `GET` | `/api/image/{id}` | One image, its annotations, and media URL |
| `GET` | `/api/media/{id}` | Streamed image proxy with range support |
| `POST` | `/api/export` | Build a full edited COCO JSON export |

The complete COCO source is downloaded and indexed once on the first dataset request, then kept in server memory for the lifetime of the process.

## Project Structure

```text
coco-browser/
├── README.md
├── server.py
└── static/
    ├── app.js
    ├── index.html
    └── style.css
```

## Troubleshooting

### Image remains loading

Check the server log and test the media proxy:

```bash
curl -I http://localhost:8888/api/media/2
```

The configured image URL must be publicly accessible or reachable from the server.

### Dataset request fails

Verify the COCO source directly:

```bash
python server.py --coco-url "https://example.com/instances.json"
```

The URL must return JSON containing `images`, `annotations`, and `categories` arrays.

### Port already in use

Choose another port:

```bash
python server.py --port 8889
```

### Browser shows an older interface

Perform a hard refresh. The HTML references versioned CSS and JavaScript assets to avoid stale browser caches.
# coco-browser
