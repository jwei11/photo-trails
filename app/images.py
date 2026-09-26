import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageOps

from app.exif import PhotoMetadata, read_metadata

pillow_heif.register_heif_opener()

RGB_MODES = ("RGB", "RGBA", "RGBX", "P")
THUMB_SIZE = 400
WEB_SIZE = 2048

# Refuse decompression bombs: a tiny file that expands to billions of pixels.
Image.MAX_IMAGE_PIXELS = 200_000_000


@dataclass
class ProcessedImage:
    metadata: PhotoMetadata
    thumb: bytes
    web: bytes


def process(contents: bytes) -> ProcessedImage:
    with Image.open(io.BytesIO(contents)) as img:
        metadata = read_metadata(img)
        upright = ImageOps.exif_transpose(img)
        metadata.width, metadata.height = upright.size
        # A CMYK or grayscale profile would mislabel the RGB pixels we write.
        icc = img.info.get("icc_profile") if img.mode in RGB_MODES else None
        return ProcessedImage(
            metadata=metadata,
            thumb=_webp(upright, THUMB_SIZE, quality=75, icc=icc),
            web=_webp(upright, WEB_SIZE, quality=82, icc=icc),
        )


def _webp(img: Image.Image, max_side: int, quality: int, icc: bytes | None) -> bytes:
    copy = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # No exif= argument, so no metadata (GPS included) is written to the derivative.
    copy.save(out, "WEBP", quality=quality, method=4, icc_profile=icc or b"")
    return out.getvalue()
