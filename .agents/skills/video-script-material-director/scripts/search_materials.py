#!/usr/bin/env python3
"""Thin entry point: deterministic retrieval remains in the selected project."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", required=True, type=Path)
args, rest = parser.parse_known_args()
workspace = args.workspace.resolve()
launcher = workspace / "mixcut"
if not launcher.is_file() or not (workspace / "src/mixcut/material_search.py").is_file():
    parser.error("工作区没有material-search实现；请先接入对应检索模块")
os.execv(str(launcher), [str(launcher), "--workspace", str(workspace), "--json", "material-search", *rest])
