"""Create/verify the dedicated Facebook Chrome profile for this scraper."""
from __future__ import annotations

from chrome_profile import PROFILE_PATH, create_selenium_driver


def main() -> int:
    print(f"[profile] ตำแหน่ง: {PROFILE_PATH}")
    print("[profile] กำลังเปิด Facebook สำหรับตั้งค่า session...")
    driver = None
    try:
        driver = create_selenium_driver(headless=False, allow_driver_download=True)
        driver.get("https://www.facebook.com/")
        print("\nล็อกอิน Facebook ในหน้าต่าง Chrome ให้เรียบร้อย")
        input("เมื่อล็อกอินเสร็จแล้ว กด Enter ที่หน้าต่างนี้เพื่อบันทึกและปิด Chrome...")
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
