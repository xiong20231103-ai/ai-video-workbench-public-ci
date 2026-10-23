"""Side-effect-free module-load probe; no installs, .env, browser or state writes."""
import json
from pathlib import Path
import shutil
import subprocess

REQUIRED = ('csv-parse', 'dotenv', 'playwright')


def check_voice_dependencies(root):
    root = Path(root)
    probe = root / 'src' / 'dependencyPreflight.js'
    candidates = (root / 'runtime' / 'node.exe', Path(r'C:\Program Files\nodejs\node.exe'))
    node = next((str(path) for path in candidates if path.is_file()), None) or shutil.which('node')
    failed = {'ok': False, 'code': 'VOICE_DEPENDENCY_CHECK_FAILED', 'missing': []}
    if not node or not probe.is_file():
        return failed
    try:
        result = subprocess.run([node, str(probe)], cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, shell=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(result.stdout) > 4096:
            return failed
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or set(value) != {'ok', 'code', 'missing'}
            or type(value['ok']) is not bool or not isinstance(value['missing'], list)
            or any(name not in REQUIRED for name in value['missing'])
            or len(set(value['missing'])) != len(value['missing'])):
            return failed
        if value['ok']:
            return value if result.returncode == 0 and not value['missing'] and value['code'] == '' else failed
        return value if result.returncode == 1 and value['missing'] and value['code'] == 'VOICE_DEPENDENCIES_MISSING' else failed
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return failed


def dependency_message(result):
    names = '、'.join(name for name in REQUIRED if name in result.get('missing', []))
    return ('【配音启动已阻止】缺失或无法加载依赖：' + names + '。请先恢复配音依赖，再重试。') if names else \
        '【配音启动已阻止】依赖检查未能完成；请检查 Node.js 和预检程序，未启动浏览器。'
