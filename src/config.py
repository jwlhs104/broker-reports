"""載入 config.yaml 設定檔"""
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

# 載入專案根目錄的 .env 檔
load_dotenv(Path(__file__).parent.parent / ".env")


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 將相對路徑轉為絕對路徑 (相對於專案根目錄)
    project_root = config_path.parent
    for key in ("reports_dir",):
        p = config["paths"][key]
        if not os.path.isabs(p):
            config["paths"][key] = str(project_root / p)

    if config["paths"]["db_url"].startswith("sqlite:///") and not config["paths"]["db_url"].startswith("sqlite:////"):
        db_file = config["paths"]["db_url"].replace("sqlite:///", "")
        config["paths"]["db_url"] = f"sqlite:///{project_root / db_file}"

    return config


CONFIG = load_config()
