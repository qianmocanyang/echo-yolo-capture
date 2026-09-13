"""静态属性自检：找出 ``self.xxx`` 被访问、但在类定义与 ``__init__`` 里都没有定义的成员。

这类遗漏（例如 ``self._drain_notices``）语法合法、导入也通过，只有在运行时
真正走到那一行才会炸。手动逐个跑太慢，这里用 AST 一次性扫全。

带基类的类会有大量合法的继承成员（``CaptureBackend._source``、``QWidget.raise_``），
因此做两层处理：跨文件解析继承链合并基类成员，再对 Qt/Python 内置成员留一份
低误报白名单。本项目自有成员统一是 snake_case 或以下划线开头，而 Qt 侧要么是
驼峰（setStyleSheet）、要么是常见小写单词（show / resize）。

用法：
    python tools/check_attrs.py            # 只报告，退出码恒为 0，结果供人工核对
    python tools/check_attrs.py --strict   # 发现可疑成员或语法错误时退出码 1，供 CI 当门禁
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Qt / Python 内置基类上合法的小写成员名，避免误报
QT_ALLOWED = {
    # QObject / QWidget
    "show", "hide", "close", "update", "resize", "move", "raise", "lower",
    "repaint", "accept", "reject", "deleteLater", "parent", "children",
    "isVisible", "isHidden", "isEnabled", "devices", "screens", "size",
    "pos", "rect", "geometry", "width", "height", "x", "y", "focus",
    "font", "palette", "window", "screen", "mask", "cursor", "layout",
    # 习惯用法：Python 侧下划线版本 + Qt 侧同名方法
    "raise_", "style", "palette_", "rect_",
    # QSystemTrayIcon / 信号
    "activated", "messageClicked", "triggered", "toggled", "clicked",
    # 惯例属性
    "tr", "setParent", "metaObject", "objectName", "signals", "slots",
    # 常见第三方
    "shape", "dtype", "size_", "flags", "index", "value", "text", "data",
}

# 明确跳过的文件
SKIP = {"tools/check_attrs.py"}


def defined_members(node: ast.ClassDef) -> set[str]:
    """类里定义的方法名、类属性名，以及任何方法体内 ``self.x = ...`` 的 x。"""
    names: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(item.name)
            for deco in item.decorator_list:
                if isinstance(deco, ast.Attribute):
                    names.add(deco.attr)
                elif isinstance(deco, ast.Name):
                    # @property / @staticmethod / @abstractmethod
                    names.add(f"__deco__{deco.id}")
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)

    # 任意方法体内 self.x = ... / for self.x in ... / with ... as self.x
    for sub in ast.walk(node):
        targets: list[ast.expr] = []
        if isinstance(sub, ast.Assign):
            targets = list(sub.targets)
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            targets = [sub.target]
        elif isinstance(sub, (ast.For, ast.AsyncFor)):
            targets = [sub.target]
        elif isinstance(sub, ast.withitem):
            targets = [sub.optional_vars] if sub.optional_vars else []
        for target in targets:
            for inner in ast.walk(target):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self"
                ):
                    names.add(inner.attr)
    return names


def base_names(node: ast.ClassDef) -> list[str]:
    out: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def build_index(trees: dict[Path, ast.Module]) -> dict[str, tuple[set[str], list[str]]]:
    """{类名: (自有成员, 基类名)}，供跨文件继承合并使用。"""
    index: dict[str, tuple[set[str], list[str]]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            members = defined_members(node)
            bases = base_names(node)
            old_members, old_bases = index.get(node.name, (set(), []))
            index[node.name] = (old_members | members, old_bases + bases)
    return index


def resolve(name: str, index: dict[str, tuple[set[str], list[str]]], seen: set[str] | None = None) -> set[str]:
    """递归合并继承链上的成员（项目内同名类取并集）。"""
    seen = seen or set()
    if name in seen:
        return set()
    seen.add(name)
    members, bases = index.get(name, (set(), []))
    out = set(members)
    for base in bases:
        out |= resolve(base, index, seen)
    return out


def main() -> int:
    strict = "--strict" in sys.argv

    paths = [p for p in sorted((ROOT / "echo").rglob("*.py"))
             if p.relative_to(ROOT).as_posix() not in SKIP]

    trees: dict[Path, ast.Module] = {}
    syntax_errors = 0
    for path in paths:
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            syntax_errors += 1
            print(f"{path.relative_to(ROOT)}: 语法错误 {exc}")

    index = build_index(trees)

    total = 0
    for path, tree in trees.items():
        rel = path.relative_to(ROOT).as_posix()
        found: dict[tuple[str, str], list[int]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            known = resolve(node.name, index)
            for sub in ast.walk(node):
                if not (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                ):
                    continue
                name = sub.attr
                if name in known or name in QT_ALLOWED:
                    continue
                # 只关心「像本项目自有成员」的命名
                if "_" not in name and not name.islower():
                    continue
                found.setdefault((node.name, name), []).append(sub.lineno)

        if found:
            print(f"\n{rel}")
            for (cls, name), lines in sorted(found.items()):
                total += 1
                print(f"  {cls}.{name}  (行 {', '.join(map(str, sorted(lines)))})")

    print(f"\n可疑成员合计 {total} 处")
    if total:
        print("说明：仍可能有 Qt 侧误报，请逐条核对后再修。")
    if syntax_errors:
        print(f"另有 {syntax_errors} 个文件存在语法错误。")
    if strict and (total or syntax_errors):
        print("--strict：发现问题，退出码 1。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
