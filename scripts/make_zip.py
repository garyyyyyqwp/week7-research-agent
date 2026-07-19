"""Create clean submission zip for week7task.

Usage:
    python scripts/make_zip.py
Output:
    <repo>/week7task-clean.zip
"""
import zipfile
import os
from pathlib import Path

# 以脚本位置定位仓库根目录 —— 不要硬编码绝对路径（曾指向 week6task 打错项目）
root = str(Path(__file__).resolve().parent.parent)
output = os.path.join(root, "week7task-clean.zip")

# 排除项：.git 必须排除（本地 git 配置含凭证）；缓存/虚拟环境/运行时数据同理
exclude_dirs = {
    ".git", ".venv", ".claude", "__pycache__", ".pytest_cache",
    "chroma_data", "data", "node_modules",
}
exclude_files = {
    ".env", "week7task.zip", "week7task-clean.zip",
    # 旧版备份/中间文件不进入交付压缩包
    "index_week6_backup.html",
}
# scripts/ 目录只保留交付相关的脚本（不含 Week 6 遗留的 verify_module* / demo_agent）
_script_allowlist = {
    "run_pipeline_demo.py", "make_zip.py", "pack_context.py",
    "verify_week7.py", "__init__.py",
}
exclude_suffixes = {".pyc", ".zip"}
# Dotfiles to include (whitelist)
include_dotfiles = {".env.example", ".gitignore", ".python-version"}

with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = set(rel.replace("\\", "/").split("/"))
        parts.discard(".")
        if parts & exclude_dirs:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in exclude_dirs]
        for fname in filenames:
            if fname.startswith(".") and fname not in include_dotfiles:
                continue
            if fname in exclude_files:
                continue
            if any(fname.endswith(sfx) for sfx in exclude_suffixes):
                continue
            # scripts/ 白名单过滤（不含 Week 6 遗留的 verify_module* / demo_agent）
            if rel == "scripts" or rel.startswith("scripts/"):
                if fname not in _script_allowlist:
                    continue
            full = os.path.join(dirpath, fname)
            arcname = os.path.relpath(full, root).replace("\\", "/")
            zf.write(full, arcname)
            print(f"  + {arcname}")
            count += 1

    print(f"\nTotal: {count} files")

size_mb = os.path.getsize(output) / 1024 / 1024
print(f"Size: {size_mb:.1f} MB  (含 static/fonts/ 内置中文字体 ~17MB，PDF 导出必需)")
print(f"Output: {output}")
