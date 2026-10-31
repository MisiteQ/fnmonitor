import os, sys, glob, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "ui-src")
OUT = os.path.join(ROOT, "app", "www", "index.html")
MINIFY = os.path.join(ROOT, "tools", "minify_ui.py")
PY = sys.executable

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

def minify(mode, src_path):
    r = subprocess.run([PY, MINIFY, mode, src_path], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr); sys.exit(1)
    return r.stdout

import tempfile
TMP = tempfile.mkdtemp()

parts = []
parts.append(read(os.path.join(S, "00-head.html")))

# CSS: 1*-*.css in order
css_files = sorted(glob.glob(os.path.join(S, "1*-*.css")))
css_all = os.path.join(TMP, "all.css")
with open(css_all, "w", encoding="utf-8") as f:
    for cf in css_files:
        f.write(read(cf))
parts.append(minify("css", css_all) + "\n")

parts.append("</style>\n</head>\n<body>\n")
parts.append(read(os.path.join(S, "20-icons.svg")))
parts.append(read(os.path.join(S, "21-body.html")))
parts.append("<script>\n(function () {\n'use strict';\n")

js_order = [
    "30-core.js", "31-line.js", "32-bars.js", "33-state.js", "44-toast.js",
    "40-hero.js", "41-modules.js", "42-modules.js", "43-modules.js",
    "45-sheets.js", "46-docker.js", "46-vms.js", "47-settings.js",
    "48-layout.js", "49-app.js",
]
js_all = os.path.join(TMP, "all.js")
with open(js_all, "w", encoding="utf-8") as f:
    for jf in js_order:
        f.write(read(os.path.join(S, jf)))
parts.append(minify("js", js_all) + "\n")

parts.append("})();\n</script>\n</body>\n</html>\n")

html = "".join(parts)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)

print("built:", OUT, len(html), "bytes")
