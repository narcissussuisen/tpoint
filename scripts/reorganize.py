#!/usr/bin/env python3
"""\nReorganize tpoint into a maintainable folder structure.\nRun this script from the tpoint directory after stopping the v9 scheduled tasks.\n"""
import os
import sys
import shutil
import re
from pathlib import Path

BASE = Path(r'C:\Users\YZP\WorkBuddy\Claw\tpoint')

if not BASE.exists():
    print(f'Base directory not found: {BASE}')
    sys.exit(1)

os.chdir(BASE)

# ---------------------------------------------------------------------------
# 1. Define target structure: {target_dir: [files to move]}
# ---------------------------------------------------------------------------
STRUCTURE = {
    'config': ['monitor_config.json', 'requirements.txt'],
    'core': [
        'monitor.py',
        'alert_engine.py',
        'indicators.py',
        'exit_manager.py',
        'entry_filter.py',
        'datasource.py',
        'feishu_alert.py',
    ],
    'scripts': ['run_monitor.bat', 'run_engine.bat', 'install_tasks.bat', 'restart.bat'],
    'backtest': [
        'backtest.py',
        'backtest_extended.py',
        'backtest_exit.py',
        'backtest_minute.py',
        'diagnostic.py',
        'compare.py',
        'download_data.py',
        'backtest_data',
    ],
    'docs': ['deploy.md', 'design.md', 'selftest_report.md'],
    'tests': ['selftest.py'],
    'data': ['metrics.json', 'state.json', 'signal.txt'],
}

# Files to delete
TO_DELETE = ['monitor.py.bak', '__pycache__']

# Log files currently at root that should be moved to logs/
ROOT_LOGS = ['monitor_console.log', 'engine_crash.log', 'monitor_crash.log', 'monitor_lifecycle.log']

# ---------------------------------------------------------------------------
# 2. Create directories
# ---------------------------------------------------------------------------
for d in list(STRUCTURE.keys()) + ['logs']:
    (BASE / d).mkdir(exist_ok=True)
    print(f'[mkdir] {d}/')

# ---------------------------------------------------------------------------
# 3. Move files and directories
# ---------------------------------------------------------------------------
for d, items in STRUCTURE.items():
    for item in items:
        src = BASE / item
        dst = BASE / d / item
        if not src.exists():
            print(f'[skip] {item} does not exist')
            continue
        if dst.exists():
            print(f'[skip] {dst} already exists')
            continue
        shutil.move(str(src), str(dst))
        print(f'[move] {item} -> {d}/')

# Move logs already in logs/ are fine; move any remaining root logs to logs/
for log in ROOT_LOGS:
    src = BASE / log
    dst = BASE / 'logs' / log
    if src.exists() and not dst.exists():
        shutil.move(str(src), str(dst))
        print(f'[move] {log} -> logs/')

# ---------------------------------------------------------------------------
# 4. Delete obsolete files
# ---------------------------------------------------------------------------
for item in TO_DELETE:
    target = BASE / item
    if target.is_file():
        target.unlink()
        print(f'[delete] {item}')
    elif target.is_dir():
        shutil.rmtree(target)
        print(f'[delete dir] {item}')

# ---------------------------------------------------------------------------
# 5. Patch Python source paths
# ---------------------------------------------------------------------------

def patch_text(path, replacements):
    text = path.read_text(encoding='utf-8')
    original = text
    for old, new in replacements:
        text = text.replace(old, new)
    if text != original:
        path.write_text(text, encoding='utf-8')
        print(f'[patch] {path.name}')
    else:
        print(f'[noop] {path.name}')

# core/monitor.py: BASE_DIR points to tpoint root; data/logs under subfolders
patch_text(
    BASE / 'core' / 'monitor.py',
    [
        ('BASE_DIR = os.path.dirname(os.path.abspath(__file__))',
         'BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))'),
        ("os.path.join(BASE_DIR, 'signal.txt')", "os.path.join(BASE_DIR, 'data', 'signal.txt')"),
        ("os.path.join(BASE_DIR, 'state.json')", "os.path.join(BASE_DIR, 'data', 'state.json')"),
        ("os.path.join(BASE_DIR, 'metrics.json')", "os.path.join(BASE_DIR, 'data', 'metrics.json')"),
        ("os.path.join(BASE_DIR, 'monitor_lifecycle.log')", "os.path.join(BASE_DIR, 'logs', 'monitor_lifecycle.log')"),
        ("os.path.join(BASE_DIR, 'monitor_fatal.log')", "os.path.join(BASE_DIR, 'logs', 'monitor_fatal.log')"),
    ]
)

# core/alert_engine.py: BASE_DIR points to tpoint root; config/data under subfolders
patch_text(
    BASE / 'core' / 'alert_engine.py',
    [
        ('BASE_DIR = os.path.dirname(os.path.abspath(__file__))',
         'BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))'),
        ("os.path.join(BASE_DIR, 'monitor_config.json')", "os.path.join(BASE_DIR, 'config', 'monitor_config.json')"),
        ("os.path.join(BASE_DIR, m.get('metrics_file', 'metrics.json'))",
         "os.path.join(BASE_DIR, 'data', m.get('metrics_file', 'metrics.json'))"),
    ]
)

# ---------------------------------------------------------------------------
# 6. Patch .bat files
# ---------------------------------------------------------------------------

# scripts/run_monitor.bat (handles both quoted and unquoted forms)
patch_text(
    BASE / 'scripts' / 'run_monitor.bat',
    [
        ('"monitor.py"', '"core\\monitor.py"'),
        (' monitor.py ', ' core\\monitor.py '),
        ('logs\\logs\\monitor_crash.log', 'logs\\monitor_crash.log'),
        ('monitor_crash.log', 'logs\\monitor_crash.log'),
    ]
)

# scripts/run_engine.bat: already has cd /d; just update paths
patch_text(
    BASE / 'scripts' / 'run_engine.bat',
    [
        ('"alert_engine.py"', '"core\\alert_engine.py"'),
        (' alert_engine.py ', ' core\\alert_engine.py '),
        ('logs\\logs\\engine_crash.log', 'logs\\engine_crash.log'),
        ('engine_crash.log', 'logs\\engine_crash.log'),
    ]
)

# ---------------------------------------------------------------------------
# 7. Patch backtest / test scripts to import from core/
# ---------------------------------------------------------------------------
for script in [
    'backtest/backtest.py',
    'backtest/backtest_extended.py',
    'backtest/backtest_exit.py',
    'backtest/backtest_minute.py',
    'backtest/diagnostic.py',
    'backtest/compare.py',
    'tests/selftest.py',
]:
    path = BASE / script
    if path.exists():
        patch_text(
            path,
            [
                ("sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
                 "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))"),
            ]
        )

# Special fix for download_data.py: it imports from tickflow, which is actually datasource
patch_text(
    BASE / 'backtest' / 'download_data.py',
    [
        ("from tickflow import TickFlow", "from datasource import MootdxDataSource as TickFlow"),
    ]
)

# ---------------------------------------------------------------------------
# 8. Create README files
# ---------------------------------------------------------------------------
READMES = {
    'config': 'Configuration files for v9 monitor and alert engine.',
    'core': 'Core Python modules: monitor, alert engine, indicators, exit manager, entry filter, datasource.',
    'scripts': 'Batch scripts to run services, install scheduled tasks, and restart services.',
    'backtest': 'Backtesting scripts and historical data for v9 strategy validation.',
    'docs': 'Design and deployment documentation.',
    'tests': 'Unit tests and self-validation scripts.',
    'data': 'Runtime data files: metrics, state, and generated signals.',
    'logs': 'Log files generated by monitor and alert engine.',
}

for d, desc in READMES.items():
    path = BASE / d / 'README.md'
    if not path.exists():
        path.write_text(f'# {d.capitalize()}\n\n{desc}\n', encoding='utf-8')
        print(f'[readme] {d}/README.md')

# ---------------------------------------------------------------------------
# 9. Create top-level README
# ---------------------------------------------------------------------------
top_readme = BASE / 'README.md'
if not top_readme.exists():
    top_readme.write_text(
        '# tpoint - v9-miji T+0 monitor\n\n'
        'Minute-level A-share T+0 trading strategy monitor with trailing stop-loss.\n\n'
        '- `core/`      : core Python modules\n'
        '- `config/`    : configuration files\n'
        '- `scripts/`   : startup and maintenance batch scripts\n'
        '- `backtest/`  : backtesting tools\n'
        '- `tests/`     : self-tests\n'
        '- `docs/`      : documentation\n'
        '- `data/`      : runtime data files\n'
        '- `logs/`      : log files\n'
        '- `venv/`      : Python virtual environment\n\n'
        'Quick start (administrator):\n'
        '  scripts\\install_tasks.bat   # register SYSTEM scheduled tasks\n'
        '  scripts\\restart.bat       # restart services\n',
        encoding='utf-8'
    )
    print('[readme] README.md')

# ---------------------------------------------------------------------------
# 10. Compile check
# ---------------------------------------------------------------------------
print('\n[compile check]')
for script in ['core/monitor.py', 'core/alert_engine.py', 'core/indicators.py',
               'core/exit_manager.py', 'core/entry_filter.py', 'core/datasource.py',
               'core/feishu_alert.py', 'tests/selftest.py']:
    path = BASE / script
    if path.exists():
        ret = os.system(f'"{BASE / "venv" / "Scripts" / "python.exe"}" -m py_compile "{path}"')
        if ret == 0:
            print(f'  OK  {script}')
        else:
            print(f'  FAIL {script}')

print('\nReorganization complete.')
