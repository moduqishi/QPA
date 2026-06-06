"""Entry point: python run.py"""

import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import yaml

config_path = Path(__file__).parent / "config.yaml"
if config_path.exists():
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
