# -*- coding: utf-8 -*-
"""幂等补丁执行器：读全文 -> 精确替换 -> 写回，逐项报告 applied/already/NOTFOUND。

用法: python _abf_patch.py <patch.json>
patch.json 格式: [{"file": "相对或绝对路径", "old": "...", "new": "...", "name": "..."}, ...]
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def apply_one(p):
    path = p["file"]
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    if not os.path.exists(path):
        return "NOTFOUND(file) %s :: %s" % (p.get("name", ""), path)
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    old, new = p["old"], p["new"]
    has_old = old in text
    has_new = new in text
    if has_old:
        text = text.replace(old, new, 1)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return "applied   %s" % p.get("name", "")
    if has_new:
        return "already   %s" % p.get("name", "")
    return "NOTFOUND  %s" % p.get("name", "")


def main():
    spec = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_abf_patch.json")
    with io.open(spec, "r", encoding="utf-8") as fh:
        patches = json.load(fh)
    lines = []
    for p in patches:
        try:
            lines.append(apply_one(p))
        except Exception as e:
            lines.append("ERROR     %s :: %r" % (p.get("name", ""), e))
    out = os.path.join(ROOT, "_abf_patch_out.txt")
    with io.open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))


main()
