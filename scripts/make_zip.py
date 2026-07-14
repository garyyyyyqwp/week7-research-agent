"""Create clean submission zip."""
import zipfile, os

root = r"C:\Users\35452\Desktop\week6task"
output = os.path.join(root, "week6task-clean.zip")
exclude_dirs = {".git", ".venv", ".claude", "__pycache__", "chroma_data"}
exclude_files = {".env", "week6task.zip", "week6task-clean.zip"}
# Dotfiles to include (whitelist)
include_dotfiles = {".env.example", ".gitignore"}

with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        # Skip hidden dirs and exclude dirs
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
            full = os.path.join(dirpath, fname)
            arcname = os.path.relpath(full, root).replace("\\", "/")
            zf.write(full, arcname)
            print(f"  + {arcname}")
            count += 1

    print(f"\nTotal: {count} files")

size_kb = os.path.getsize(output) / 1024
print(f"Size: {size_kb:.1f} KB")
print(f"Output: {output}")
