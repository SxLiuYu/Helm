from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

import uvicorn
import yaml

from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Damselfish intelligent model router")
    parser.add_argument("--config", default=os.environ.get("DAMSELFISH_CONFIG", "config.yml"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found: {args.config}")
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"Error: invalid YAML in config: {e}")
        raise SystemExit(1)
    except Exception as e:
        print(f"Error loading config: {e}")
        raise SystemExit(1)
    os.environ["DAMSELFISH_CONFIG"] = args.config

    level = getattr(logging, args.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    # Rotating file handler (10 MB × 3 backups)
    config_dir = Path(args.config).resolve().parent
    log_dir = config_dir / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "damselfish.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=level, handlers=[console, file_handler])

    uvicorn.run(
        "damselfish.app:build_default_app",
        factory=True,
        host=args.host or config.host,
        port=args.port or config.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
