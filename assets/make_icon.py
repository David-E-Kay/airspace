"""Draw the radar icon and write assets/icon.ico + assets/icon-256.png.

This is the icon's source of truth - the committed .ico and .png come out of
it. Run it from the repo root, and only when changing the icon:

    pip install pillow && python assets/make_icon.py

Pillow is NOT a dependency of the board. Nothing in dashboard.py imports it.
"""
from PIL import Image, ImageDraw

S = 1024          # draw big, downscale for smooth edges
BG = (21, 23, 28, 255)
RING = (49, 53, 63, 255)
AMBER = (240, 198, 116, 255)
GREY = (107, 114, 128, 255)
WEDGE = tuple(round(b + 0.42 * (a - b)) for b, a in
              zip(BG[:3], AMBER[:3])) + (255,)


def draw(size=S, simple=False):
    k = size / 256.0
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x, y, r):
        return [(x - r) * k, (y - r) * k, (x + r) * k, (y + r) * k]

    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=56 * k, fill=BG)
    # The wedge is amber mixed into the background rather than amber with an
    # alpha: ImageDraw overwrites pixels instead of blending them, so a
    # translucent fill would punch a see-through hole in the tile.
    d.pieslice(box(128, 128, 86), start=-90, end=-45, fill=WEDGE)
    # Below ~32px the second ring and the dim blips collapse into noise, so
    # the small sizes get fewer, fatter marks. Same picture, less of it.
    rings, weight = ((88,), 13) if simple else ((88, 52), 7)
    for r in rings:
        d.ellipse(box(128, 128, r), outline=RING, width=int(round(weight * k)))
    d.line([128 * k, 128 * k, 128 * k, 42 * k], fill=AMBER,
           width=int(round(weight * k)))
    d.ellipse(box(172, 94, 19 if simple else 13), fill=AMBER)
    if not simple:
        d.ellipse(box(88, 164, 10), fill=GREY)
        d.ellipse(box(156, 180, 10), fill=GREY)
    return img


full, small = draw(), draw(simple=True)
full.resize((256, 256), Image.LANCZOS).save('assets/icon-256.png')


def at(n):
    return (small if n <= 32 else full).resize((n, n), Image.LANCZOS)


# .ico carries every size Windows asks for and picks per context. The image
# save() is called on must be the LARGEST: Pillow skips any requested size
# bigger than the base, so leading with 16x16 silently writes a one-size file.
frames = [at(n) for n in (256, 128, 64, 48, 32, 24, 16)]
frames[0].save('assets/icon.ico', format='ICO',
               sizes=[im.size for im in frames], append_images=frames[1:])
print('wrote assets/icon.ico and assets/icon-256.png')
