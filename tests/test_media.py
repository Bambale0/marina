import io
import shutil
import subprocess

import pytest
from tests.test_identity import app, login


def test_catalog_video_is_parsed_not_only_checked_by_extension(app, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for media integration")
    target = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=32x32:d=0.2",
            "-an",
            "-c:v",
            "libx264",
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
        timeout=15,
    )
    c = login(app)
    draft = c.post(
        "/api/admin/content",
        json={
            "kind": "exercise",
            "title": "Synthetic video fixture",
            "reason": "Media integration fixture",
            "data": {},
        },
    ).json
    url = "/api/admin/content/" + draft["id"] + "/asset"
    valid = target.read_bytes()
    assert (
        c.post(
            url,
            data={
                "reason": "Verify video",
                "file": (io.BytesIO(valid), "fixture.mp4", "video/mp4"),
            },
        ).status_code
        == 201
    )
    assert (
        c.post(
            url,
            data={
                "reason": "Reject corrupt video",
                "file": (io.BytesIO(valid[:8] + b"broken"), "broken.mp4", "video/mp4"),
            },
        ).status_code
        == 422
    )
