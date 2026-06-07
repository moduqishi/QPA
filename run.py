"""Entry point: python run.py"""

import sys
from pathlib import Path


def _resolve_config_path() -> Path:
    """Locate config.yaml: prefer exe directory, fallback to source directory."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle — exe's directory
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "config.yaml"


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
    uvicorn.run(app, host=host, port=port, reload=False)
