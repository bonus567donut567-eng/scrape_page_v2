"""Shared Chrome profile and bounded-cache runtime for Facebook automation."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from browser_runtime import require_runtime_browser_executable


ROOT = Path(__file__).resolve().parent
PROFILE_PATH = Path(
    os.getenv("SCRAPE_PAGE_CHROME_PROFILE", str(ROOT / "chrome-profile"))
).expanduser().resolve()
LOCAL_DRIVER_PATH = PROFILE_PATH / "driver" / ("chromedriver.exe" if os.name == "nt" else "chromedriver")
PROFILE_CACHE_SOFT_LIMIT_MB = 400
PREFERENCES_MAX_MB = 5
CACHE_CHECK_INTERVAL_SECONDS = 300
_last_cache_check = 0.0

SAFE_CACHE_DIRS = (
    ("Default", "Cache"),
    ("Default", "Code Cache"),
    ("Default", "Media Cache"),
    ("Default", "GPUCache"),
    ("Default", "DawnWebGPUCache"),
    ("Default", "DawnGraphiteCache"),
    ("Default", "GraphiteDawnCache"),
    ("Default", "Service Worker", "CacheStorage"),
    ("Default", "Service Worker", "ScriptCache"),
    ("Default", "Shared Dictionary"),
    ("GPUPersistentCache",),
    ("GrShaderCache",),
    ("ShaderCache",),
    ("GraphiteDawnCache",),
    ("DawnGraphiteCache",),
    ("DawnWebGPUCache",),
    ("Crashpad", "reports"),
    ("Crashpad", "pending"),
    ("BrowserMetrics",),
    ("component_crx_cache",),
    ("extensions_crx_cache",),
)
SAFE_CACHE_FILES = (
    "BrowserMetrics-spare.pma", "CrashpadMetrics-active.pma", "CrashpadMetrics.pma",
)


def _path_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    return total


def profile_cache_size_mb(profile_path: Path = PROFILE_PATH) -> float:
    return sum(_path_size(profile_path.joinpath(*parts)) for parts in SAFE_CACHE_DIRS) / 1048576


def clean_profile_caches(profile_path: Path = PROFILE_PATH) -> float:
    """Remove only regenerable cache paths; preserve cookies and login state."""
    freed = 0
    if not profile_path.is_dir():
        return 0.0
    for parts in SAFE_CACHE_DIRS:
        target = profile_path.joinpath(*parts)
        size = _path_size(target)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                freed += size
    for name in SAFE_CACHE_FILES:
        target = profile_path / name
        size = _path_size(target)
        try:
            target.unlink(missing_ok=True)
            freed += size
        except OSError:
            pass
    return freed / 1048576


def reset_bloated_preferences(profile_path: Path = PROFILE_PATH) -> list[str]:
    """Reset oversized settings files without touching cookies or Local State."""
    removed: list[str] = []
    for name in ("Preferences", "Secure Preferences"):
        target = profile_path / "Default" / name
        if _path_size(target) <= PREFERENCES_MAX_MB * 1048576:
            continue
        try:
            target.unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


def prepare_profile(profile_path: Path = PROFILE_PATH) -> Path:
    global _last_cache_check
    profile_path.mkdir(parents=True, exist_ok=True)
    removed = reset_bloated_preferences(profile_path)
    if removed:
        print(f"[profile] รีเซ็ตไฟล์ตั้งค่าที่บวม: {', '.join(removed)}")
    now = time.monotonic()
    if now - _last_cache_check >= CACHE_CHECK_INTERVAL_SECONDS:
        _last_cache_check = now
        cache_mb = profile_cache_size_mb(profile_path)
        if cache_mb > PROFILE_CACHE_SOFT_LIMIT_MB:
            freed = clean_profile_caches(profile_path)
            print(f"[profile] ล้าง Chrome cache {freed:.1f} MB (คง cookies/login ไว้)")
    return profile_path


def chrome_arguments() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-notifications",
        "--mute-audio",
        "--disk-cache-size=262144000",
        "--media-cache-size=104857600",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1440,900",
        "--disable-features=Vulkan,WebGPU,UseSkiaRenderer",
        "--no-first-run",
    ]


def _chrome_major(executable: str) -> int | None:
    if os.name != "nt":
        return None
    try:
        env = os.environ.copy()
        env["SCRAPE_PAGE_CHROME_EXE"] = executable
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-Item -LiteralPath $env:SCRAPE_PAGE_CHROME_EXE).VersionInfo.ProductVersion",
            ],
            capture_output=True, text=True, timeout=10, env=env,
        )
        return int(result.stdout.strip().split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _driver_major(executable: Path) -> int | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for token in result.stdout.split():
            if token[:1].isdigit():
                return int(token.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def ensure_local_chromedriver(*, allow_download: bool = False) -> Path:
    """Prepare one matching driver during setup; runtime never downloads it."""
    browser = require_runtime_browser_executable()
    browser_major = _chrome_major(browser)
    configured = os.getenv("SCRAPE_PAGE_CHROMEDRIVER", "").strip()
    local = LOCAL_DRIVER_PATH
    if local.is_file() and (browser_major is None or _driver_major(local) == browser_major):
        return local
    if not allow_download:
        raise RuntimeError(
            "ไม่พบ ChromeDriver local ที่ตรงกับ Chrome กรุณารัน .\\setup.ps1 ก่อน"
        )

    from selenium.webdriver.common.selenium_manager import SeleniumManager

    if configured:
        source = Path(configured).expanduser().resolve()
    else:
        result = SeleniumManager().binary_paths([
            "--browser", "chrome", "--browser-path", browser,
        ])
        source = Path(str(result.get("driver_path", ""))).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"เตรียม ChromeDriver ไม่สำเร็จ: ไม่พบไฟล์ {source}")
    if browser_major is not None and _driver_major(source) != browser_major:
        raise RuntimeError(
            f"ChromeDriver ไม่ตรงรุ่น Chrome {browser_major}: {source}"
        )
    local.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, local)
    return local


def cleanup_failed_profile_processes(profile_path: Path = PROFILE_PATH) -> None:
    """Best-effort cleanup limited to Chrome processes using this exact profile."""
    if os.name != "nt":
        return
    env = os.environ.copy()
    env["SCRAPE_PAGE_PROFILE_TO_CLEAN"] = str(profile_path)
    command = (
        "$p=$env:SCRAPE_PAGE_PROFILE_TO_CLEAN; "
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like ('*' + $p + '*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_path / name).unlink(missing_ok=True)
        except OSError:
            pass

def create_selenium_driver(
    *,
    headless: bool = True,
    profile_path: Path = PROFILE_PATH,
    allow_driver_download: bool = False,
):
    """Create Selenium Chrome with an explicit local driver and persistent profile."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    profile_path = prepare_profile(profile_path)
    executable = require_runtime_browser_executable()
    driver_executable = ensure_local_chromedriver(allow_download=allow_driver_download)
    options = Options()
    options.binary_location = executable
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new" if headless else "--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    for argument in chrome_arguments():
        options.add_argument(argument)
    try:
        driver = webdriver.Chrome(
            service=Service(executable_path=str(driver_executable)),
            options=options,
        )
    except Exception:
        cleanup_failed_profile_processes(profile_path)
        raise
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(30)
    return driver


def preflight_chrome_runtime() -> None:
    """Prove Selenium can start before link collection creates a large queue."""
    driver = None
    try:
        driver = create_selenium_driver(headless=True)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                cleanup_failed_profile_processes(PROFILE_PATH)