# scrape_page_v2

โปรแกรมแยกเดี่ยวสำหรับกวาดโพสต์รูปจาก Facebook Page สาธารณะ แล้วดาวน์โหลด:

- ภาพหลักของโพสต์
- แคปชั่นเต็ม
- ภาพในคอมเมนต์ที่ตรวจได้ว่าเป็นของเจ้าของเพจ
- JSON manifest ต่อโพสต์และต่อรอบการทำงาน
- อัปโหลดภาพไป Google Drive และเพิ่ม caption + ลิงก์ภาพลง Google Sheets ผ่าน API

โปรแกรมไม่ใช้บัญชีหรือ cookie Facebook และไม่เชื่อมกับ Task Runner, Google Flow, ChatGPT หรือคิวของ `googleflowCLI`
ส่วนส่งงานใช้ OAuth ของ Google โดยตรง ไม่ได้กรอก Google Form ผ่าน browser

## ติดตั้ง

เปิด PowerShell ในโฟลเดอร์นี้:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

ต้องมี Python 3.11 แบบ 64 บิต และแนะนำให้ติดตั้ง Google Chrome รุ่นปัจจุบัน

หรือใช้สคริปต์ติดตั้ง:

```powershell
.\setup.ps1
```

ถ้า `scrapIg` มี `client_secret.json` และ `token.json` อยู่แล้ว โปรแกรมจะใช้ไฟล์เดิมนั้น
โดยอัตโนมัติโดยไม่คัดลอก secret เพิ่ม หากต้องการสร้าง token แยกสำหรับโปรเจกต์นี้:

```powershell
.\.venv\Scripts\python.exe setup_oauth.py
```

Google Sheet ต้องมี header `original_caption` และ `original_picture` ส่วนคอลัมน์เวลา
รองรับชื่อ `time`, `timestamp` หรือ `ประทับเวลา` ค่า Spreadsheet ID, Sheet GID และ
โฟลเดอร์ Drive ตั้งผ่าน environment variables ได้:

```powershell
$env:SCRAPE_PAGE_SPREADSHEET_ID = "ใส่ Spreadsheet ID"
$env:SCRAPE_PAGE_SHEET_GID = "ใส่เลข gid"
$env:SCRAPE_PAGE_DRIVE_FOLDER_ID = "ใส่ Folder ID (เว้นว่างให้ระบบหา/สร้างจากชื่อ)"
```

## ใช้งาน

### รอบเต็มหลายเพจ (แนะนำ)

เปิด `pages.txt` แล้วใส่ชื่อผู้ใช้เพจหรือ URL เต็ม หนึ่งเพจต่อหนึ่งบรรทัด:

```text
IG.dara
https://www.facebook.com/page.two
```

จากนั้นรัน:

```powershell
.\run_full.ps1
```

เมื่อดึงเสร็จ รอบเต็มจะส่งโพสต์ใหม่และโพสต์เก่าที่ยังค้างไป Google Drive/Sheets
โดยอัตโนมัติ เมื่อสำเร็จจะสร้าง `submitted.flag` ในโฟลเดอร์โพสต์เพื่อไม่ส่งซ้ำ

ถ้าต้องการเห็นหน้าต่าง browser ตอนรวบรวมลิงก์:

```powershell
.\run_full.ps1 -Headed
```

ดึงอย่างเดียว ไม่ส่ง Google:

```powershell
.\run_full.ps1 -NoSubmit
```

ส่งเฉพาะโพสต์ที่ค้าง โดยไม่เปิด Facebook:

```powershell
.\.venv\Scripts\python.exe main.py --submit-only
```

### เรียกผ่าน bot-hub

โปรเจกต์ถูกลงทะเบียนใน `..\bot-hub\bots.yaml` ด้วย id `scrape-page-v2` แล้ว
กด Trigger จากหน้า `/bots` หรือสร้าง Schedule รายวันใน `/schedules` ได้ คำสั่งที่
bot-hub เรียกคือ `.venv\Scripts\python.exe -u main.py --full-run` และ log จะแสดง
ในหน้า Job ของ bot-hub โดยตรง

รอบเต็มจะทำงานตามลำดับบรรทัดใน `pages.txt` และไล่โพสต์รูปจากใหม่ไปเก่า
โดยรับเฉพาะโพสต์ที่มี **Reaction รวมมากกว่า 3,000** (ค่า 3,000 พอดีไม่ผ่าน)
สูงสุด 20 โพสต์ใหม่ต่อเพจ พร้อมภาพหลัก แคปชั่น และภาพคอมเมนต์ของเจ้าของเพจ
รอบหนึ่งอาจรับได้ 0 โพสต์หากยังไม่มีโพสต์ใหม่ผ่านเกณฑ์

โหมดรอบเต็มใช้ incremental state v2 ใน `data/page_states.json`:

- เก็บ anchor 10 โพสต์แรกต่อเพจเพื่อหาแนวสิ้นสุดของข้อมูลใหม่
- เมื่อพบ anchor เดิมครบ 3 รายการ จะตรวจเผื่อต่ออีก 20 โพสต์แล้วหยุด
- สแกนสูงสุด 200 โพสต์ต่อเพจหากยังไม่พบ anchor
- โพสต์ที่ยังไม่ถึงเกณฑ์จะอยู่ใน watchlist และถูกตรวจซ้ำเป็นเวลา 7 วัน
- โพสต์ที่อ่านยอดไม่ได้จะ retry ในรอบถัดไปและไม่ถูกตัดเป็นหมดอายุ
- โพสต์ที่ส่งสำเร็จแล้วจะไม่ถูกส่งซ้ำ

anchor จะอัปเดตเฉพาะเมื่อกวาดเพจจบตามปกติ หากพบ login wall, checkpoint หรือ
Facebook block ระบบจะเก็บ anchor เดิมไว้เพื่อไม่ให้ข้ามโพสต์โดยไม่ตั้งใจ

คำว่า Like ในโหมดนี้หมายถึงยอด Reaction รวมที่ Facebook แสดง เช่น Like, Love และ Haha
เนื่องจากหน้าสาธารณะไม่ได้แสดงยอด Like แยกชนิดอย่างสม่ำเสมอ

กวาดเพจเดียว สูงสุด 20 โพสต์ใหม่:

```powershell
.\.venv\Scripts\python.exe main.py --url "https://www.facebook.com/IG.dara" --max-posts 20
```

หลายเพจ:

```powershell
.\.venv\Scripts\python.exe main.py `
  --url "https://www.facebook.com/page.one" `
  --url "https://www.facebook.com/page.two" `
  --max-posts 10
```

ไม่ดึงภาพคอมเมนต์:

```powershell
.\.venv\Scripts\python.exe main.py --url "https://www.facebook.com/IG.dara" --no-comments
```

แสดง browser ตอนกวาดลิงก์เพื่อ debug:

```powershell
.\.venv\Scripts\python.exe main.py --url "https://www.facebook.com/IG.dara" --headed
```

ดึงโพสต์รูปเดี่ยว:

```powershell
.\.venv\Scripts\python.exe main.py --post-url "https://www.facebook.com/photo/?fbid=123456789"
```

บังคับตรวจใหม่โดยไม่ใช้ประวัติเคยตรวจและไม่ใช้เงื่อนไขหยุด 5 โพสต์เดิม:

```powershell
.\.venv\Scripts\python.exe main.py --url "https://www.facebook.com/IG.dara" --retry-all
```

## โครงสร้างผลลัพธ์

```text
outputs/
├── latest_run.json
├── runs/
│   └── run_YYYYMMDD_HHMMSS.json
├── debug/
│   ├── page_1_login_required.png
│   └── page_1_login_required.html
└── posts/
    └── <post_id>/
        ├── image.jpg
        ├── caption.txt
        ├── post.json
        ├── submitted.flag
        └── comments/
            ├── comment_001.jpg
            └── comment_media_manifest.json
```

ใน `post.json` จะมี `reaction_count`, `reaction_text`, `reaction_source` และ
`minimum_reactions_exclusive` เพื่อบันทึกยอดและหลักฐานที่ใช้ตัดสิน โพสต์ที่ถูกกรองออก
จะอยู่ใน `outputs/latest_run.json` แต่จะไม่มีโฟลเดอร์ภาพใต้ `outputs/posts/`

`data/page_states.json` เป็นประวัติหลักของโหมดกรอง Reaction โดยเก็บ anchor, watchlist,
ยอดล่าสุด และสถานะการส่งแยกต่อเพจ ส่วน `data/seen_posts.json` ถูกนำเข้าเป็นรายการสำเร็จเดิม
เพื่อกันส่งซ้ำ `data/scanned_posts.json` เป็นประวัติรุ่นเก่าและไม่ถูกใช้ในรอบเต็มแบบใหม่
เนื่องจากข้อมูลเดิมบางส่วนสร้างจาก parser Reaction รุ่นเก่า

ค่าปริยายปรับได้ด้วย `--watch-days`, `--anchor-hits`, `--anchor-tail` และ
`--page-state` โดยทั่วไปไม่จำเป็นต้องเปลี่ยนเมื่อใช้ `--full-run`

## Automatic cleanup

หลังส่ง Google Drive/Sheets สำเร็จ ระบบจะล้างไฟล์ในเครื่องโดยอัตโนมัติเมื่อพบ
submitted.flag แล้วเท่านั้น รูปหลัก, caption.txt และโฟลเดอร์รูปคอมเมนต์จะถูกลบ
แต่ post.json กับ submitted.flag จะยังอยู่เพื่อกันส่งซ้ำ ข้อมูลที่ยังส่งไม่สำเร็จจะไม่ถูกลบ

ระบบจะลบไฟล์ใน outputs/debug ที่เก่ากว่า 7 วัน และเก็บรายงานใน outputs/runs
ล่าสุด 30 รอบ รูปบน Google Drive จะไม่ถูกลบเพราะลิงก์ใน Google Sheet ยังต้องใช้งาน

หากส่ง Google Drive/Sheets ครั้งแรกไม่สำเร็จ ระบบจะรอ 20 วินาทีแล้วลองส่งซ้ำทันที
ถ้าครั้งที่ 2 ยังไม่สำเร็จ จะบันทึกสถานะ failed, หยุดส่งซ้ำ และลบไฟล์รูปกับ
โฟลเดอร์รูปคอมเมนต์พร้อม caption.txt ทันที โดยเก็บเฉพาะ post.json และสถานะไว้ตรวจสอบ
ประวัติ Post ID จะยังอยู่เพื่อไม่ให้ดึงโพสต์เดิมใหม่

## Exit code

- `0` ทำงานสำเร็จหรือไม่มีโพสต์ใหม่
- `2` มีอย่างน้อยหนึ่งโพสต์ที่ดึงไม่สำเร็จ
- `3` มี instance อื่นกำลังทำงานอยู่

## ข้อจำกัด

- Facebook เปลี่ยน DOM ได้ตลอด หากเริ่มดึงไม่ได้ให้ดู `outputs/debug`
- เพจหรือโพสต์ที่ Facebook บังคับล็อกอินอาจดึงไม่ได้
- รองรับโพสต์รูปเป็นหลัก ไม่ได้ออกแบบสำหรับ Reels หรือวิดีโอ
- ใช้กับเพจและข้อมูลที่คุณมีสิทธิ์เก็บเท่านั้น
