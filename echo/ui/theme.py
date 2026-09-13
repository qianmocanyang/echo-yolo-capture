"""视觉设计令牌与 QSS。

方案 §7.1 给了完整的取值，这里把它落成唯一的源头：颜色、圆角、间距、字号
都只在这个文件里定义一次，界面代码一律引用令牌，不再写死颜色。
这样"改一处，全局一致"，也避免出现同一层灰色在不同控件上深浅不一。

两个容易踩的坑，在实现里已经规避：

1. **QSS 没有变量**。所以 QSS 是由 :func:`build_qss` 用令牌格式化出来的字符串，
   而不是一个静态 .qss 文件。
2. **`padding` 与 `min-height` 会打架**。按钮只设 `min-height` 而带 padding 时，
   Qt 会把内容区再撑高，导致同一行的按钮和输入框对不齐。
   所以按钮统一用 `height` + `padding: 0 14px` 的写法。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    """方案 §7.1 的色板，加几个派生色。"""

    # ---- 方案给定 ----
    bg: str = "#111318"
    card: str = "#1B1F27"
    input: str = "#242A35"
    border: str = "#343C49"
    text: str = "#F3F5F8"
    text_dim: str = "#A7B0BF"
    accent: str = "#8B9CFF"
    echo: str = "#8B95A5"

    # ---- 派生态 ----
    card_alt: str = "#161A21"        # 卡片内嵌区域，比卡片再深一点
    card_hover: str = "#21262F"
    border_soft: str = "#272E39"
    text_faint: str = "#6C7686"
    on_accent: str = "#141821"       # 强调色按钮上的深色文字（方案要求）
    accent_hover: str = "#9EAEFF"
    accent_pressed: str = "#7A8CFF"
    accent_soft: str = "rgba(139, 156, 255, 0.14)"
    danger: str = "#FF7A7A"
    danger_soft: str = "rgba(255, 122, 122, 0.14)"
    warn: str = "#FFC46B"
    warn_soft: str = "rgba(255, 196, 107, 0.14)"
    ok: str = "#6FD38B"
    ok_soft: str = "rgba(111, 211, 139, 0.14)"
    info: str = "#79B8FF"
    info_soft: str = "rgba(121, 184, 255, 0.14)"
    selection: str = "rgba(139, 156, 255, 0.22)"
    shadow: str = "rgba(0, 0, 0, 0.35)"
    overlay: str = "rgba(8, 10, 14, 0.72)"


@dataclass(frozen=True, slots=True)
class Metrics:
    """方案 §7.1 的圆角、间距与字号。"""

    radius_card: int = 12
    radius_control: int = 8
    radius_pill: int = 999
    gap: int = 8                     # 8 px 基础网格
    card_padding: int = 20           # 方案要求 20～24
    card_padding_wide: int = 24
    sidebar_width: int = 196
    sidebar_width_compact: int = 68
    settings_width: int = 344
    topbar_height: int = 56
    statusbar_height: int = 32
    thumb_height: int = 104
    min_window = (900, 640)          # 方案 §7.1 小窗口适配下限
    default_window = (1120, 760)

    font_family: str = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
    font_mono: str = '"Cascadia Mono", "Consolas", monospace'
    size_body: int = 13
    size_small: int = 12
    size_tiny: int = 11
    size_title: int = 18
    size_display: int = 22
    duration_fast: int = 120         # 方案 §7.1 动效 120～180 ms
    duration_normal: int = 180


DARK = Palette()
METRICS = Metrics()


def build_qss(palette: Palette = DARK, metrics: Metrics = METRICS) -> str:
    """把令牌格式化成 QSS。"""
    p, m = palette, metrics
    return f"""
/* ===================== 全局 ===================== */
* {{
    font-family: {m.font_family};
    font-size: {m.size_body}px;
    outline: none;
}}
QWidget {{
    color: {p.text};
    background: transparent;
}}
QMainWindow, #RootSurface {{
    background: {p.bg};
}}
QToolTip {{
    background: {p.card};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: {m.radius_control}px;
    padding: 6px 10px;
    font-size: {m.size_small}px;
}}

/* ===================== 文本层级 ===================== */
#PageTitle   {{ font-size: {m.size_display}px; font-weight: 600; }}
#CardTitle   {{ font-size: {m.size_body + 1}px; font-weight: 600; }}
#Caption     {{ font-size: {m.size_small}px; color: {p.text_dim}; }}
#Hint        {{ font-size: {m.size_small}px; color: {p.text_faint}; }}
#Mono        {{ font-family: {m.font_mono}; font-size: {m.size_small}px; color: {p.text_dim}; }}
#FieldLabel  {{ font-size: {m.size_small}px; color: {p.text_dim}; }}
#EchoMark    {{ font-size: {m.size_tiny}px; color: {p.echo}; letter-spacing: 2px; }}
.dim         {{ color: {p.text_dim}; }}
.faint       {{ color: {p.text_faint}; }}

/* ===================== 卡片 ===================== */
#Card {{
    background: {p.card};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_card}px;
}}
#CardFlat {{
    background: {p.card_alt};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
}}
#Card:hover {{ border-color: {p.border}; }}
#Divider {{
    background: {p.border_soft};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

/* ===================== 按钮 ===================== */
QPushButton {{
    background: {p.input};
    color: {p.text};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
    padding: 0 14px;
    height: 34px;
    font-size: {m.size_small}px;
}}
QPushButton:hover  {{ background: {p.card_hover}; border-color: {p.border}; }}
QPushButton:pressed{{ background: {p.input}; }}
QPushButton:disabled {{
    color: {p.text_faint};
    background: {p.card_alt};
    border-color: {p.border_soft};
}}
QPushButton[variant="primary"] {{
    background: {p.accent};
    color: {p.on_accent};
    border: none;
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover   {{ background: {p.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {p.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {p.border}; color: {p.text_faint}; }}
QPushButton[variant="danger"] {{ color: {p.danger}; }}
QPushButton[variant="danger"]:hover {{ background: {p.danger_soft}; }}
QPushButton[variant="ghost"] {{
    background: transparent;
    border-color: transparent;
    color: {p.text_dim};
}}
QPushButton[variant="ghost"]:hover {{ background: {p.card_hover}; color: {p.text}; }}
QPushButton[variant="nav"] {{
    background: transparent;
    border: none;
    border-radius: {m.radius_control}px;
    color: {p.text_dim};
    height: 40px;
    padding: 0 12px;
    text-align: left;
    font-size: {m.size_small}px;
}}
QPushButton[variant="nav"]:hover   {{ background: {p.card_hover}; color: {p.text}; }}
QPushButton[variant="nav"]:checked {{
    background: {p.accent_soft};
    color: {p.accent};
    font-weight: 600;
}}
QPushButton[size="large"] {{ height: 38px; padding: 0 18px; }}

/* ===================== 输入 ===================== */
QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p.input};
    color: {p.text};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
    padding: 0 10px;
    height: 32px;
    selection-background-color: {p.selection};
}}
QPlainTextEdit, QTextEdit {{ padding: 8px 10px; }}
QLineEdit:hover, QSpinBox:hover {{ border-color: {p.border}; }}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QSpinBox:disabled {{
    color: {p.text_faint};
    background: {p.card_alt};
}}
QLineEdit[state="error"], QSpinBox[state="error"] {{ border-color: {p.danger}; }}
QLineEdit[readOnly="true"] {{ color: {p.text_dim}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

QComboBox {{
    background: {p.input};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
    padding: 0 10px;
    height: 32px;
}}
QComboBox:hover {{ border-color: {p.border}; }}
QComboBox:focus, QComboBox:on {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {p.card};
    border: 1px solid {p.border};
    border-radius: {m.radius_control}px;
    padding: 4px;
    selection-background-color: {p.card_hover};
    selection-color: {p.text};
    outline: none;
}}

/* ===================== 勾选与开关 ===================== */
QCheckBox, QRadioButton {{ spacing: 8px; color: {p.text_dim}; font-size: {m.size_small}px; }}
QCheckBox:hover, QRadioButton:hover {{ color: {p.text}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.border};
    border-radius: 4px;
    background: {p.input};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
    image: none;
}}
QRadioButton::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background: {p.card_alt}; border-color: {p.border_soft};
}}

/* ===================== 滚动区 ===================== */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {p.border}; border-radius: 4px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {p.border}; border-radius: 4px; min-width: 32px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===================== 列表 / 表格 ===================== */
QListWidget, QTreeWidget, QTableWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: {m.radius_control}px;
    padding: 6px 8px;
    color: {p.text_dim};
}}
QListWidget::item:hover {{ background: {p.card_hover}; color: {p.text}; }}
QListWidget::item:selected {{ background: {p.accent_soft}; color: {p.accent}; }}
QHeaderView::section {{
    background: transparent;
    color: {p.text_faint};
    border: none;
    border-bottom: 1px solid {p.border_soft};
    padding: 6px 8px;
    font-size: {m.size_tiny}px;
}}
QTableWidget::item {{ padding: 4px 8px; }}
QTableWidget::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}

/* ===================== 进度条 ===================== */
QProgressBar {{
    background: {p.input};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

/* ===================== 菜单 ===================== */
QMenu {{
    background: {p.card};
    border: 1px solid {p.border};
    border-radius: {m.radius_control}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 22px 7px 12px;
    border-radius: 6px;
    color: {p.text_dim};
    font-size: {m.size_small}px;
}}
QMenu::item:selected {{ background: {p.card_hover}; color: {p.text}; }}
QMenu::item:disabled {{ color: {p.text_faint}; }}
QMenu::separator {{ height: 1px; background: {p.border_soft}; margin: 5px 8px; }}

/* ===================== 分隔条 ===================== */
QSplitter::handle {{ background: transparent; width: 1px; }}

/* ===================== 自绘控件补充 ===================== */
#StatePill {{
    border-radius: {m.radius_pill}px;
    padding: 3px 12px;
    font-size: {m.size_small}px;
}}
#PreviewSurface {{
    background: {p.card_alt};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
}}
#ThumbCell {{
    background: {p.card_alt};
    border: 1px solid {p.border_soft};
    border-radius: {m.radius_control}px;
}}
#ThumbCell:hover {{ border-color: {p.accent}; }}
#ThumbCell[selected="true"] {{ border-color: {p.accent}; border-width: 2px; }}
#Sidebar {{ background: {p.card_alt}; border-right: 1px solid {p.border_soft}; }}
#TopBar   {{ background: {p.bg}; border-bottom: 1px solid {p.border_soft}; }}
#StatusBar {{ background: {p.bg}; border-top: 1px solid {p.border_soft}; }}
#StickyBar {{ background: {p.card}; border-top: 1px solid {p.border_soft}; }}
"""


def status_colors(state: str, palette: Palette = DARK) -> tuple[str, str]:
    """状态 → (前景色, 背景色)。

    方案 §7.1 要求「状态用文字加图标表达，不只依靠颜色」，
    所以这里的颜色只是辅助，界面上永远同时给出状态文字。
    """
    mapping = {
        "unconfigured": (palette.text_dim, palette.card_hover),
        "idle": (palette.text_dim, palette.card_hover),
        "capturing": (palette.accent, palette.accent_soft),
        "paused": (palette.warn, palette.warn_soft),
        "waiting": (palette.warn, palette.warn_soft),
        "save_error": (palette.danger, palette.danger_soft),
    }
    return mapping.get(state, (palette.text_dim, palette.card_hover))


def level_colors(level: str, palette: Palette = DARK) -> tuple[str, str]:
    mapping = {
        "info": (palette.info, palette.info_soft),
        "warn": (palette.warn, palette.warn_soft),
        "error": (palette.danger, palette.danger_soft),
        "ok": (palette.ok, palette.ok_soft),
    }
    return mapping.get(level, (palette.text_dim, palette.card_hover))
