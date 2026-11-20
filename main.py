"""长衡 LongLedger 启动入口。

用法：
    streamlit run app/streamlit_app.py
或：
    python main.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent / "app" / "streamlit_app.py"


def main() -> int:
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(APP)])


if __name__ == "__main__":
    raise SystemExit(main())
