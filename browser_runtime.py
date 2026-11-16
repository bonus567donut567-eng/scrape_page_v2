"""Portable browser discovery for Playwright-based automation.

The Python package and its downloaded Chromium are separate installations.
After moving the project, upgrading Playwright, or using another computer the
package can therefore exist while ``browser_type.executable_path`` does not.
Prefer an explicitly configured/system browser and use bundled Chromium only
when its executable is actually present.
"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


BROWSER_EXECUTABLE_ENV = "GOOGLEFLOW_BROWSER_EXECUTABLE"


class BrowserNotFoundError(RuntimeError):
    """Raised when no Chromium-compatible executable is available."""


def _unique_paths(values: Iterable[str | os.PathLike[str] | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        path = Path(os.path.expandvars(str(value))).expanduser()
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _system_browser_candidates() -> list[Path]:
    configured = os.getenv(BROWSER_EXECUTABLE_ENV, "").strip()
    values: list[str | os.PathLike[str] | None] = [configured]

    if os.name == "nt":
        program_files = os.getenv("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app_data = os.getenv("LOCALAPPDATA", "")
        for root in (program_files, program_files_x86, local_app_data):
            if root:
                values.extend((
                    Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                    Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                ))
        command_names = ("chrome.exe", "msedge.exe")
    elif sys.platform == "darwin":
        values.extend((
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ))
        command_names = ("google-chrome", "chromium", "microsoft-edge")
    else:
        command_names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
        )

    values.extend(shutil.which(name) for name in command_names)
    return _unique_paths(values)


def browser_candidates(browser_type: Any | None = None) -> list[Path]:
    """Return portable candidates in preference order.

    ``browser_type`` is normally ``playwright.chromium``.  It is optional so
    startup diagnostics can still report an installed Chrome/Edge even if the
    Playwright Python package itself is missing.
    """
    values: list[str | os.PathLike[str] | None] = list(_system_browser_candidates())
    if browser_type is not None:
        try:
            values.append(browser_type.executable_path)
        except Exception:
            pass
    return _unique_paths(values)


def find_browser_executable(browser_type: Any | None = None) -> str:
    """Return the first existing Chrome/Edge/Chromium executable, or ``""``."""
    for candidate in browser_candidates(browser_type):
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def find_runtime_browser_executable() -> str:
    """Find a system browser or the Chromium bundled for this Playwright."""
    executable = find_browser_executable()
    if executable:
        return executable
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return find_browser_executable(playwright.chromium)
    except Exception:
        return ""


def require_browser_executable(browser_type: Any | None = None) -> str:
    executable = find_browser_executable(browser_type)
    if executable:
        return executable
    configured = os.getenv(BROWSER_EXECUTABLE_ENV, "").strip()
    configured_hint = (
        f" ค่าปัจจุบันของ {BROWSER_EXECUTABLE_ENV} คือ {configured!r} แต่ไม่พบไฟล์"
        if configured else ""
    )
    raise BrowserNotFoundError(
        "ไม่พบ Google Chrome, Microsoft Edge หรือ Chromium ของ Playwright บนเครื่องนี้; "
        "ติดตั้ง Chrome/Edge หรือรัน `python -m playwright install chromium` แล้วลองใหม่."
        + configured_hint
    )


def require_runtime_browser_executable() -> str:
    executable = find_runtime_browser_executable()
    if executable:
        return executable
    return require_browser_executable()


def chromium_launch_options(browser_type: Any) -> dict[str, str]:
    """Options shared by persistent and non-persistent Chromium launches."""
    return {"executable_path": require_browser_executable(browser_type)}


def stop_failed_playwright_manager(manager) -> None:
    """Release a partially started driver, including its pending init task."""
    if manager is None:
        return
    try:
        manager.__exit__(None, None, None)
    except Exception:
        pass


@contextmanager
def playwright_session(factory, attempts=3):
    """Retry driver startup only; never retry actions performed by the caller."""
    import time

    for attempt in range(1, attempts + 1):
        manager = None
        try:
            manager = factory()
            playwright = manager.start()
            break
        except Exception:
            stop_failed_playwright_manager(manager)
            if attempt == attempts:
                raise
            time.sleep(2)
    try:
        yield playwright
    finally:
        try:
            playwright.stop()
        except Exception:
            pass
