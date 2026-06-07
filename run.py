"""Entry point: python run.py"""

import logging
import sys
from pathlib import Path


# ── Logging configuration ───────────────────────────────────────────
# Set level to DEBUG to see every request and upstream response chunk.
# In production you can change back to INFO.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet down noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _resolve_config_path() -> Path:
    """Locate config.yaml: prefer exe directory, fallback to source directory."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "config.yaml"


# For normal/Docker runs: add the parent of this package dir so `from QPA.xxx` works.
# e.g. run.py is at /QPA/run.py -> parent.parent = / -> /QPA is importable as QPA.
# For PyInstaller: the package is frozen in, skip this.
if not getattr(sys, "frozen", False):
    _root = str(Path(__file__).parent.parent.resolve())
    if _root not in sys.path:
        sys.path.insert(0, _root)

config_path = _resolve_config_path()
if config_path.exists():
    import yaml
    cfg = yaml.safe_load(config_path.read_text()) or {}
    server = cfg.get("server", {})
    host = server.get("host", "0.0.0.0")
    port = server.get("port", 8963)
else:
    host, port = "0.0.0.0", 8963

if __name__ == "__main__":
    import uvicorn
    from QPA.main import app
    # Let uvicorn inherit our logger config
    uvicorn.run(app, host=host, port=port, reload=False,
                log_level="info", access_log=False)
