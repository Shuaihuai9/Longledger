"""长衡 LongLedger · Windows 桌面端入口。

运行：python main_app.py

界面层只调用 services（LedgerService / AccountService），
账务与数据库完全由 core / data 层负责，与 Streamlit 版共用同一套后端。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.db import open_database  # noqa: E402
from config.settings import APP_VERSION  # noqa: E402
from desktop.qt import QApplication, QFont, QIcon  # noqa: E402
from desktop.theme import PaperStyle, build_stylesheet  # noqa: E402
from desktop.window import MainWindow  # noqa: E402


def _icon_path() -> Path:
    return PROJECT_ROOT / "assets" / "brand" / "changheng.ico"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("长衡")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("LongLedger")
    icon = _icon_path()
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    # 以 Fusion 为底，再用 PaperStyle 手绘箭头，避免 Windows 原生控件破坏纸面质感
    app.setStyle(PaperStyle("Fusion"))

    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(build_stylesheet())

    conn = open_database()
    window = MainWindow(conn)
    window.show()
    try:
        return app.exec()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
