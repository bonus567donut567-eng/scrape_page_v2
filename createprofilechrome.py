"""Create and verify the anonymous Chrome runtime used by this scraper."""
from __future__ import annotations

from chrome_profile import PROFILE_PATH, create_selenium_driver


def main() -> int:
    print(f"[profile] ตำแหน่ง: {PROFILE_PATH}")
    print("[profile] กำลังตรวจ Chrome และ ChromeDriver แบบไม่ล็อกอิน...")
    driver = None
    try:
        driver = create_selenium_driver(headless=True, allow_driver_download=True)
        driver.get("about:blank")
        driver.execute_script("return document.readyState")
        print("[profile] ChromeDriver พร้อมใช้งานแบบ anonymous")
        return 0
    except Exception as exc:
        print(f"[profile] สร้าง Chrome profile ไม่สำเร็จ: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
