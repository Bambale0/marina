"""Wrap the original RGB PNG IDAT stream in one PDF page without re-encoding."""

import hashlib
import json
import struct
from pathlib import Path

MAPPING = {
    "3-4": "image_2026-11-05_00-52-45.png",
    "5-6": "image_2026-11-05_00-53-11.png",
    "7-9": "image_2026-11-05_01-01-58.png",
    "10-14": "image_2026-11-05_01-03-50.png",
}


def convert(raw):
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed, header = 8, [], None
    while offset < len(raw):
        size = struct.unpack(">I", raw[offset : offset + 4])[0]
        kind = raw[offset + 4 : offset + 8]
        data = raw[offset + 8 : offset + 8 + size]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            compressed.append(data)
        offset += size + 12
        if kind == b"IEND":
            break
    assert header and header[2:] == (8, 2, 0, 0, 0), "Expected 8-bit RGB, non-interlaced PNG"
    w, h = header[:2]
    stream = b"".join(compressed)
    assert stream
    draw = f"q\n{w} 0 0 {h} 0 0 cm\n/Im0 Do\nQ\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>".encode(
            "ascii"
        ),
        f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Interpolate false /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns {w} >> /Length {len(stream)} >>\nstream\n".encode(
            "ascii"
        )
        + stream
        + b"\nendstream",
        f"<< /Length {len(draw)} >>\nstream\n".encode("ascii") + draw + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{i} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for value in offsets[1:]:
        output.extend(f"{value:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(output)


def main(source_dir, target_dir):
    source_dir, target_dir = Path(source_dir), Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for group, name in MAPPING.items():
        raw = (source_dir / name).read_bytes()
        pdf = convert(raw)
        path = target_dir / f"Pamyatka_{group}_podgotovka.pdf"
        path.write_bytes(pdf)
        result.append(
            {
                "group": group,
                "source_png": name,
                "source_image": name,
                "filename": path.name,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "path": str(path),
                "sha256": hashlib.sha256(pdf).hexdigest(),
                "size": len(pdf),
            }
        )
    return result


if __name__ == "__main__":
    import sys

    print(json.dumps(main(sys.argv[1], sys.argv[2]), ensure_ascii=False))
