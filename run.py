#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Универсальный запуск FL.Radar.
- проверяет зависимости и доустанавливает их через python -m pip
- запускает server.py (он сам откроет браузер)
"""
import os
import sys
import subprocess
import importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# имя импорта -> имя пакета pip
REQUIRED = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "flask": "flask",
}


def missing_packages():
    missing = []
    for module, package in REQUIRED.items():
        if importlib.util.find_spec(module) is None:
            missing.append(package)
    return missing


def install(packages):
    print("[*] Установка зависимостей:", ", ".join(packages))
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])
    except subprocess.CalledProcessError as e:
        print("[!] Не удалось установить зависимости:", e)
        print("    Попробуйте вручную:")
        print("   ", sys.executable, "-m pip install", " ".join(packages))
        sys.exit(1)


def main():
    os.chdir(BASE_DIR)
    missing = missing_packages()
    if missing:
        install(missing)

    server = os.path.join(BASE_DIR, "server.py")
    if not os.path.exists(server):
        print("[!] server.py не найден рядом с run.py")
        sys.exit(1)

    print("[*] Запуск FL.Radar...")
    # Запускаем сервер в этом же процессе
    runpy_globals = {"__name__": "__main__", "__file__": server}
    with open(server, "r", encoding="utf-8") as f:
        code = compile(f.read(), server, "exec")
    exec(code, runpy_globals)


if __name__ == "__main__":
    main()