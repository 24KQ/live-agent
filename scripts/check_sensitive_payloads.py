# -*- coding: utf-8 -*-
"Check project for sensitive info leaks."
from __future__ import annotations
import argparse, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def _build_patterns():
    return [
        ("env_path", re.compile(r'(?<!\.example["'"'"'])[\\/]\.env["'"'"')\\s]')),
        ("api_key", re.compile(r'(?i)(api_key|api_secret|app_secret)\s*[=:]\s*["'"'"'][a-zA-Z0-9_\-]{16,}')),
        ("password", re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["'"'"'][^"'"'"]{3,}')),
        ("token", re.compile(r'(?i)(token|access_token|bearer)\s*[=:]\s*["'"'"'][a-zA-Z0-9_\-\.]{20,}')),
        ("user_path", re.compile(r'[Cc]:\\[Uu]sers\\[^\\"'"'"]+')),
        ("private_key", re.compile(r'-----BEGIN\s+(RSA|EC|DSA|PRIVATE)\s+KEY-----')),
        ("conn_pwd", re.compile(r'(?i)(password|pwd)\s*=\s*[^&;\s"'"'"]+')),
    ]
WHITELIST = ['.env.example', 'test_operator_auth', 'test_sensitive']
SUFFIXES = {'.py','.md','.json','.yaml','.yml','.toml','.sql','.html','.js','.env'}
def scan():
    p = argparse.ArgumentParser()
    p.add_argument('paths', nargs='*')
    p.add_argument('--strict', action='store_true')
    a = p.parse_args()
    roots = [Path(x).resolve() for x in a.paths] if a.paths else [ROOT/d for d in ['docs/project_guidance','docs/worklog','docs/superpowers','front','src','tests']]
    found = 0; patterns = _build_patterns()
    for root in roots:
        if not root.exists(): continue
        for f in sorted(root.rglob('*')):
            if not f.is_file() or f.suffix not in SUFFIXES: continue
            if any(w in str(f) for w in WHITELIST): continue
            try: text = f.read_text(encoding='utf-8')
            except: continue
            for ln, line in enumerate(text.splitlines(), 1):
                for cat, pat in patterns:
                    if pat.search(line):
                        try: r = str(f.relative_to(ROOT))
                        except: r = str(f)
                        print(f'[{cat:12s}] {r}:{ln}  {line.strip()[:120]}')
                        found += 1
    if found == 0: print('No sensitive info leaks found.')
    sys.exit(1 if found and a.strict else 0)
if __name__ == '__main__': scan()