"""Per card: share of pixels that changed between two render sets, and the bounding box of the change."""
import sys
from pathlib import Path

from PIL import Image, ImageChops

a, b = Path(sys.argv[1]), Path(sys.argv[2])
for f in sorted(b.glob('*.png')):
    g = a / f.name
    if not g.exists():
        print(f'{f.stem:26} new'); continue
    x, y = Image.open(g).convert('RGB'), Image.open(f).convert('RGB')
    if x.size != y.size:
        print(f'{f.stem:26} size {x.size} -> {y.size}'); continue
    d = ImageChops.difference(x, y).convert('L').point(lambda v: 255 if v > 8 else 0)
    n = sum(1 for v in d.getdata() if v)
    if n:
        print(f'{f.stem:26} {100 * n / (x.size[0] * x.size[1]):6.2f}%  box {d.getbbox()}')
