"""
Корневой conftest.py — выполняется ПЕРВЫМ (до tests/conftest.py).

Задачи:
  1. Добавить корень проекта в sys.path ПЕРЕД любым импортом
     (чтобы `from core.state_machine import ...` работал)
  2. Выставить env vars для config.py ПЕРЕД импортом
     (без них config.py падает с RuntimeError)
  3. Автоматически создать __init__.py в core/, services/, behavior/
     если они отсутствуют (Python требует эти файлы для импорта как пакеты)
  4. Подавить конфликт с системным tests/ пакетом в site-packages

Этот файл должен лежать в КОРНЕ проекта (рядом с app.py, config.py).
"""
import os
import sys

# Абсолютный путь к корню проекта (где лежит этот файл)
ROOT = os.path.dirname(os.path.abspath(__file__))

# Вставляем ROOT в НАЧАЛО sys.path — приоритет над site-packages
if ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)

# Также добавляем tests/ в sys.path, чтобы `from conftest import FakePTZ`
# работал из тестовых файлов (без префикса tests.)
TESTS_DIR = os.path.join(ROOT, "tests")
if TESTS_DIR in sys.path:
    sys.path.remove(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)

# ──────────────────────────────────────────────────────────────────────────
#  Автоматически убедимся, что __init__.py есть в core/, services/, behavior/
# ──────────────────────────────────────────────────────────────────────────
# Python требует __init__.py в директории, чтобы импортировать её как пакет.
# Если пользователь скопировал файлы вручную и забыл __init__.py —
# `from core.state_machine import` упадёт с ModuleNotFoundError.
# Создаём их автоматически, если отсутствуют.
for subdir in ("core", "services", "behavior"):
    init_path = os.path.join(ROOT, subdir, "__init__.py")
    if os.path.isdir(os.path.join(ROOT, subdir)) and not os.path.exists(init_path):
        try:
            with open(init_path, "w") as f:
                f.write('"""Auto-generated __init__.py for %s package."""\n' % subdir)
        except (IOError, PermissionError):
            # Если не можем создать — это увидит пользователь в ошибке импорта
            pass

# Env vars для config.py — ДО любого импорта config/behavior/services
os.environ.setdefault("CRANE_CAMERA_IP", "10.0.0.1")
os.environ.setdefault("CRANE_CAMERA_USER", "test_user")
os.environ.setdefault("CRANE_CAMERA_PASS", "test_pass")
os.environ.setdefault("CRANE_API_TOKEN", "test_token_long_enough")
