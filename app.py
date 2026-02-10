import io
from flask import Flask, render_template, request, send_file, abort
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import inch
from PIL import Image

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB total request limit

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

def _check_file(file_storage):
    if not file_storage or not file_storage.filename:
        return False
    name = file_storage.filename.lower()
    return any(name.endswith(ext) for ext in ALLOWED_EXTS)

def _open_image(file_storage) -> Image.Image:
    # Pillow safety: basic verify + convert to RGB for consistent output
    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)
    img.verify()
    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream).convert("RGB")
    return img


def _cover_crop_to_box(pil_img: Image.Image, box_w: float, box_h: float) -> Image.Image:
    """
    Returns a NEW PIL image cropped (center-crop) to match the box aspect ratio,
    so it will fill the box completely when resized.
    """
    img_w, img_h = pil_img.size
    img_ar = img_w / img_h
    box_ar = box_w / box_h

    # If image is wider than box, crop left/right. If taller, crop top/bottom.
    if img_ar > box_ar:
        # crop width
        new_w = int(img_h * box_ar)
        left = (img_w - new_w) // 2
        return pil_img.crop((left, 0, left + new_w, img_h))
    else:
        # crop height
        new_h = int(img_w / box_ar)
        top = (img_h - new_h) // 2
        return pil_img.crop((0, top, img_w, top + new_h))

def _draw_pil_cover(c: canvas.Canvas, pil_img: Image.Image,
                    box_x: float, box_y: float, box_w: float, box_h: float,
                    rotate_180: bool = False):
    """
    Draws a PIL image so it FILLS the box (cropped), optionally rotated 180 degrees.
    """
    img = pil_img

    if rotate_180:
        img = img.rotate(180, expand=True)

    # Crop to box aspect ratio, then draw stretched exactly to box size
    cropped = _cover_crop_to_box(img, box_w, box_h)
    c.drawImage(ImageReader(cropped), box_x, box_y, width=box_w, height=box_h, mask="auto")


def build_card_pdf(front_img, back_img, inner_left_img, inner_right_img) -> bytes:
    buf = io.BytesIO()

    page_w, page_h = landscape(letter)  # 11 x 8.5
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))

    margin = 0.35 * inch
    gutter = 0.10 * inch
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin

    quad_w = (usable_w - gutter) / 2
    quad_h = (usable_h - gutter) / 2

    top_y = margin + quad_h + gutter
    bot_y = margin
    left_x = margin
    right_x = margin + quad_w + gutter

    # Outside (top row): FRONT (top-left, upside down) | BACK (top-right, upside down)
    _draw_pil_cover(c, front_img, left_x, top_y, quad_w, quad_h, rotate_180=True)
    _draw_pil_cover(c, back_img, right_x, top_y, quad_w, quad_h, rotate_180=True)

    # Inside (bottom row): INNER LEFT | INNER RIGHT (not rotated)
    _draw_pil_cover(c, inner_left_img, left_x, bot_y, quad_w, quad_h, rotate_180=False)
    _draw_pil_cover(c, inner_right_img, right_x, bot_y, quad_w, quad_h, rotate_180=False)


    # Light fold / cut guides
    c.saveState()
    c.setLineWidth(0.5)
    c.setDash(2, 3)
    c.setStrokeGray(0.65)
    c.line(page_w / 2, 0, page_w / 2, page_h)      # vertical fold
    c.line(0, page_h / 2, page_w, page_h / 2)      # middle guide
    c.restoreState()

    c.showPage()
    c.save()

    buf.seek(0)
    return buf.getvalue()

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    front = request.files.get("front")
    back = request.files.get("back")
    inner_left = request.files.get("inner_left")
    inner_right = request.files.get("inner_right")

    if not all(_check_file(f) for f in [front, back, inner_left, inner_right]):
        return abort(400, description="Please upload 4 image files (jpg/png/webp/bmp).")

    try:
        front_img = _open_image(front)
        back_img = _open_image(back)
        inner_left_img = _open_image(inner_left)
        inner_right_img = _open_image(inner_right)
    except Exception:
        return abort(400, description="One of the files is not a valid image.")

    pdf_bytes = build_card_pdf(front_img, back_img, inner_left_img, inner_right_img)

    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name="birthday_card.pdf",
        mimetype="application/pdf"
    )

if __name__ == "__main__":
    app.run()

