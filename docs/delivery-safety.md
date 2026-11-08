# การส่งแจ้งเตือนและ Preview หลัง GM-007

GM-007 แก้ CHECK_URLS และใช้ delivery ledger สำหรับ chapter card และสรุปเรื่องโปรด
เพื่อหยุดการส่งซ้ำอัตโนมัติเมื่อไม่ทราบผล การเปลี่ยนนี้ไม่รับรอง exactly-once
และไม่ได้อนุญาตหรือดำเนินการ replay รายการจริง

## พฤติกรรมปัจจุบัน

| เหตุการณ์ | ผล |
|---|---|
| Telegram ยืนยันพร้อม message ID | เก็บ receipt ก่อนให้ caller เลื่อน baseline |
| Telegram ปฏิเสธชัดเจน | เก็บ rejected; baseline ไม่เลื่อน; รอบหลัง retry ได้ |
| Timeout, HTTP 5xx, payload ผิดรูปแบบ หรือไม่มี message ID | คง unknown; ไม่ retry อัตโนมัติ |
| รูปถูกปฏิเสธชัดเจน | อนุญาต fallback เป็นข้อความภายใต้ card identity เดียวกัน |
| ผลส่งรูปไม่ทราบ | พัก card; ไม่ส่งข้อความ fallback |
| ส่งสำเร็จแล้ว state save หรือ push ล้มเหลว | รอบถัดไปอ้าง receipt เดิม ไม่ส่ง card เดิมอีก |
| บันทึก intent ไม่ได้ | ไม่เรียก Telegram |
| Telegram รับแล้ว แต่บันทึก receipt ไม่ได้ | intent ที่เก็บไว้ยังเป็น unknown; พักตรวจสอบ |
| Summary ถูกปฏิเสธ | retry เฉพาะ summary ในรอบหลัง แม้ไม่มี card ใหม่ |
| Summary ไม่ทราบผล | พัก summary; ไม่ส่ง card หรือ summary ซ้ำเพื่อชดเชย |

`CHECK_URLS` เลื่อน baseline เมื่อ send_update ยืนยันว่าทุกตอนในรายการมี receipt
ที่เก็บสำเร็จแล้วเท่านั้น รวมกรณีเป็น receipt เดิมจากรอบก่อน ข้อความ log ว่า notified
ไม่ใช่หลักฐานส่งใหม่ในรอบนั้น ให้ใช้ ledger/ข้อความจริงประกอบ

## Ledger และผลต่อการให้บริการ

Ledger คือ `delivery_state.json`, schema version 1; แยกจาก monitor baseline,
bookmarks และ Telegram command offset แต่ละ key เป็น hash ของ scope + kind +
manga URL + เลขตอน ใช้ identity รายตอนแม้ card รวมหลายตอน และใช้ kind เดียว
สำหรับ photo/text ของ card ส่วน favorite มี identity แยก
scope ใช้ hash ของ bot ID ที่คงเดิมเมื่อเปลี่ยน token กับ chat ID
ledger ไม่เก็บ token, chat ID แบบตรงตัว หรือเนื้อความ Telegram เต็มข้อความ
เก็บ URL/title/chapter และข้อมูลเรื่องโปรด จึงไม่ใช่ข้อมูลส่วนตัวใน public repository

ก่อนส่งจะเขียนสถานะ unknown พร้อม claim ID ให้สำเร็จก่อน หลังส่งจึงบันทึก accepted
พร้อม method/message ID หรือ rejected เฉพาะรายการที่ claim รอบนั้นยังเป็นเจ้าของ
ถ้า batch มีตอนที่ accepted แล้ว จะส่งเฉพาะตอนที่ยังไม่เคยส่งหรือเคยถูกปฏิเสธ
ถ้ามี unknown ค้าง จะไม่เลื่อน baseline ข้าม update นั้น แม้ตอนใหม่อื่นส่งสำเร็จแล้ว

**Actions:** ใช้ Contents API บน `master` และ GITHUB_TOKEN เดิมที่มี contents:write
อ่าน blob SHA แล้วเขียนแบบ compare-and-set; conflict อ่านใหม่ได้ไม่เกิน 3 ครั้ง
unknown ที่ persisted ก่อน POST จึงยังอยู่แม้ runner ถูกยกเลิกหรือขั้น push state ล้มเหลว
ไม่มีการเพิ่ม PAT หรือ permission และไม่ fallback ไปเก็บเฉพาะ local ใน Actions
missing file/token, permission denial, redirect, invalid ledger และผลเขียนกำกวม
ทำให้พักการส่ง ส่วนการเก็บ monitor/bookmark/offset ตอนจบยังทำตามขั้นตอนเดิม
และไม่ stage ledger เก่าใน checkout ทับข้อมูลใหม่บน remote

**VM:** ใช้ไฟล์ local ตำแหน่งเดียวกัน พร้อม file lock, fsync และ atomic replace
ต้องอยู่บน persistent disk และมี backup ที่ตรวจสอบได้ การทำสำเนา checkout ไป VM ใหม่
ด้วย ledger ว่างหรือเก่าทำให้หลักฐานสูญหาย ห้าม restore ไฟล์ ledger เก่าหรือใช้ git reset
ทับไฟล์นี้ระหว่างอัปเดต ห้ามเปิด VM และ Actions ส่งพร้อมกันเพราะใช้คนละ store

การเขียน ledger เพิ่ม GitHub requests/commits ต่อการส่งแต่ละ batch เมื่อ GitHub หรือ
storage ขัดข้อง ระบบจะพักแจ้งเตือนเพื่อเลี่ยงการส่งซ้ำ แทนการส่งก่อนแล้วหวังว่า push จะสำเร็จ
ไฟล์จำกัด **750,000 bytes** และไม่มีการลบ receipt อัตโนมัติ เมื่อใกล้เต็มต้องวางแผน
ย้าย/แบ่งเก็บหลักฐานแบบรักษา identity ก่อนเต็ม; เมื่อเต็มระบบหยุดส่ง ไม่ล้าง ledger
ความจุนี้มีขอบเขตและอาจเต็มเร็วใน repository ที่แจ้งจำนวนมาก ต้องติดตามการเติบโต
ก่อนเปิดใช้งานยาวต่อเนื่อง ไม่เพิ่ม cap โดยไม่ประเมินขอบเขต Contents API

Summary ที่ค้างใช้ metadata ที่ queue ตอน card สำเร็จ และส่งครั้งละหนึ่ง batch
ไม่เกินประมาณ 3,500 ตัวอักษรต่อรอบเพื่ออยู่ภายในขนาดข้อความ Telegram
การยกเลิก bookmark ภายหลังไม่ลบ summary ที่ค้างจาก card เดิมโดยอัตโนมัติ

## Preview ที่ใช้ได้ทุกโหมด

`BACKFILL_DRYRUN=true` (หรือ 1/yes) เลือก preview ก่อนตรวจ Telegram credentials
หรือสร้าง notifier ทั้งใน `bot_realtime.py` และ entrypoint `go_manga_monitor.py`

- มี CHECK_URLS: ดู detail เป้าหมายโดยไม่ส่งหรือเปลี่ยน baseline
- ไม่มี CHECK_URLS แต่มี BACKFILL_TODAY: แสดงรายการตามวันที่
- มีเพียง dry-run flag: แสดง candidate จาก normal scan โดยใช้ baseline ใน memory
- Flags หลายตัวไม่ทำให้หลุดไปส่งจริง; preview ไม่ใช้ Telegram token ก็ได้
- Workflow ข้าม snapshot/persist/upload และส่ง flag ให้ self-chain helper
  ซึ่งคืน disabled ก่อนอ่าน PAT จึงไม่สร้างรอบลูก normal

Preview ยัง scrape เว็บไซต์มังงะ จึงไม่ใช่ offline execution หากต้องการ offline
ให้รัน unittest ที่ mock HTTP ตามคำสั่งท้ายเอกสาร อย่าใช้ workflow dispatch จริง
เป็นวิธีทดสอบโค้ดระหว่างพัฒนา

## ตรวจหลัง deploy และ reconcile

ก่อนเปิดใช้ GM-007 ต้องให้รอบเก่าที่ใช้ code ไม่มี ledger จบหรือหยุดอย่างควบคุมได้
ตรวจ scheduled/chained/manual runs และ VM ทั้งหมด การ merge โค้ดอย่างเดียวไม่ทำให้
process เก่าที่กำลังรันได้รับ guard ใหม่ทันที ไม่เปิด writers ที่ใช้คนละ store พร้อมกัน
ตรวจว่า ledger ล่าสุดบน master เป็น schema ที่ถูกต้องและ GITHUB_TOKEN เดิมเขียนได้
โดยตรวจผลตามรอบที่ได้รับอนุญาต ไม่ส่งข้อความทดสอบเพิ่มเติมเอง

เมื่อพบ unknown ให้บันทึก scope, key, URL/chapter/kind, claim ID และ run/commit
ที่เกี่ยวข้อง แล้วเทียบข้อความจริงในห้องเป้าหมาย:

1. พบข้อความตรงรายการ: เสนอ diff บน ledger ล่าสุดเป็น accepted พร้อม method/message ID
2. ยืนยันว่าไม่ส่งครบทุก attempt รวม photo/fallback: เสนอ diff เป็น rejected เพื่อให้ retry
3. ไม่พอจะยืนยัน: คง unknown ไม่ลบ key และไม่เปลี่ยนเป็น rejected เพราะรอนานแล้ว

การเขียน diff ดังกล่าวและการเปิดส่งจริงต้องได้รับอนุญาตแยก ยังไม่มี CLI สำหรับ
reconcile/replay ใน GM-007 ห้ามลบ ledger, เปลี่ยน scope หรือคืน snapshot เก่าเพื่อบังคับส่ง
History ก่อน GM-007 ไม่มี receipt; ห้ามสร้าง accepted จาก baseline เพียงอย่างเดียว
รายการ historical unknown ยังใช้ [คู่มือกู้คืน](recovery-replay.md) และหลักฐานจริง
Backfill หลัง GM-007 เคารพ accepted/unknown เช่นเดียวกับ normal จึงไม่บังคับส่งซ้ำ

ตรวจ bot/snapshot/persist/self-chain แยกกันตาม GM-005; job สีเขียวไม่ได้แปลว่า
ไม่มี unknown ตรวจ log การพักส่งและสถานะ ledger ด้วย ไม่มีการพิสูจน์ delivery
จากคำว่า UPDATE หรือ baseline เพียงอย่างเดียว

## ขอบเขตและ rollback

Guard นี้ครอบคลุม card และ favorite ของ code ใหม่ที่ใช้ store เดียวกัน
ไม่ครอบคลุม command replies, callbacks, ข้อความที่ส่งมือ, bot รุ่นเก่า, การเปลี่ยน URL/
เลขตอน/target identity, store คนละชุด หรือการลบ/ย้อนหลักฐานโดยผู้ดูแล
ความผิดพลาดหลัง intent แต่ก่อน HTTP อาจทำให้รายการที่ยังไม่เคยส่งถูกพักเป็น unknown
นี่คือข้อแลกเปลี่ยนเพื่อหลีกเลี่ยงการ retry โดยไม่มีหลักฐาน ไม่ใช่ exactly-once

Rollback ต้องหยุด sender ที่เกี่ยวข้องก่อน รักษา ledger และ state ล่าสุดไว้
revert เฉพาะ code/config พร้อมตัดสินใจว่าจะคงหยุดส่งต่อหรือใช้ guard รุ่นใด
การย้อนกลับไป code เดิมจะเปิดช่องส่งซ้ำอีก ห้าม revert ledger หรือ baseline/offset
และห้ามถือว่า revert state ถอนข้อความ Telegram ได้

## ตรวจ offline

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -v
```

Tests ใช้ state/Git ชั่วคราว, real notifier/ledger code และ mock HTTP เท่านั้น
ครอบคลุม accepted/rejected/unknown, receipt/intent loss, restart, rejected push,
CAS race, ข้อมูลผิดรูปแบบ, capacity, preview และคิว summary ที่ค้างข้ามรอบ
ไม่มี Telegram, GitHub writes หรือ workflow dispatch จริงระหว่าง tests

API reference: [GitHub repository contents](https://docs.github.com/en/rest/repos/contents)
(blob SHA สำหรับ update, Base64 content และ contents:write)
