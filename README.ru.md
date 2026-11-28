<div align="center">

  <p>
    <a href="README.md">English</a> &nbsp;&bull;&nbsp; <b>Russian</b>
  </p>

  <br/>

  <img src="docs/assets/logo.png" alt="DroidGravity" width="88" height="88" style="border-radius: 20px;" />

  # DroidGravity

**Автономная среда разработки Google Antigravity, работающая нативно на Android и ARM64.**

  [![Android](https://img.shields.io/badge/Android-7.0%2B-3DDC84?style=flat-square&logo=android&logoColor=white)](https://android.com)
  [![Arch](https://img.shields.io/badge/ARM64-v8.0--A%2B-blue?style=flat-square)](https://arm.com)
  [![Core](https://img.shields.io/badge/Antigravity_Core-2.36.0-blue?style=flat-square)](https://github.com/dharshan-X/droidgravity/releases)
  [![Release](https://img.shields.io/badge/Версия-2.4.2-blueviolet?style=flat-square)](https://github.com/dharshan-X/droidgravity/releases)
  [![License](https://img.shields.io/badge/Лицензия-Apache_2.0-orange?style=flat-square)](LICENSE)

  <br/>

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero_dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero_light.png">
    <img alt="Интерфейс DroidGravity" src="docs/assets/hero_dark.png" width="100%" style="border-radius: 12px; border: 1px solid rgba(128,128,128,0.2);" />
  </picture>

</div>

---

### Обзор

**DroidGravity** переносит Google Antigravity (**Core v2.36.0**) прямо на мобильные устройства Android ARM64 на базе `werdio325-png/antigravity-android`.

Никаких PRoot, chroot или накладных расходов виртуализации. DroidGravity исполняется **нативно в пространстве пользователя Android (Bionic)** с использованием встроенного загрузчика glibc (`ld-linux-aarch64.so.1`), раскрывая полную скорость физических ядер CPU и накопителей UFS.

---

### Ключевые изменения

* **Ядро Antigravity v2.36.0:** Интегрирован и пропатчен бинарник `language_server` (`aarch64` v2.36.0) с обходом ограничений seccomp, `/etc/resolv.conf`, PTY-шелла (`/bin/bash` -> `/bin///sh`), `SIGILL` и поддержкой ARMv8.0-A.
* **Наложение мобильного UI поверх оригинала (Override):** Вместо перезаписи старой копией, сборщик извлекает актуальный веб-бандл напрямую из `language_server` (`web/extract_upstream.py`) и накладывает мобильные патчи (`web/override_html.py`, `web/patch_main.py`, `web/src/**`).
* **Модульная архитектура:** Разделение ядра на 6 изолированных подсистем (`modules/*`) с отдельными тестами.
* **Нативный сплэш:** Отрисовка анимации точек на Canvas экономит ~40 МБ RAM и ускоряет запуск.
* **Desktop Mode:** Переключатель в настройках *Appearance* для планшетов, клавиатур и Samsung DeX.
* **Поддержка ARMv8.0-A:** Встроенная эмуляция атомиков и SIMD (`liblse_compat.so`) для старых чипсетов.
* **Фоновый микро-демон:** Легковесный `agy-daemon` (<10 МБ RAM) с планировщиком cron, слежением за файлами и очередью AI-задач.

---

### Быстрый старт

1. **Скачайте APK** со страницы [Releases](https://github.com/dharshan-X/droidgravity/releases):
   * `antigravity.apk` — стандартная версия (оригинальные проверки Google).
   * `antigravity-region-bypass.apk` — версия с обходом проверки учетных записей.
2. **Предоставьте доступ к памяти** (`Все файлы`). *(Опционально: подключите Shizuku для доступа к ADB)*.
3. **Установите рабочее окружение** в терминале или через агента:
   ```bash
   pkg update && pkg install git python nodejs
   ```

---

### Сборка и тестирование

```bash
# Сборка стандартного релизного APK (dist/antigravity.apk):
bash build.sh

# Сборка релизного APK с обходом проверок (dist/antigravity-region-bypass.apk):
bash build.sh --region-bypass

# Запуск полного набора тестов:
bash test_all_modules.sh
```

---

### Структура проекта

```text
droidgravity/
├── .github/workflows/  # Конвейер GitHub Actions для сборки и публикации APK
├── config/             # Константы приложения, пути окружения и правила агента
├── modules/            # Модульная архитектура ядра
│   ├── core/           # Распаковка, glibc-линковщик, системные шимы, SHA-256
│   ├── patches/        # Движок бинарного патчинга (системные вызовы, DNS, SIGILL, LSE)
│   ├── io/             # Шина ввода-вывода (I/O Bus): event broker, FIFO, сокеты
│   ├── env/            # Резолвер путей, диагностика, логи Flight Recorder, CLI
│   ├── builder/        # Сборщик APK без Gradle (javac, D8, aapt, zipalign, apksigner)
│   └── runner/         # Подготовка рантайма, health probe, E2E тесты
├── app/                # Android-хост (нативный сплэш, WebView, мосты)
├── web/                # Извлечение веб-бандла из ядра и мобильные оверлеи
├── env/                # Bionic userspace, оверлей Termux, менеджер pkg, liblse_compat
├── docs/assets/        # Скриншоты, баннеры и диаграммы
├── build.sh            # Корневой скрипт сборки APK
└── test_all_modules.sh # Полный набор тестов для всех 6 модулей
```

Каждый модуль в `modules/`, `app/`, `web/` и `env/` содержит собственный файл `README.md` с подробным описанием архитектуры и компонентов.

---

<div align="center">
  <sub>Распространяется под лицензией <a href="LICENSE">Apache 2.0</a></sub>
</div>
