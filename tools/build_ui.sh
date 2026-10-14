#!/bin/sh
# ============================================================
# 构建前端单文件 app/www/index.html
# 可读源码在 ui-src/（按文件名顺序拼接），本脚本去掉注释与缩进后
# 合成单文件，并用 node --check 校验脚本语法。
#
# 修改界面请编辑 ui-src/ 下的文件，然后执行：sh tools/build_ui.sh
# 不要直接改 app/www/index.html（下次构建会被覆盖）。
# ui-src/ 不会被打进 fpk 安装包。
# ============================================================
set -e
cd "$(dirname "$0")/.."
S=ui-src
OUT=app/www/index.html
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
{
  cat "$S/00-head.html"
  cat "$S"/1*-*.css > "$TMP/all.css"; python3 tools/minify_ui.py css "$TMP/all.css"; echo
  printf '</style>\n</head>\n<body>\n'
  cat "$S/20-icons.svg" "$S/21-body.html"
  printf '<script>\n(function () {\n%s\n' "'use strict';"
  # 注意顺序：44-toast 需在 40-hero 之前（与原构建一致）
  cat "$S/30-core.js" "$S/31-line.js" "$S/32-bars.js" "$S/33-state.js" "$S/44-toast.js" \
      "$S/40-hero.js" "$S/41-modules.js" "$S/42-modules.js" "$S/43-modules.js" \
      "$S/45-sheets.js" "$S/46-docker.js" "$S/46-vms.js" "$S/47-settings.js" "$S/48-layout.js" "$S/49-app.js" > "$TMP/all.js"
  python3 tools/minify_ui.py js "$TMP/all.js"; echo
  printf '})();\n</script>\n</body>\n</html>\n'
} > "$TMP/index.html"
python3 -c "import sys;t=open(sys.argv[1],encoding='utf-8').read();a=t.rindex('<script>\n')+9;b=t.rindex('</script>');open(sys.argv[2],'w',encoding='utf-8').write(t[a:b])" "$TMP/index.html" "$TMP/check.js"
if command -v node >/dev/null 2>&1; then node --check "$TMP/check.js"; else echo "提示：未安装 node，跳过语法检查"; fi
cp "$TMP/index.html" "$OUT"
wc -c "$OUT"
