"""從 Dropbox 同步券商報告到本地 (不含 LLM 萃取)

職責: 只負責「下載 + 掃描入 DB」，LLM 萃取由 ingest_all.py 負責。

用法:
  python -m scripts.sync_dropbox                # 同步 + 掃描入 DB
  python -m scripts.sync_dropbox --dry-run      # 預覽會下載哪些檔案
  python -m scripts.sync_dropbox --status       # 顯示同步狀態

搭配萃取:
  python -m scripts.sync_dropbox && python -m scripts.ingest_all --limit 50
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

# 清除巢狀 Claude Code 標記
for _key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
    os.environ.pop(_key, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from src.config import CONFIG
from src.ingest import scan_and_register

console = Console()


def _check_rclone():
    """確認 rclone 已安裝且 remote 已設定"""
    try:
        result = subprocess.run(["rclone", "version"], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        console.print("[red]✗ rclone 未安裝。請執行: brew install rclone[/red]")
        sys.exit(1)

    remote_name = CONFIG["dropbox"]["remote"]
    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, check=True)
    remotes = [r.strip().rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]

    if remote_name not in remotes:
        console.print(f"[red]✗ rclone remote '{remote_name}' 未設定。請執行: rclone config[/red]")
        sys.exit(1)

    return True


def _build_rclone_cmd(dry_run: bool = False):
    """組裝 rclone copy 指令"""
    cfg = CONFIG["dropbox"]
    reports_dir = CONFIG["paths"]["reports_dir"]
    remote = cfg["remote"]
    remote_path = cfg["remote_path"]
    excludes = cfg.get("excludes", [])

    src = f"{remote}:{remote_path}"

    cmd = [
        "rclone",
        "copy",
        src,
        reports_dir,
        "--ignore-existing",  # 只下載本地不存在的檔案
        "--no-traverse",  # 大量檔案時加速
        "-v",  # verbose
        "--stats",
        "5s",  # 每 5 秒顯示進度
        "--transfers",
        "4",  # 並行下載數
        "--filter",
        "- .*",  # 排除隱藏檔
        "--filter",
        "+ *.pdf",  # 只要 PDF
        "--filter",
        "+ *.docx",  # 和 DOCX
        "--filter",
        "- *",  # 排除其他
    ]

    for pattern in excludes:
        cmd.extend(["--exclude", pattern])

    if dry_run:
        cmd.append("--dry-run")

    return cmd


def show_status():
    """顯示 Dropbox 同步狀態"""
    _check_rclone()

    cfg = CONFIG["dropbox"]
    remote = cfg["remote"]
    remote_path = cfg["remote_path"]
    reports_dir = CONFIG["paths"]["reports_dir"]
    src = f"{remote}:{remote_path}"

    console.print("\n[bold]Dropbox 同步狀態[/bold]")
    console.print(f"  Remote: {src}")
    console.print(f"  Local:  {reports_dir}\n")

    # 計算遠端檔案數 (PDF + DOCX)
    console.print("[blue]正在查詢 Dropbox...[/blue]")
    result = subprocess.run(
        [
            "rclone",
            "ls",
            src,
            "--filter",
            "+ *.pdf",
            "--filter",
            "+ *.docx",
            "--filter",
            "- *",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]查詢失敗: {result.stderr}[/red]")
        return

    remote_files = [line.strip().split(None, 1)[1] for line in result.stdout.strip().split("\n") if line.strip()]
    remote_count = len(remote_files)

    # 計算本地檔案數
    from pathlib import Path

    local_files = set(f.name for f in Path(reports_dir).glob("*.pdf")) | set(
        f.name for f in Path(reports_dir).glob("*.docx")
    )
    local_count = len(local_files)

    # 比對差異
    remote_names = set(os.path.basename(f) for f in remote_files)
    new_files = remote_names - local_files
    new_count = len(new_files)

    console.print(f"  Dropbox 上:  {remote_count} 份報告")
    console.print(f"  本地:        {local_count} 份報告")

    if new_count > 0:
        console.print(f"  [yellow]待同步:      {new_count} 份新報告[/yellow]\n")
        # 顯示前 20 個新檔案
        for i, name in enumerate(sorted(new_files)[:20]):
            console.print(f"    + {name}")
        if new_count > 20:
            console.print(f"    ... 還有 {new_count - 20} 份")
    else:
        console.print("  [green]✓ 已完全同步[/green]\n")


def sync_from_dropbox(dry_run: bool = False):
    """從 Dropbox 同步報告到本地 + 掃描入 DB（不含 LLM 萃取）"""
    _check_rclone()

    cmd = _build_rclone_cmd(dry_run=dry_run)
    mode = "預覽模式 (dry-run)" if dry_run else "同步中"

    console.print(f"\n[bold blue]📥 Dropbox {mode}[/bold blue]")
    console.print(f"  指令: {' '.join(cmd)}\n")

    start = datetime.now()
    result = subprocess.run(cmd, text=True)

    elapsed = (datetime.now() - start).total_seconds()

    if result.returncode != 0:
        console.print(f"\n[red]✗ rclone 執行失敗 (exit code {result.returncode})[/red]")
        sys.exit(1)

    console.print(f"\n[green]✓ 同步完成 ({elapsed:.1f}s)[/green]")

    if dry_run:
        console.print("[yellow]（dry-run 模式，未實際下載檔案）[/yellow]")
        return 0

    # 掃描新檔案入 DB
    console.print("\n[bold blue]📋 掃描新檔案...[/bold blue]")
    new_count = scan_and_register()

    if new_count > 0:
        console.print(f"\n[yellow]有 {new_count} 份新報告待萃取，請執行:[/yellow]")
        console.print(f"  python -m scripts.ingest_all --limit {new_count}")
    else:
        console.print("[green]沒有新檔案需要處理[/green]")

    return new_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="從 Dropbox 同步券商報告")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式，不實際下載")
    parser.add_argument("--status", action="store_true", help="顯示同步狀態")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        sync_from_dropbox(dry_run=args.dry_run)
