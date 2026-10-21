import json
import subprocess
import tempfile
from . import Problem, require


def verify_video(data):
    """Parse media with ffprobe, with no network protocols allowed."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(data)
            f.flush()
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-show_entries",
                    "stream=codec_type,width,height:format=duration",
                    "-of",
                    "json",
                    f.name,
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            require(result.returncode == 0, "Видео не прошло техническую проверку")
            info = json.loads(result.stdout)
            require(
                any(
                    s.get("codec_type") == "video"
                    and s.get("width", 0) > 0
                    and s.get("height", 0) > 0
                    for s in info.get("streams", [])
                )
                and float(info.get("format", {}).get("duration", 0)) > 0,
                "Видео не содержит доступной видеодорожки",
            )
            return info
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise Problem("Не удалось проверить видео. Проверьте ffprobe и формат файла", 422)
