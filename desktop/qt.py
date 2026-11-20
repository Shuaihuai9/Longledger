"""Qt 绑定适配层：优先 PyQt6，未安装时回退到 API 兼容的 PySide6。

界面代码统一从本模块取 Qt 类型，业务层（core/data/services/ingest）不依赖任何 UI 库。
"""
from __future__ import annotations

try:
    from PyQt6.QtCore import *  # noqa: F401,F403
    from PyQt6.QtCore import pyqtSignal as QtSignal
    from PyQt6.QtGui import *  # noqa: F401,F403
    from PyQt6.QtWidgets import *  # noqa: F401,F403

    QT_BINDING = "PyQt6"
except ImportError:  # pragma: no cover - 仅在本机缺少 PyQt6 时触发
    from PySide6.QtCore import *  # noqa: F401,F403
    from PySide6.QtCore import Signal as QtSignal
    from PySide6.QtGui import *  # noqa: F401,F403
    from PySide6.QtWidgets import *  # noqa: F401,F403

    QT_BINDING = "PySide6"
