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
- Runs background dataset tools with live status-bar progress.
- Includes the **Island Frequency** tool for connected-component analysis across all instances.
- Includes snapshot-based undo/redo and a Photoshop-style history selector.

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
--workers N         Island Frequency worker processes; 0 uses all available CPUs
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
WORKERS=0 \
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
| Undo | `Ctrl/Cmd+Z` |
| Redo | `Ctrl/Cmd+Shift+Z` or `Ctrl+Y` |
| Stop active analysis | **Stop** in the status bar |

## Island Frequency Tool

Open **Tools** in the header and select **Island Frequency**.

The tool analyzes every detection instance in the currently configured COCO dataset. For each instance it:

1. Decodes the compressed COCO RLE mask.
2. Splits column-major foreground runs into vertical intervals.
3. Connects overlapping intervals in adjacent image columns.
4. Counts the resulting connected components using 4-connectivity.

While the job runs, the bottom status bar displays the tool name, percentage progress, and processed instance count. When complete, a popup shows:

- Total detection instances
- Total islands
- Mean and median islands per instance
- Mode and minimum/maximum range
- Single-island and multi-island instance counts
- A frequency distribution graph
- Per-category island summaries

The analysis runs in a background server thread and uses all CPUs available to the server process by default. Set `--workers N` to cap the number of worker processes, for example:

```bash
python server.py --workers 2
```

Each frequency row also has an **Inspect** action. Choose a value such as `N=2` to open a larger inspector popup over the report. The inspector contains a scrollable, paginated instance list; select an instance to focus its bounding box rather than showing the whole image, and render each connected mask island in a different color. Use Up/Down Arrow to move through the inspector list.

The inspector's **Edit mask** mode supports add/erase brush strokes and a brush-size control. Mask strokes are stored in browser local storage and applied to the selected instance when exporting through the server.

**Split instance** mode lets you click one or more colored mask islands, choose a replacement class, and create a new COCO instance from only those islands. The original instance is marked removed; the split is undoable and appears in exports.

## Shape Descriptor Lab

Open **Tools → Shape Descriptor Lab**. The tool does not calculate every descriptor on launch. Select a subgroup of descriptors and classes, then run only that measurement.

Available descriptors:

- Aspect ratio
- Compactness / circularity
- Solidity
- Convexity
- Eccentricity
- Normalized perimeter
- Hu moments
- Zernike moments
- Fourier descriptors
- Hausdorff distance
- Chamfer distance

The first nine descriptors are calculated from the instance mask. Hausdorff and Chamfer compare each mask against the first valid instance in its selected class as a reference. Results are grouped by class and include count, mean, standard deviation, min, P05, median, P95, max, and a histogram for every selected descriptor group.

## Excess Island Filter

Open **Tools → Excess Island Filter**. Enable at least one filter:

- Island count range
- Maximum island area ratio relative to the largest island

The tool scans all instances, reports multi-island candidates, and shows candidate island counts, component areas, and the number of islands matching the drop threshold. It is a detection/report tool and does not silently modify masks.



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

Every review, bbox edit, new box, category change, and mask brush stroke creates a history snapshot. Use the header Undo/Redo buttons, the history selector, or the keyboard shortcuts to move through the edit timeline.

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
| `POST` | `/api/tools/island-frequency/start` | Start an Island Frequency analysis job |
| `GET` | `/api/tools/island-frequency/status?job_id={id}` | Poll Island Frequency progress and results |

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
