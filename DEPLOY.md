# Deploy Go-Manga Bot แบบ Real-time บน Oracle Cloud (ฟรี 24 ชม.)

คู่มือตั้งค่าให้บอทรันตลอดเวลา ตอบคำสั่ง/ปุ่มทันที และเช็คตอนใหม่ทุก 5 นาที
บนเครื่อง VM ฟรีของ Oracle Cloud (Always Free)

---

## ภาพรวม
- รัน `bot_realtime.py` ค้างไว้บน VM ด้วย systemd (auto-restart ถ้าล่ม)
- state (bookmark ฯลฯ) เก็บบนดิสก์ VM → ไม่หายตอนรีสตาร์ท
- **ต้องปิด GitHub Actions schedule** เพื่อไม่ให้แจ้งเตือนซ้ำ

---

## Phase 1 — สมัคร Oracle + สร้าง VM

1. สมัคร https://www.oracle.com/cloud/free/ (ต้องใช้บัตรเครดิต/เดบิตยืนยันตัวตน
   แต่โซน **Always Free** ไม่ถูกตัดเงิน) เลือก Home Region ใกล้ ๆ เช่น Singapore / Japan
2. เข้า Console → เมนู ☰ → **Compute → Instances → Create Instance**
3. ตั้งค่า:
   - **Image:** **Oracle Linux 9** (ค่าเริ่มต้น ใช้ได้เลย ไม่ต้องหา Ubuntu)
   - **Shape:** กด Change shape → **Ampere (ARM)** ถ้าเต็มให้เลือก
     **VM.Standard.E2.1.Micro** (AMD, 1GB RAM) — เป็น Always Free ทั้งคู่
   - **SSH keys:** เลือก **Generate a key pair for me** แล้ว **กด Download**
     ทั้ง private + public key (เก็บ private key ไว้ให้ดี ใช้ล็อกอิน)

> ⚠️ Oracle Linux 9 → user สำหรับ SSH คือ **`opc`** (ไม่ใช่ `ubuntu`)
4. กด **Create** รอสักครู่จน state = **Running** แล้วจดค่า **Public IP address**

> ไม่ต้องเปิด port อะไรเพิ่ม เพราะบอทเป็นการเชื่อมออก (outbound) อย่างเดียว

---

## Phase 2 — เข้า VM แล้วติดตั้ง

เปิด Terminal/PowerShell ในเครื่องคุณ (สมมติ private key ชื่อ `ssh-key.key` อยู่ใน Downloads):

```bash
# Windows: ทำให้ไฟล์ key ปลอดภัยก่อน (ครั้งเดียว)
icacls "%USERPROFILE%\Downloads\ssh-key.key" /inheritance:r /grant:r "%USERNAME%:R"

# ล็อกอินเข้า VM (แทน <PUBLIC_IP> ด้วย IP ที่จดไว้)
ssh -i "%USERPROFILE%\Downloads\ssh-key.key" ubuntu@<PUBLIC_IP>
```

> **Oracle Linux 9** ล็อกอินด้วย user `opc`:
> ```bash
> ssh -i "%USERPROFILE%\Downloads\ssh-key.key" opc@<PUBLIC_IP>
> ```

พอเข้าได้แล้ว (prompt เป็น `opc@...`) ติดตั้งของที่ต้องใช้ (Oracle Linux ใช้ `dnf`):

```bash
sudo dnf install -y python3 python3-pip git
git clone https://github.com/maiiqqq/manga-monitor.git
cd manga-monitor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

---

## Phase 3 — ใส่ token แล้วลองรัน

สร้างไฟล์ `.env` บน VM (บอทอ่านค่าจากไฟล์นี้อัตโนมัติ):

```bash
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=ใส่_token_ของคุณ
TELEGRAM_CHAT_ID=ใส่_chat_id_ของคุณ
EOF
```

ลองรันดูก่อน (Ctrl+C เพื่อหยุด):

```bash
./venv/bin/python bot_realtime.py
```

ถ้าขึ้น `Real-time bot started ...` = ใช้ได้ ลองพิมพ์ `/help` ในบอท จะตอบทันที

---

## Phase 4 — ให้รันตลอด 24 ชม. ด้วย systemd

```bash
sudo tee /etc/systemd/system/manga-bot.service > /dev/null <<EOF
[Unit]
Description=Go-Manga Real-time Bot
After=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/manga-monitor
ExecStart=/home/opc/manga-monitor/venv/bin/python /home/opc/manga-monitor/bot_realtime.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now manga-bot
sudo systemctl status manga-bot     # ดูว่า active (running)
```

ดู log สด ๆ:
```bash
journalctl -u manga-bot -f
```

---

## Phase 5 — ปิด GitHub Actions (สำคัญ!)

ไม่งั้นทั้ง VM และ Actions จะ scrape+แจ้งเตือนซ้ำกัน

**วิธีง่ายสุด:** ไปที่ repo บน GitHub → แท็บ **Actions** → เลือก workflow
"Go-Manga Monitor" → ปุ่ม **⋯ (มุมขวา)** → **Disable workflow**

---

## อัปเดตโค้ดในอนาคต
```bash
cd ~/manga-monitor && git pull && sudo systemctl restart manga-bot
```

## คำสั่งดูแลที่ใช้บ่อย
```bash
sudo systemctl restart manga-bot    # รีสตาร์ท
sudo systemctl stop manga-bot       # หยุด
journalctl -u manga-bot -n 50       # ดู log ล่าสุด 50 บรรทัด
```
