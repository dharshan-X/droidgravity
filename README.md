<div align="center">

  <p>
    <b>English</b> &nbsp;&bull;&nbsp; <a href="README.ru.md">Russian</a>
  </p>

  <br/>

  <img src="docs/assets/logo.png" alt="DroidGravity" width="88" height="88" style="border-radius: 20px;" />

  # DroidGravity

**Autonomous Google Antigravity development environment running natively on Android & ARM64.**

  [![Android](https://img.shields.io/badge/Android-7.0%2B-3DDC84?style=flat-square&logo=android&logoColor=white)](https://android.com)
  [![Arch](https://img.shields.io/badge/ARM64-v8.0--A%2B-blue?style=flat-square)](https://arm.com)
  [![Core](https://img.shields.io/badge/Antigravity_Core-2.35.2-blue?style=flat-square)](https://github.com/dharshan-X/droidgravity/releases)
  [![Release](https://img.shields.io/badge/App_Version-2.4.2-blueviolet?style=flat-square)](https://github.com/dharshan-X/droidgravity/releases)
  [![License](https://img.shields.io/badge/License-Apache_2.0-orange?style=flat-square)](LICENSE)

  <br/>

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero_dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero_light.png">
    <img alt="DroidGravity Interface" src="docs/assets/hero_dark.png" width="100%" style="border-radius: 12px; border: 1px solid rgba(128,128,128,0.2);" />
  </picture>

</div>

---

### Overview

**DroidGravity** brings Google Antigravity (currently **Core v2.35.2**) directly to Android ARM64 mobile hardware, forked and extended from `werdio325-png/antigravity-android`.

No PRoot, chroot, or virtualization overhead. DroidGravity runs **bare-metal inside native Android userspace (Bionic)** alongside a bundled glibc loader (`ld-linux-aarch64.so.1`), unleashing the full read/write speed of UFS storage and physical CPU cores. Driven by Gemini models, the agent inspects repositories, authors code across multiple files, compiles binaries, and runs tests completely on-device.

---

### Highlights

* **Antigravity Core v2.35.2:** Bundles and binary-patches the upstream `aarch64` `language_server` (`2.35.2`) for Android seccomp, `/etc/resolv.conf`, PTY shell (`/bin/bash` -> `/bin///sh`), `SIGILL` preservation, and ARMv8.0-A CPU compatibility.
* **Non-Destructive Mobile UI Override:** Extracts the genuine upstream Jetski Web UI directly from `language_server` at build time (`web/extract_upstream.py`) and layers mobile overrides on top (`web/override_html.py`, `web/patch_main.py`, `web/src/**`) instead of overwriting upstream with a stale static snapshot.
* **Modular Core:** Clean separation into 6 standalone subsystems (`modules/*`) with independent test suites.
* **Native Splash:** Canvas-based dot animation saving ~40 MB RAM with instant startup.
* **Desktop Mode:** Instant toggle injected into Appearance settings for tablets, external keyboards, and Samsung DeX.
* **Universal ARMv8.0-A Support:** Built-in transparent atomic & SIMD emulation (`liblse_compat.so`) for older ARMv8.0-A CPUs without hardware LSE.
* **Background Micro-Daemon:** Lightweight `agy-daemon` (<10 MB RAM) with cron scheduler, file watcher, and AI task queue.
* **GitHub Actions CI/CD:** Automated workflow building and publishing signed standard and region-bypass APKs with SHA-256 checksums.

---

### Key Features

* **Autonomous Agent:** Plans tasks, edits code across project trees, executes shell tools, and fixes build errors.
* **Elevated Control (Shizuku):** Direct ADB-level operations (`uid=2000`) without root for package installs and system diagnostics.
* **Built-in `pkg` Manager:** Fast package manager written in pure Bash and AWK installing development runtimes (`git`, `python`, `node`, `clang`) from Termux repositories.
* **Adaptive Viewports:** Touch-first mobile UI (`viewport` IME resizing, zero-delay taps, touch dropdown menus) with an optional desktop layout toggle.
* **In-Place Upgrades:** Consistent keystore guarantees updating APKs without losing sessions or workspace data.

---

### Quick Start

1. **Download APK** from [Releases](https://github.com/dharshan-X/droidgravity/releases):
   * `antigravity.apk` — standard release (original Google account checks).
   * `antigravity-region-bypass.apk` — release with account eligibility check bypass.
2. **Grant Storage Permission** (`All Files Access`). *(Optional: enable Shizuku for ADB access)*.
3. **Install Runtimes** via terminal or agent:
   ```bash
   pkg update && pkg install git python nodejs
   ```

---

### Building & Testing

#### Local Build

Requirements: `python3`, `java` / `javac` (JDK 8+), `aapt`, `apksigner`, and optionally `node` (for JS syntax validation).

```bash
# Build standard release APK (dist/antigravity.apk):
bash build.sh

# Build release APK with region bypass (dist/antigravity-region-bypass.apk):
bash build.sh --region-bypass

# Run all test suites:
bash test_all_modules.sh
```

#### GitHub Actions Build

1. Trigger **Build and Release Antigravity APKs** from the **Actions** tab (`workflow_dispatch`) or push a `v*` tag.
2. Download `antigravity.apk`, `antigravity-region-bypass.apk`, and `SHA256SUMS.txt` from the workflow artifacts or the generated GitHub Release.

---

### Repository Structure

```text
droidgravity/
├── .github/workflows/  # CI/CD pipeline for building and publishing APKs
├── config/             # App constants, environment paths, and AI rules
├── modules/            # Decoupled core modules
│   ├── core/           # Binary unpacking, glibc loader, shims, SHA-256 manifest
│   ├── patches/        # Binary patch engine (syscall emulation, resolv, SIGILL, LSE)
│   ├── io/             # High-performance I/O Bus: event broker, FIFO pipes, sockets
│   ├── env/            # Path resolver, diagnostics, Flight Recorder logs, CLI
│   ├── builder/        # Gradle-free APK builder (javac, D8, aapt, zipalign, apksigner)
│   └── runner/         # Standalone runtime preparation, port probe, E2E tests
├── app/                # Android host app (native splash, WebView, system bridges)
├── web/                # Upstream web extractor & non-destructive mobile UI overrides
├── env/                # Bionic userspace, Termux overlay, pkg manager, liblse_compat
├── docs/assets/        # Screenshots, banners, and diagrams
├── build.sh            # Root APK build script
└── test_all_modules.sh # Test suite for all 6 modules
```

Each module in `modules/`, `app/`, `web/`, and `env/` includes its own dedicated `README.md` with in-depth technical documentation.

---

<div align="center">
  <sub>Licensed under the <a href="LICENSE">Apache 2.0 License</a></sub>
</div>
