import io
import os
from flask import Flask, render_template, request, send_file, abort
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import inch
from PIL import Image

# Enable HEIC/HEIF support if pillow-heif is installed
try:
    import pillow_heif  # pip install pillow-heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

app = Flask(__name__)

# Limit total upload size (all 4 images combined)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40MB

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}


# ---- Helpers: file validation + image loading ----

def _check_file(file_storage) -> bool:
    if not file_storage or not file_storage.filename:
        return False
    name = file_storage.filename.lower()
    return any(name.endswith(ext) for ext in ALLOWED_EXTS)


def _open_image(file_storage) -> Image.Image:
    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)

    img.verify()  # quick validation

    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream).convert("RGB")

    img.load()  # <-- ADD IT HERE (forces image into memory immediately)

    return img


# ---- Helpers: crop-to-fill + draw into a box ----

def _cover_crop_to_box(pil_img: Image.Image, box_w: float, box_h: float) -> Image.Image:
    """
    Center-crop to match the box aspect ratio so the image fills the box with no white bars.
    """
    img_w, img_h = pil_img.size
    img_ar = img_w / img_h
    box_ar = box_w / box_h

    if img_ar > box_ar:
        # image too wide -> crop left/right
        new_w = int(img_h * box_ar)
        left = (img_w - new_w) // 2
        return pil_img.crop((left, 0, left + new_w, img_h))
    else:
        # image too tall -> crop top/bottom
        new_h = int(img_w / box_ar)
        top = (img_h - new_h) // 2
        return pil_img.crop((0, top, img_w, top + new_h))

def _draw_pil_contain(
    c,
    pil_img,
    box_x,
    box_y,
    box_w,
    box_h,
    rotate_180=False
):
    img = pil_img
    if rotate_180:
        img = img.rotate(180, expand=True)

    img_w, img_h = img.size
    scale = min(box_w / img_w, box_h / img_h)

    draw_w = img_w * scale
    draw_h = img_h * scale

    x = box_x + (box_w - draw_w) / 2
    y = box_y + (box_h - draw_h) / 2

    c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h, mask="auto")

def _draw_pil_cover(
    c: canvas.Canvas,
    pil_img: Image.Image,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    rotate_180: bool = False
):
    """
    Draw an image so it completely fills the box (cropped), optionally rotated 180 degrees.
    """
    img = pil_img
    if rotate_180:
        img = img.rotate(180, expand=True)

    cropped = _cover_crop_to_box(img, box_w, box_h)
    c.drawImage(ImageReader(cropped), box_x, box_y, width=box_w, height=box_h, mask="auto")


# ---- Helpers: downscale to avoid OOM on Render Free ----

MAX_PIXELS = 8_000_000    # 8MP
TARGET_DPI = 200          # still good print quality for a card


def _downscale_for_print(img: Image.Image, box_w_points: float, box_h_points: float) -> Image.Image:
    """
    Downscale an image to a reasonable size for printing into a given panel.
    box_w_points/box_h_points are ReportLab points (72 points = 1 inch).
    """
    # Hard cap megapixels first (prevents massive iPhone shots from killing RAM)
    total_px = img.width * img.height
    if total_px > MAX_PIXELS:
        ratio = (MAX_PIXELS / total_px) ** 0.5
        new_w = max(1, int(img.width * ratio))
        new_h = max(1, int(img.height * ratio))
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Then cap to the print panel size at TARGET_DPI (with a little cushion for crop)
    box_w_in = box_w_points / 72.0
    box_h_in = box_h_points / 72.0
    target_w = int(box_w_in * TARGET_DPI * 1.35)
    target_h = int(box_h_in * TARGET_DPI * 1.35)

    if img.width > target_w or img.height > target_h:
        img = img.copy()
        img.thumbnail((target_w, target_h), Image.LANCZOS)

    return img


# ---- PDF builder ----

def build_card_pdf(front_img, back_img, inner_left_img, inner_right_img, fit_mode=None):
    """
    Creates a 1-page PDF (Letter landscape) split into 4 panels:
      Top-left:  FRONT (outside) rotated 180
      Top-right: BACK  (outside) rotated 180
      Bottom-left: Inner Left
      Bottom-right: Inner Right
    """
    buf = io.BytesIO()
    page_w, page_h = landscape(letter)  # 11 x 8.5

    c = canvas.Canvas(buf, pagesize=(page_w, page_h))

    margin = 0.35 * inch
#    gutter = 0.10 * inch
    gutter_x = 0
    gutter_y = 0.06 * inch

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin

    quad_w = (usable_w - gutter_x) / 2
    quad_h = (usable_h - gutter_y) / 2

    # Downscale once to avoid memory spikes
    front_img = _downscale_for_print(front_img, quad_w, quad_h)
    back_img = _downscale_for_print(back_img, quad_w, quad_h)
    inner_left_img = _downscale_for_print(inner_left_img, quad_w, quad_h)
    inner_right_img = _downscale_for_print(inner_right_img, quad_w, quad_h)

    top_y = margin + quad_h + gutter_y
    bot_y = margin
    left_x = margin
    right_x = margin + quad_w + gutter_x

    # Choose drawing function based on checkbox
    draw_fn = _draw_pil_contain if fit_mode == "contain" else _draw_pil_cover

    # Outside (top row): FRONT | BACK (rotated)
    draw_fn(c, front_img, left_x, top_y, quad_w, quad_h, rotate_180=True)
    draw_fn(c, back_img, right_x, top_y, quad_w, quad_h, rotate_180=True)

    # Inside (bottom row)
    draw_fn(c, inner_left_img, left_x, bot_y, quad_w, quad_h, rotate_180=False)
    draw_fn(c, inner_right_img, right_x, bot_y, quad_w, quad_h, rotate_180=False)

    # Light fold guides (optional)
    c.saveState()
    c.setLineWidth(0.5)
    c.setDash(2, 3)
    c.setStrokeGray(0.65)
#    c.line(page_w / 2, 0, page_w / 2, page_h)  # vertical fold guide
#    c.line(0, page_h / 2, page_w, page_h / 2)  # horizontal guide
    c.restoreState()

    c.showPage()
    c.save()

    buf.seek(0)
    return buf.getvalue()


# ---- Routes ----

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    front = request.files.get("front")
    back = request.files.get("back")
    inner_left = request.files.get("inner_left")
    inner_right = request.files.get("inner_right")
    fit_mode = request.form.get("fit_mode")  # None or "contain"


    # Validate presence + extensions
    if not all(_check_file(f) for f in [front, back, inner_left, inner_right]):
        return abort(400, description="Please upload 4 image files (jpg/png/webp/bmp/heic).")

    try:
        front_img = _open_image(front)
        back_img = _open_image(back)
        inner_left_img = _open_image(inner_left)
        inner_right_img = _open_image(inner_right)

        pdf_bytes = build_card_pdf(
          front_img,
          back_img,
          inner_left_img,
          inner_right_img,
          fit_mode=fit_mode
        )


    except Exception as e:
        # Helpful log for Render
        print("ERROR in /generate:", repr(e))
        return abort(
            400,
            description="Could not process one of the images. Try smaller JPG/PNG images (very large photos can fail on free hosting)."
        )

    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name="birthday_card.pdf",
        mimetype="application/pdf"
    )


if __name__ == "__main__":
    app.run()

