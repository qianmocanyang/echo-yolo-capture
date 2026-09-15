"""echo —— 游戏截图与 YOLO 数据集采集工具。

对应技术方案 v1.1。首版范围：P0 全部 + P1 主体（定时/连拍、批次、审核、
去重标记、标注导入、YOLO 导出），P2（内置标注编辑器、模型预标注）留出扩展点。
"""

__version__ = "1.3.1"

APP_NAME = "echo"
APP_DISPLAY_NAME = "echo"
APP_TAGLINE = "游戏截图与 YOLO 数据集采集"

# 配置结构版本，用于配置迁移；与 config.json 中的 config_version 对应。
CONFIG_VERSION = 1

__all__ = ["__version__", "APP_NAME", "APP_DISPLAY_NAME", "APP_TAGLINE", "CONFIG_VERSION"]
