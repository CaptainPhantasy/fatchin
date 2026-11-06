"""Build the transparent FatChin icon pyramid from its authentic mascot.

Optional authoring dependencies: pillow, numpy, scipy.
Use --repair SOURCE.png only to import an original black-matted source; normal
runs resize the checked-in, repaired mascot without rematting its edges.
"""

import argparse
from PIL import Image, ImageFilter
import numpy as np
from scipy import ndimage as nd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "web"


def repair_source(source):
    im = np.array(Image.open(source).convert("RGBA"))
    rgb = im[:, :, :3].astype(float)
    original = im[:, :, 3] / 255.0
    # Flood from the exterior only. Interior dark ink is never used as a key.
    background = (original < 0.03) | (rgb.max(2) < 12)
    seed = np.zeros(background.shape, bool)
    seed[0, :] = background[0, :]
    seed[-1, :] = background[-1, :]
    seed[:, 0] = background[:, 0]
    seed[:, -1] = background[:, -1]
    exterior = nd.binary_propagation(seed, mask=background)
    shape = nd.binary_fill_holes(~exterior)
    # Remove detached antialias specks; the mascot and string are one main component.
    labels, n = nd.label(shape)
    counts = np.bincount(labels.ravel())
    counts[0] = 0
    shape = labels == counts.argmax()
    core = nd.binary_erosion(shape, iterations=2)
    alpha = np.where(shape, np.maximum(original, 0.1), 0.0)
    alpha[core] = 1.0
    # Feather only the silhouette boundary, never eyes/lettering/hooves.
    soft = nd.gaussian_filter(shape.astype(float), 0.55)
    boundary = nd.binary_dilation(shape, iterations=1) & ~core
    alpha[boundary] = soft[boundary]
    alpha[alpha < 0.025] = 0.0
    alpha[core] = 1.0
    # Nearest opaque edge colour removes the old black matte from boundary pixels.
    _, indices = nd.distance_transform_edt(~core, return_indices=True)
    edge_rgb = rgb[tuple(indices)]
    rgb[boundary] = edge_rgb[boundary]
    rgb[alpha == 0] = 0
    out = Image.fromarray(
        np.dstack(
            [np.clip(rgb, 0, 255).astype("uint8"), np.rint(alpha * 255).astype("uint8")]
        )
    )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair", type=Path, help="Original black-matted mascot image"
    )
    args = parser.parse_args()
    root = ROOT
    if args.repair:
        out = repair_source(args.repair)
        out.save(root / "brand/fatchin-logo.png", optimize=True)
    else:
        out = Image.open(root / "brand/fatchin-logo.png").convert("RGBA")
    # Preserve full golden string in the source and frame the whole character in icons.
    bbox = out.getbbox()
    mascot = out.crop(bbox)
    side = max(mascot.size)
    canvas = Image.new("RGBA", (round(side / 0.9),) * 2)
    canvas.alpha_composite(
        mascot,
        ((canvas.width - mascot.width) // 2, (canvas.height - mascot.height) // 2),
    )
    for size, name in [
        (16, "favicon-16x16.png"),
        (32, "favicon-32x32.png"),
        (180, "apple-touch-icon.png"),
        (192, "icon-192.png"),
        (512, "icon-512.png"),
    ]:
        icon = canvas.resize((size, size), Image.Resampling.LANCZOS)
        if size <= 32:
            # Sharpen colour only; leave antialiased coverage intact.
            sharp = icon.convert("RGB").filter(
                ImageFilter.UnsharpMask(radius=0.5, percent=115, threshold=2)
            )
            sharp.putalpha(icon.getchannel("A"))
            icon = sharp
        icon.save(root / name, optimize=True)
    canvas.save(
        root / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)]
    )


if __name__ == "__main__":
    main()
