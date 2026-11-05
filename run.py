#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SettleTrace 统一入口。

为什么需要这个文件：项目按 `src/` 布局组织，包目录是 `src/app`。
直接跑 `uvicorn app.main:app` 会报 `ModuleNotFoundError: No module named 'app'`，
因为 `src` 不在 `sys.path` 里。有两种常见解法：

  1. `uvicorn app.main:app --app-dir src`（要记住参数）
  2. `set PYTHONPATH=src`（Windows 下每次都要设）

这个入口把两件事都封装了，跨平台、不用记参数：

    python run.py                    # 启动服务（默认 8000，带 --reload）
    python run.py serve --port 8100  # 换端口
    python run.py serve --no-reload  # 生产模式（关闭热重载）
    python run.py check_db           # 数据库自检（8 步）
    python run.py migrate            # 后台表 + 提示词 v1 + 回填切片
    python run.py migrate-agent      # 智能体域 10 张表 + 编排补丁
    python run.py seed [--reset]     # 灌演示单据
    python run.py ingest --corpus docs/requirements/discovery-report.md
    python run.py rollback --list    # 回滚（另见 --to / --dry-run）
    python run.py eval --category violation   # 跑评测

设计的克制：这里只做「路径注入 + 参数透传」，不复制任何业务逻辑。
子命令实际执行的是 `app.*` 模块的 `__main__`，和 `python -m app.xxx` 完全等价。
"""
from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

# 让 `import app...` 能找到包。放在最前面，子进程不需要再设环境变量。
sys.path.insert(0, str(SRC))

# 子命令 → 模块。新增 CLI 工具时只改这里。
MODULES = {
    "check_db": "app.check_db",
    "migrate": "app.admin_migrate",
    "migrate-agent": "app.agent_migrate",
    "seed": "app.agent_seed",
    "ingest": "app.ingest",
    "rollback": "app.rollback",
}


def _parse_serve(argv: list[str]) -> tuple[str, str, bool]:
    host, port, reload = "127.0.0.1", "8000", True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--host", "-h") and i + 1 < len(argv):
            host = argv[i + 1]; i += 2
        elif a in ("--port", "-p") and i + 1 < len(argv):
            port = argv[i + 1]; i += 2
        elif a == "--no-reload":
            reload = False; i += 1
        elif a == "--reload":
            reload = True; i += 1
        elif a.isdigit():
            port = a; i += 1
        else:
            i += 1
    return host, port, reload


def serve(argv: list[str]) -> int:
    """启动服务。

    用子进程而不是 uvicorn.run()：--reload 会 fork 出子进程重新 import 应用，
    而新进程不会继承我们注入到 sys.path 的内容；带上 --app-dir 才稳。
    """
    host, port, reload = _parse_serve(argv)
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
           "--app-dir", str(SRC), "--host", host, "--port", port]
    if reload:
        cmd.append("--reload")
    print(f"[SettleTrace] http://{host}:{port}  （Ctrl+C 停止）")
    try:
        return subprocess.run(cmd, cwd=str(ROOT)).returncode
    except KeyboardInterrupt:
        return 0


def run_module(name: str, argv: list[str]) -> int:
    """把参数原样交给模块的 __main__，行为等价于 python -m。"""
    mod = MODULES[name]
    sys.argv = [mod] + argv
    try:
        runpy.run_module(mod, run_name="__main__")
    except SystemExit as e:                 # argparse 正常退出
        return int(e.code or 0)
    return 0


def run_eval(argv: list[str]) -> int:
    """评测执行器在 tests/eval 下，不属于 app 包，单独调。"""
    script = ROOT / "tests" / "eval" / "run_eval.py"
    if not script.is_file():
        print(f"找不到评测脚本：{script}")
        return 1
    return subprocess.run([sys.executable, str(script)] + argv,
                          cwd=str(ROOT)).returncode


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return serve([])

    cmd, rest = args[0], args[1:]
    if cmd in ("serve", "run"):
        return serve(rest)
    if cmd in MODULES:
        return run_module(cmd, rest)
    if cmd == "eval":
        return run_eval(rest)
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    print(f"未知命令：{cmd}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
