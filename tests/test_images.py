import io
from datetime import datetime, timezone

import pytest
from PIL import Image, ImageCms, TiffImagePlugin

from app.exif import IFD_EXIF, IFD_GPS, parse_exif_datetime, parse_exif_offset
from app.images import process


def R(value):
    return TiffImagePlugin.IFDRational(value)


def make_jpeg(width=600, height=400, orientation=None, gps=True, when="2025:07:01 14:05:09"):
    img = Image.new("RGB", (width, height), "red")
    img.paste("blue", (0, 0, width // 2, height))  # left half blue, to check rotation
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15\x00"
    if orientation:
        exif[0x0112] = orientation
    ifd = exif.get_ifd(IFD_EXIF)
    if when:
        ifd[0x9003] = when
        ifd[0x9011] = "+01:00"
        ifd[0x9291] = "25"
    if gps:
        g = exif.get_ifd(IFD_GPS)
        g[1], g[2] = "N", (R(38), R(43), R(20.28))
        g[3], g[4] = "W", (R(9), R(8), R(21.48))
        g[5], g[6] = b"\x00", R(112.5)
        g[29], g[7] = "2025:07:01", (R(13), R(5), R(9))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_reads_time_camera_and_location():
    meta = process(make_jpeg()).metadata
    assert meta.camera_make == "Apple"
    assert meta.camera_model == "iPhone 15"  # trailing NUL stripped
    assert meta.camera_time == datetime(2025, 7, 1, 14, 5, 9, 250000)
    assert meta.exif_offset_min == 60
    assert meta.lat == pytest.approx(38.72230, abs=1e-5)
    assert meta.lon == pytest.approx(-9.13930, abs=1e-5)  # W becomes negative
    assert meta.altitude_m == pytest.approx(112.5)
    assert meta.gps_time == datetime(2025, 7, 1, 13, 5, 9, tzinfo=timezone.utc)
    assert meta.tags["Make"] == "Apple"
    assert "GPSLatitude" in meta.tags["GPS"]


def test_photo_without_metadata_is_still_processed():
    img = Image.new("RGB", (100, 100))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    result = process(buf.getvalue())
    assert result.metadata.camera_time is None
    assert result.metadata.lat is None
    assert result.thumb


def test_derivatives_contain_no_metadata():
    result = process(make_jpeg())
    for data in (result.thumb, result.web):
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "WEBP"
            assert not img.getexif()
            assert "exif" not in img.info


def test_rgb_colour_profile_is_kept_but_cmyk_profile_is_dropped():
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    cmyk_profile = b"fake CMYK profile bytes"

    rgb, cmyk = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(rgb, "JPEG", icc_profile=srgb)
    Image.new("CMYK", (64, 64), (0, 255, 255, 0)).save(cmyk, "JPEG", icc_profile=cmyk_profile)

    with Image.open(io.BytesIO(process(rgb.getvalue()).web)) as out:
        assert out.info.get("icc_profile") == srgb
    with Image.open(io.BytesIO(process(cmyk.getvalue()).web)) as out:
        assert not out.info.get("icc_profile")
        assert out.mode == "RGB"


def test_orientation_tag_is_applied_to_derivatives():
    # Orientation 6 = "rotate 90° clockwise to display": a landscape sensor image shown as portrait.
    result = process(make_jpeg(width=600, height=400, orientation=6))
    assert (result.metadata.width, result.metadata.height) == (400, 600)
    with Image.open(io.BytesIO(result.thumb)) as thumb:
        assert thumb.height > thumb.width
        top_left = thumb.convert("RGB").getpixel((5, 5))
        assert top_left[2] > 200 and top_left[0] < 60  # blue half rotated to the top


def test_thumbnail_fits_within_size():
    result = process(make_jpeg(width=4000, height=3000))
    with Image.open(io.BytesIO(result.thumb)) as t, Image.open(io.BytesIO(result.web)) as w:
        assert max(t.size) == 400
        assert max(w.size) == 2048


def test_null_island_coordinates_are_treated_as_missing():
    img = Image.new("RGB", (10, 10))
    exif = Image.Exif()
    g = exif.get_ifd(IFD_GPS)
    g[1], g[2], g[3], g[4] = "N", (R(0), R(0), R(0)), "E", (R(0), R(0), R(0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert process(buf.getvalue()).metadata.lat is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2025:07:01 14:05:09", datetime(2025, 7, 1, 14, 5, 9)),
        ("0000:00:00 00:00:00", None),
        ("    :  :     :  :  ", None),
        (b"2025:07:01 14:05:09\x00", datetime(2025, 7, 1, 14, 5, 9)),
        (None, None),
    ],
)
def test_parse_exif_datetime(raw, expected):
    assert parse_exif_datetime(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("+01:00", 60), ("-04:00", -240), ("+05:30", 330), ("+99:00", None), ("garbage", None)],
)
def test_parse_exif_offset(raw, expected):
    assert parse_exif_offset(raw) == expected
