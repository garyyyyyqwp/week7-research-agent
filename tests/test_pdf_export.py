"""PDF 导出中文字体回归测试。

背景: Render native runtime 容器没有任何 CJK 系统字体, 且 apt-get 不可用
(非 root)。唯一部署无关的方案是仓库内置字体 static/fonts/ + @font-face
file:// 直接加载。这些测试保证该链路不再被悄悄破坏。
不依赖 weasyprint(本地 Windows 装不上 GTK 也能跑)。
"""

import re
from pathlib import Path

import pytest

from app.routers.report import _detect_cjk_fonts, _md_to_html

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO_ROOT / "static" / "fonts"


def test_bundled_cjk_fonts_exist():
    """仓库必须内置 CJK 字体 — 删掉它们线上 PDF 会立刻变豆腐块。"""
    assert FONTS_DIR.is_dir(), "static/fonts/ 目录不存在"
    files = list(FONTS_DIR.glob("*.otf")) + list(FONTS_DIR.glob("*.ttf")) \
        + list(FONTS_DIR.glob("*.ttc"))
    assert files, "static/fonts/ 下没有任何字体文件"
    # 字体文件必须是真字体, 不是 LFS 指针/错误页 (真字体至少几 MB)
    for f in files:
        assert f.stat().st_size > 1_000_000, f"{f.name} 太小, 可能不是有效字体文件"


def test_bundled_fonts_cover_common_chinese():
    """字体 cmap 必须覆盖常用汉字与全角标点。"""
    fonttools = pytest.importorskip("fontTools.ttLib")
    test_chars = "研究报告智能体中文导出参考文献摘要基于大模型检索与引用，。：？、；「」"
    for f in sorted(FONTS_DIR.glob("*.otf")):
        cmap = fonttools.TTFont(f).getBestCmap()
        missing = [c for c in test_chars if ord(c) not in cmap]
        assert not missing, f"{f.name} 缺字形: {missing}"


def test_detect_prefers_bundled_fonts():
    """检测逻辑必须优先返回仓库内置字体(而非系统字体), 且为 file:// URI。"""
    fonts = _detect_cjk_fonts()
    assert "regular" in fonts, "未检测到 regular 字重"
    assert "bold" in fonts, "未检测到 bold 字重"
    for key in ("regular", "bold"):
        assert fonts[key].startswith("file:///"), fonts[key]
        assert "static/fonts" in fonts[key], (
            f"{key} 未取自内置目录: {fonts[key]}"
        )
    assert "Bold" in fonts["bold"] and "Bold" not in fonts["regular"]


def test_md_to_html_registers_both_weights():
    """生成的 HTML 必须注册 normal+bold 两条 @font-face, body 用 ProjectCJK。"""
    html = _md_to_html("# 中文标题\n\n**加粗** 与 `代码中文`", "研究报告")
    rules = re.findall(r"@font-face\s*\{[^}]*\}", html)
    assert len(rules) == 2, f"应有 2 条 @font-face 规则, 实际 {len(rules)}"
    weights = [re.search(r"font-weight:\s*(\w+)", r).group(1) for r in rules]
    assert weights == ["normal", "bold"], weights
    for r in rules:
        assert "file:///" in r, "字体必须通过 file:// URL 加载"
    # body 与 code 都要有 CJK 字体链
    body_rule = re.search(r"body\s*\{[^}]*\}", html).group(0)
    assert "ProjectCJK" in body_rule
    code_rule = re.search(r"\n  code\s*\{[^}]*\}", html).group(0)
    assert "ProjectCJK" in code_rule, "code 块字体链缺少 CJK 兜底"


def test_md_to_html_content_intact():
    """中文内容原样进入 HTML(UTF-8 未损坏), 表格防断裂包裹存在。"""
    html = _md_to_html(
        "# 深度研究\n\n段落中文内容。\n\n| 列A | 列B |\n|---|---|\n| 甲 | 乙 |",
        "研报《测试》",
    )
    assert "深度研究" in html and "段落中文内容" in html
    assert "甲" in html and "乙" in html
    assert 'charset="UTF-8"' in html
    assert "page-break-inside: avoid" in html
