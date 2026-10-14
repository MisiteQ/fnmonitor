import re, sys
def css(s):
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*([{};,>])\s*', r'\1', s)
    s = re.sub(r'\{([^{}]*)\}', lambda m: '{' + re.sub(r'\s*:\s*', ':', m.group(1)) + '}', s)
    s = s.replace(';}', '}').replace('}', '}\n')
    return s.strip()
def js(s):
    out = []
    in_block = False
    for line in s.split('\n'):
        t = line.strip()
        if in_block:
            if '*/' in t:
                in_block = False
                rest = t.split('*/', 1)[1].strip()
                if rest: out.append(rest)
            continue
        if t.startswith('/*'):
            if '*/' in t:
                rest = t.split('*/', 1)[1].strip()
                if rest: out.append(rest)
            else:
                in_block = True
            continue
        if t.startswith('//') or not t:
            continue
        out.append(t)
    return '\n'.join(out)
mode, src = sys.argv[1], open(sys.argv[2]).read()
sys.stdout.write(css(src) if mode == 'css' else js(src))
