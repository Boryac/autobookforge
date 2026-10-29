# tools

## patch.py

幂等补丁执行器：读全文 -> 精确替换 -> 写回，逐项报告 `applied` / `already` / `NOTFOUND`。

```bash
python tools/patch.py patches.json
```

`patches.json` 格式：

```json
[{"name": "说明", "file": "相对项目根的路径", "old": "原文", "new": "新文"}]
```

用途：在批量修改时逐项确认落盘结果，避免「工具返回成功但磁盘没变」的静默失败。

## uiaudit.mjs

多视口界面审查。走 CDP 直连 headless Chrome，**测量真实几何**（`getBoundingClientRect` /
`scrollHeight`），而不是只看截图判断。

```bash
node tools/uiaudit.mjs http://127.0.0.1:8000/ ./out
```

检查项：

- 每张磁贴是否被裁切（`scrollHeight - clientHeight`，且 `overflow` 非 visible）
- 页面横向溢出元素
- 16 个关键控件是否可达（相对滚动容器）
- 输出 `out/_uiaudit.txt` 与各视口整页截图

默认视口：1600x1000 / 1366x768 / 1280x800。

## uicheck.mjs

单视口精细检查，额外输出右栏 2 倍放大截图，便于看细节。

```bash
node tools/uicheck.mjs http://127.0.0.1:8000/ ./out 1600 1000
```

输出：`_uicheck.json`（完整几何）、`_uicheck-full.png`、`_uicheck-side.png`、
`_uicheck-side-bottom.png`（滚到底部，用于验证粘性定位）。

两个脚本均零依赖（Node 内置 `WebSocket` + `fetch`），无需 puppeteer。
Windows 下若 Chrome 不在默认路径，用 `CHROME=<path>` 覆盖。
