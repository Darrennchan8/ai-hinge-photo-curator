import json

import pytest

from curator.ingest import _sidecar, sidecar_photo_name


@pytest.mark.parametrize("sidecar, title, photo", [
    ("IMG_1840.HEIC.supplemental-metadata.json", "IMG_1840.HEIC", "IMG_1840.HEIC"),
    ("IMG_1840.HEIC.supplemental-metadata(1).json", "IMG_1840.HEIC", "IMG_1840(1).HEIC"),
    ("IMG_1840.HEIC(2).json", "IMG_1840.HEIC", "IMG_1840(2).HEIC"),
    ("IMG_20190804_154755_HDR_long_name.jpg.supplemen.json", "IMG_20190804_154755_HDR_long_name.jpg",
     "IMG_20190804_154755_HDR_long_name.jpg"),
    ("party(1).jpg.supplemental-metadata.json", "party(1).jpg", "party(1).jpg"),  # (1) is part of the real name
    ("IMG_7.JPG.json", None, "IMG_7.JPG"),
])
def test_sidecar_photo_name(sidecar, title, photo):
    assert sidecar_photo_name(sidecar, title) == photo


def test_duplicate_named_sidecars_get_distinct_keys():
    meta = {"title": "IMG_1.HEIC", "photoTakenTime": {"timestamp": "1"}, "url": "https://photos.google.com/photo/A"}
    a = _sidecar("T/Google Photos/X/IMG_1.HEIC.supplemental-metadata.json", json.dumps(meta).encode())
    b = _sidecar("T/Google Photos/X/IMG_1.HEIC.supplemental-metadata(1).json", json.dumps(meta).encode())
    assert a[0] == "T/Google Photos/X/IMG_1.HEIC" and b[0] == "T/Google Photos/X/IMG_1(1).HEIC"
