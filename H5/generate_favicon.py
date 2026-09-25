"""Build the browser ICO from the Photoshop-exported squid icon."""

from pathlib import Path

from PIL import Image

here = Path(__file__).resolve().parent
source = here / "favicon-squid.png"
target = here / "favicon-squid.ico"

with Image.open(source) as image:
    image.convert("RGBA").save(
        target,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
