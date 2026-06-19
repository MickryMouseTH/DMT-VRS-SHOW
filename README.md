# VRS_Show — NVR Playback Tool

เครื่องมือเปิดดูภาพย้อนหลัง (playback) จากกล้องวงจรปิดผ่าน RTSP โดยรองรับ NVR
3 ตัวแบบ fallback ตามลำดับ **Plaza → DC1 → DC2**:

- **Plaza NVR** — channel-based (`cam/playback`, อ้างอิงด้วยชื่อด่าน → channel)
- **DC1 NVR** — channel-based **วิธีเดียวกับ Plaza** (ชื่อ → channel)
- **DC2 NVR** — UID-based (`recordstream`, อ้างอิงด้วยชื่อ lane → UID)

มีหน้าจอ loading ภาษาไทยระหว่างรอเชื่อมต่อ

- **Program:** `VRS_Show`
- **Version:** `1.8.0`
- **ภาษา:** Python 3 (ทดสอบบน Python 3.13)

---

## 1. โครงสร้างโปรเจกต์

```
DMT-VRS-SHOW/
├── vrsshow.py            # โปรแกรมหลัก (entry point)
├── LogLibrary.py         # โหลด config + ตั้งค่า logging (loguru)
├── VRS_Show_config.json  # ไฟล์ config จริงที่ใช้รันงาน
├── _config.json          # ไฟล์ config ตัวอย่าง/สำรอง
├── THSarabunNew.ttf      # ฟอนต์ไทยสำหรับวาดข้อความบนเฟรม
├── logs/                 # ไฟล์ log (หมุนเวียนตามขนาด + บีบอัด .zip)
└── README.md             # เอกสารฉบับนี้
```

### หน้าที่ของแต่ละโมดูล

| ไฟล์ | หน้าที่ |
|------|---------|
| `vrsshow.py` | ตรรกะหลัก: parse argument, สร้าง RTSP URL, เชื่อมต่อสตรีม, แสดงผลวิดีโอ, จัดการ singleton lock |
| `LogLibrary.py` | `Load_Config()` อ่าน/สร้างไฟล์ JSON config และ `Loguru_Logging()` ตั้งค่า sink ทั้ง console และไฟล์ |

---

## 2. การไหลของโปรแกรม (Flow)

```
main
 ├─ handle_singleton_lock()        # อนุญาตให้รันได้ครั้งละ 1 instance
 └─ run_playback(config)           # parse argument ครั้งเดียว แล้วไล่หา NVR ที่รู้จักชื่อนี้
       ├─ play_channel_nvr(Plaza)  # 1) ลอง Plaza ก่อน
       ├─ play_channel_nvr(DC1)    # 2) ถ้าชื่อไม่อยู่ใน Plaza → ลอง DC1 (วิธีเดียวกัน)
       └─ play_uid_nvr(DC2)        # 3) ถ้ายังไม่เจอ → ลอง DC2 (UID)
```

### ตรรกะการไล่ลำดับ (tri-state)
แต่ละ handler คืนค่า 3 สถานะ:

| ค่าที่คืน | ความหมาย | การทำงานต่อ |
|-----------|----------|-------------|
| `None`  | NVR นี้ **ไม่รู้จักชื่อ** ที่ขอ | ลอง NVR ตัวถัดไป |
| `True`  | เชื่อมต่อและเล่นสำเร็จ | หยุด |
| `False` | NVR นี้ **เป็นเจ้าของชื่อ** แต่ต่อ/เล่นไม่สำเร็จ | ลอง NVR ตัวถัดไป |

> หลักคิด: chain จะ **หยุดเฉพาะเมื่อเล่นสำเร็จ (`True`)** เท่านั้น — ถ้า NVR ใด
> ไม่รู้จักชื่อ (`None`) หรือเป็นเจ้าของชื่อแต่ต่อ/เล่นไม่ได้ (`False`) ก็จะ
> ไล่ลองตัวถัดไปตามลำดับ Plaza → DC1 → DC2 จนกว่าจะมีตัวใดเล่นได้

ทุกโหมดมีลำดับการทำงานเหมือนกัน (ผ่าน `_connect_and_play`):

1. สร้าง RTSP URL จาก config + argument
2. เปิด `cv.VideoCapture` ใน **background thread** (UI ไม่ค้าง)
3. แสดงหน้าจอ loading พร้อม spinner ระหว่างรอเชื่อมต่อ (มี timeout)
4. เมื่อเชื่อมต่อสำเร็จ → เล่นวิดีโอด้วย **threaded reader** จนจบ/ผู้ใช้กด `q`

---

## 3. การติดตั้งและรัน

### Dependencies

```bash
pip install opencv-python numpy pillow loguru psutil
```

- **Tkinter** ใช้สำหรับ GUI — มากับ Python อยู่แล้วบน Windows (ไม่ต้องลงเพิ่ม)
  บน Linux บางดิสโทรอาจต้อง `sudo apt install python3-tk`
- **Pillow** ต้องมี `ImageTk` (มากับ `pillow` ปกติ) สำหรับแสดงเฟรมใน Tkinter

### การรัน

```bash
# รูปแบบ : python3 vrsshow.py <ชื่อ> <เวลาเริ่ม DDMMYYYYHHMMSS> [duration(วินาที)]
# duration เป็น optional — ถ้าไม่ใส่จะใช้ play_seconds จาก config

# ชื่ออยู่ใน plaza_to_channel  -> เล่นจาก Plaza NVR
python3 vrsshow.py DM35 19062026120000

# ชื่ออยู่ใน dc1_to_channel    -> เล่นจาก DC1 NVR (วิธีเดียวกับ Plaza)
python3 vrsshow.py DC101 19062026120000

# ชื่ออยู่ใน dc2_lane_to_uid   -> เล่นจาก DC2 NVR (UID) — GUI นับเวลาเอง/ปิด connection
python3 vrsshow.py AN01 19062026120000

# override play_seconds เฉพาะครั้งนี้ให้เล่น 60 วินาที
python3 vrsshow.py DM35 19062026120000 60
```

> argument ตัวแรกเป็นได้ทั้งชื่อด่าน (Plaza/DC1) หรือชื่อ lane (DC2)
> โปรแกรมจะค้นหาตามลำดับ `plaza_to_channel` → `dc1_to_channel` → `dc2_lane_to_uid`
> และเล่นจาก NVR ตัวแรกที่รู้จักชื่อนั้น
> เวลาเริ่ม/ความยาวคุมด้วย `start_offset_seconds` / `play_seconds` ใน config (ดู §4)

### หน้าต่างเล่นวิดีโอ (Tkinter GUI แบบ custom)
ตั้งแต่ v1.7.0 เปลี่ยนจากหน้าต่าง OpenCV เปล่าๆ มาเป็น **Tkinter GUI ธีม dark**
มี **ปุ่มกดจริง** ที่แถบล่าง + แถบสถานะแสดงเวลาเล่น และ loading spinner ภาษาไทย

| ปุ่ม / คีย์ | การทำงาน |
|-------------|----------|
| **⏸ หยุดชั่วคราว** (`p` / `Space`) | pause — ค้างที่เฟรมล่าสุด (ป้าย `PAUSED`), กดซ้ำ (▶ เล่นต่อ) |
| **⟳ โหลดใหม่** (`r`) | reload — เชื่อมต่อใหม่และเล่นตั้งแต่ต้น |
| **■ หยุด/ปิด** (`q` / `Esc`) | หยุดเล่นและ**ปิด connection** ออกจากโปรแกรม |
| ปิดหน้าต่าง **[X]** | เหมือนปุ่มหยุด/ปิด (ปิด connection ด้วย) |

> ทุกกรณีที่ออก (ปิด / ปิดหน้าต่าง / สตรีมจบ / reload) จะ release `VideoCapture`
> ผ่าน `ThreadedVideoStream.stop()` เสมอ — connection RTSP ถูกปิดแน่นอน

---

## 4. รูปแบบไฟล์ Config

```jsonc
{
  // --- Plaza NVR (channel-based) ---
  "plaza_nvr_ip": "172.30.114.100",
  "plaza_nvr_user": "admin",
  "plaza_nvr_password": "********",
  "plaza_to_channel": { "DM23": 49 },    // ชื่อด่าน -> หมายเลข channel

  // --- DC1 NVR (channel-based แบบเดียวกับ Plaza) ---
  "dc1_nvr_ip": "",
  "dc1_nvr_user": "admin",
  "dc1_nvr_password": "",
  "dc1_to_channel": { "DC101": 1 },      // ชื่อ -> หมายเลข channel

  // --- DC2 NVR (UID-based) ---
  "dc2_nvr_ip": "",
  "dc2_lane_to_uid": { "AN01": "uuid..." }, // ชื่อ lane -> stream UID

  // --- เวลาเล่น (ใช้กับทุก NVR) ---
  "start_offset_seconds": 0,             // เริ่มเล่นก่อนเวลาที่ระบุกี่วินาที (ลบออก)
  "play_seconds": 300,                   // เล่นไปกี่วินาที

  "log_Level": "DEBUG",
  "Log_Console": 1,                      // 1 = พิมพ์ log ออก console
  "log_Backup": 90,                      // เก็บ log ย้อนหลัง (วัน)
  "Log_Size": "10 MB"                    // ขนาดไฟล์ก่อนหมุนเวียน
}
```

> เพื่อความเข้ากันได้ย้อนหลัง โค้ดยังอ่านคีย์เดิม `dc_nvr_ip` / `lane_to_uid`
> เป็น DC2 ได้ หากไม่พบ `dc2_nvr_ip` / `dc2_lane_to_uid`

### เวลาเล่น (`start_offset_seconds` / `play_seconds`)
- **`start_offset_seconds`** — เริ่มเล่น **ก่อน** เวลาที่ส่งเข้ามากี่วินาที (ลบออกจากเวลาเริ่ม)
  เช่น เวลา `12:00:00` + offset `10` → เริ่มจริงที่ `11:59:50`
- **`play_seconds`** — ความยาวที่จะเล่น (วินาที)
  - **Plaza / DC1** (channel): กำหนด `endtime = starttime + play_seconds` ใน URL
  - **DC2** (UID/`recordstream`) ไม่มี endtime ใน URL → **GUI นับเวลาเองและปิด connection**
    เมื่อเล่นครบ `play_seconds` (เวลาที่ pause ไม่ถูกนับ)
- ใส่ argument ตัวที่ 3 ตอนรัน (`duration`) เพื่อ **override** `play_seconds` ชั่วคราวได้

> ⚠️ **ความปลอดภัย:** `VRS_Show_config.json` เก็บรหัสผ่าน NVR เป็น plaintext
> ควรจำกัดสิทธิ์ไฟล์ (`chmod 600`) และไม่ commit ขึ้น git

---

## 5. บั๊ก/ข้อบกพร่องที่ตรวจพบและแก้ไข

### 5.1 รอบแรก (v1.3 → v1.4)

| # | ปัญหาเดิม | ผลกระทบ | การแก้ไข |
|---|-----------|----------|----------|
| 1 | `import` ของ `LogLibrary` ซ้ำ 2 บรรทัด | โค้ดรก | รวมเหลือครั้งเดียว |
| 2 | โหลดฟอนต์ `THSarabun.ttf` ใหม่ **ทุกเฟรม** (ทุก 100 ms) | I/O ดิสก์ซ้ำซาก ทำให้หน้าจอ loading หน่วง | cache ฟอนต์ใน `_FONT_CACHE` โหลดครั้งเดียวต่อขนาด |
| 3 | หน้าจอ loading แปลง BGR→PIL→BGR ใหม่ทุกเฟรม | กิน CPU โดยไม่จำเป็น | เรนเดอร์ข้อความครั้งเดียวเป็น base frame แล้ววาดแต่ spinner (OpenCV) |
| 4 | ตั้ง `OPENCV_FFMPEG_CAPTURE_OPTIONS` **ภายใน thread** หลัง/ระหว่างเปิด capture | เกิด race; บางครั้งไม่ได้ใช้ TCP จริง | ย้ายไปตั้งครั้งเดียวตอน import ก่อนสร้าง VideoCapture |
| 5 | DC NVR ไม่ได้บังคับ RTSP over TCP (ต่างจาก Plaza) | สตรีม UDP หลุด/ภาพแตกบนเครือข่ายแย่ | ใช้ค่า TCP ร่วมกันทั้งสองโหมด |
| 6 | ใช้ global `vcap / is_connected / thread_error` ร่วมกันแบบไม่มี lock | ไม่ thread-safe, state ค้างข้ามการเรียก | แทนด้วย `open_capture_async()` ที่คืน container เฉพาะงาน |
| 7 | `Get_DC_Nvr` ตรวจ `vcap.isOpened()` ขณะ `vcap` อาจเป็น `None` | เสี่ยง `AttributeError` | เช็ค `container.get('cap')` เป็น `None` ก่อนใช้ |
| 8 | DC NVR ไม่ตรวจ format เวลา ก่อน `strptime` | crash ด้วย `ValueError` ดิบ | ครอบ `try/except` แจ้ง error ชัดเจน |
| 9 | ไม่มีการคุมจังหวะเฟรม (FPS) ระหว่างเล่น | วิดีโออาจเล่นเร็วผิดจริง | คำนวณ `waitKey delay` จาก FPS ของสตรีม |
| 10 | `os.remove(LOCK_FILE)` อาจ throw หากไฟล์หาย | โปรแกรมตกตอนปิด | ครอบ `try/except OSError` |

### 5.2 รอบสอง — audit ละเอียด (→ v1.5.1)

| # | ปัญหา | ผลกระทบ | การแก้ไข |
|---|-------|----------|----------|
| 1 | DC2 IP ว่าง `or`-fallback เป็น `"127.0.0.1"` | การ์ด `if not ip` ไม่ทำงาน → พยายามต่อ localhost รอ timeout เต็ม | fallback เป็น `""` เพื่อให้การ์ดทำงาน |
| 2 | ไม่มี ffmpeg socket timeout → `VideoCapture()` ค้างได้ไม่จำกัด; timeout บนหน้า loading ไม่ยกเลิกการต่อจริง → cap + thread รั่ว | resource leak, thread ทับซ้อน | เพิ่ม `stimeout;6000000` + `open_capture_async()` รองรับ `abandon_capture()` ปล่อย cap ที่เปิดได้ภายหลัง |
| 3 | `stop()` `release()` cap ขณะ reader ยัง `read()` | undefined behavior / อาจ segfault | reader เป็นเจ้าของ cap คนเดียว, `release()` ใน `finally`; `stop()` แค่ set event + join |
| 4 | singleton kill โปรเซสตาม PID โดยไม่ตรวจตัวตน | อาจ kill โปรเซสอื่นจาก **PID reuse** | `_is_our_process()` ตรวจ `name()`/`cmdline()` ก่อน kill |
| 5 | `cv.destroyWindow()` บนหน้าต่างที่อาจไม่เคยถูกสร้าง | `cv.error` เมื่อ connect ล้มเหลวเร็ว | `_safe_destroy_window()` ครอบ `try/except cv.error` |
| 6 | FPS = 0 → `delay = 1` | วิดีโอย้อนหลัง fast-forward | fallback fps = 25 เมื่ออ่านค่าไม่ได้ |
| 7 | สร้าง lock ไม่ atomic (TOCTOU) | สอง instance รันพร้อมกันได้ | `os.open(O_CREAT\|O_EXCL)` + ลบ lock เฉพาะที่ตัวเองครอง |
| 8 | UID lookup ใช้ `a or b` พังถ้า UID เป็นสตริงว่าง | สับสนระหว่าง "ไม่พบ" กับ "พบแต่ว่าง" | ใช้ `in` ตรวจ key แล้วแยกกรณี UID ว่าง (return `False`) |

---

## 6. การปรับความเร็ว & Multi-threading

### 6.1 Threaded video reader (`ThreadedVideoStream`)
แยกการ **ถอดรหัสเฟรม (network + ffmpeg)** ออกจากการ **แสดงผล** ด้วย
producer–consumer queue:

- **Producer thread** อ่านเฟรมจาก RTSP ใส่คิว (bounded, back-pressure)
- **Main thread** ทำแค่ `cv.imshow` / `waitKey`

ผลคือเมื่อการอ่านเฟรมหนึ่งติดเครือข่าย ภาพยังไหลลื่นจากเฟรมในคิว และคิวแบบ
จำกัดขนาด (`maxsize=16`) ป้องกัน memory โตไม่จำกัด

### 6.2 Async connect (`open_capture_async`)
เปิด `VideoCapture` ใน thread แยก เพื่อให้หน้าจอ loading ตอบสนองและมี timeout
ได้จริง (ผู้ใช้กดยกเลิกได้)

### 6.3 ลด latency ของบัฟเฟอร์
ตั้ง `CAP_PROP_BUFFERSIZE = 1` ให้แสดงเฟรมล่าสุด ไม่สะสมดีเลย์ในงานยาว

### 6.4 GUI ไม่บล็อกด้วย Tk `after()` loop
`VideoPlayerGUI` ดึงเฟรมจากคิวแบบ non-blocking (`stream.read(timeout=0.01)`) ใน
Tk event loop ผ่าน `after()` — main thread แค่ repaint ส่วนการถอดรหัสยังอยู่บน
reader thread ทำให้ UI ไม่ค้างแม้เน็ตสะดุด และใช้ **generation counter** ยกเลิก
callback เก่าตอน reload เพื่อไม่ให้มี loop ซ้อน

---

## 7. GUI (Tkinter แบบ custom) — `VideoPlayerGUI`

หน้าต่างเล่นวิดีโอธีม dark (Catppuccin-inspired) ประกอบด้วย:
- **header** ชื่อสตรีม, **พื้นที่วิดีโอ** (scale รักษาสัดส่วน), **แถบปุ่ม** ล่าง
- ปุ่ม custom (flat + hover): ⏸ หยุดชั่วคราว / ⟳ โหลดใหม่ / ■ หยุด-ปิด
- แถบสถานะแสดง `กำลังเล่น mm:ss`, `PAUSED`, สถานะเชื่อมต่อ/จบ
- loading spinner + ข้อความไทย (เรนเดอร์ด้วย `THSarabunNew.ttf` ผ่าน PIL)

**State machine:** `_start_connect → _poll_connect →(สำเร็จ)→ _begin_playback →
_tick`; `_reload` เพิ่ม `gen` แล้วเริ่ม connect ใหม่; `_on_close`/ปิดหน้าต่าง
เรียก `_teardown_stream` (release cap + `abandon_capture`) เสมอ

---

## 8. โครงสร้างฟังก์ชันใน `vrsshow.py`

| ฟังก์ชัน / คลาส | หน้าที่ |
|----------|---------|
| `_get_font()` / `putText_Thai()` | โหลดฟอนต์ (cache) + วาดข้อความไทย (ใช้ใน loading) |
| `getplaytime()` | แปลงเวลา local → Zulu (UTC) สำหรับ URL ของ DC2 |
| `open_capture_async()` / `abandon_capture()` | เปิดสตรีม async + ยกเลิก/ปล่อย cap หลัง timeout |
| `ThreadedVideoStream` | reader thread + queue เล่นวิดีโอแบบลื่น (reader เป็นเจ้าของ cap) |
| `VideoPlayerGUI` | หน้าต่าง Tkinter custom + ปุ่ม pause/reload/stop + loading |
| `parse_cli_args()` | parse argument (name / time / duration) ครั้งเดียว |
| `_connect_and_play()` | สร้าง `VideoPlayerGUI` แล้วคืนผลว่าเล่นได้ (`ok`) หรือไม่ |
| `play_channel_nvr()` | โหมด channel-based ใช้ร่วมกันโดย Plaza และ DC1 |
| `play_uid_nvr()` | โหมด UID-based สำหรับ DC2 (`recordstream`) |
| `run_playback()` | ไล่ลำดับ Plaza → DC1 → DC2 แล้วเล่นจากตัวที่รู้จักชื่อ |
| `handle_singleton_lock()` | บังคับรันครั้งละ 1 instance ผ่าน lock file + PID |

---

## 9. Logging
ตั้งค่าโดย `LogLibrary.Loguru_Logging()`:
- เขียนลง `logs/VRS_Show_<version>.log`
- หมุนเวียนเมื่อถึงขนาด `Log_Size`, เก็บย้อนหลัง `log_Backup` วัน, บีบอัดเป็น `.zip`
- พิมพ์ออก console เมื่อ `Log_Console = 1`

---

## 10. ข้อเสนอแนะต่อไป (ยังไม่ทำในรุ่นนี้)
- เพิ่ม `requirements.txt` เพื่อ pin เวอร์ชัน dependency
- ย้ายรหัสผ่าน NVR ไปไว้ใน environment variable / secret manager
- เพิ่มแถบ seek/progress bar ใน GUI (ตอนนี้มีแต่ปุ่ม pause/reload/stop)
- เพิ่ม unit test สำหรับ `getplaytime()` และการแปลงเวลา
