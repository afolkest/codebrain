"""Explicit extra Claude homes shared by local ingestion and collection."""
import hashlib
import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".codebrain" / "claude-roots.json"


def local_roots(default: Path) -> list[Path]:
    """Keep the standard home first; additional homes are opt-in, never globbed."""
    try:
        extra = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        extra = []
    if not isinstance(extra, list) or any(not isinstance(p, str) or not p.strip() for p in extra):
        raise ValueError(f"{CONFIG_PATH} must contain a JSON array of absolute directory paths")
    roots = [Path(default).expanduser()]
    for value in extra:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Claude home must be absolute: {value!r}")
        if path not in roots:
            roots.append(path)
    return roots


def archive_key(root: Path) -> str:
    """Keep same-named files from different homes in separate pool namespaces."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
