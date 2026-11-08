# ผลตรวจรวม GM-001–007

วันที่: 8 พฤศจิกายน 2569 (Asia/Bangkok)

## สถานะการรวมงาน

GM-006 PR #30 อยู่บน master แล้วก่อนเริ่มตรวจ จึงไม่ได้ merge ซ้ำ
GM-001–007 อยู่ครบ: #23, #26, #25, #27, #28, #30, #29 ตามลำดับ ticket
GM-002 #24 รวมเข้า dependency branch ก่อน แล้วเข้าถึง master ผ่าน #26

เริ่มตรวจ master `43905ae8b6baddece4c698f5cb256ff18457e1b9` และตรวจซ้ำหลังพักงาน
ที่ `6b7b6566de1676b5f4dc56a2cd6e5f7bd245a2ac`
ระหว่างสอง SHA นี้เปลี่ยนเฉพาะ monitor_state.json และ delivery_state.json
source/config/docs/tests ไม่เปลี่ยน ตรวจ hash Git blob ครบ 39 ไฟล์ ณ SHA ล่าสุด
โดยอ่าน runtime state แยกและไม่เขียนข้อมูลนั้นกลับ repository

**ผลรวม:** baseline ปัจจุบันผ่าน 128 tests แต่การตรวจเพิ่มพบ Important edge case
GM-007-F1 ใน CHECK_URLS วันที่อนาคต จึงมี patch และ 3 tests เพิ่มใน PR ที่แนบรายงานนี้
อย่าตีความว่า baseline master แก้เคสนี้แล้วจนกว่า patch จะ merge

## ผลราย ticket

| Ticket | สิ่งที่ตรวจ | ผลและขอบเขต |
|---|---|---|
| GM-001 | push error classification, exit status, SHA/attempt, redaction | ผ่าน fixture และทบทวน code; ไม่เรียก internal error ว่า remote moved โดยไม่มี NFF |
| GM-002 | original bytes, JSON validation, secret screening, export allowlist, artifact gates | ผ่าน tests และ helper กับสำเนา state ล่าสุด; ไม่กระตุ้น upload artifact จริง |
| GM-003 | bounded retry/jitter, permission stop, NFF merge, uncertain push confirmation, outer timeout | ผ่าน tests ที่ใช้ Git ชั่วคราวและ fault injection; ไม่มี force push |
| GM-004 | dispatch POST ครั้งเดียว, correlated GET, accepted/unknown/failed/disabled, preview guard | ผ่าน tests; live job ล่าสุดแสดง accepted/http204; ไม่สั่ง dispatch เพิ่ม |
| GM-005 | แยก sent/rejected/unknown, candidate recovery, rollback และ preview limitations | ทบทวน runbook และ regression; ไม่มี replay/restore จริง |
| GM-006 | permissions/rules, job-step timing, merge deletion/concurrent edit policies | audit อยู่บน master; ไม่พบเหตุให้เพิ่มสิทธิ์/เปลี่ยน concurrency ในขอบเขตข้อมูลที่ตรวจ |
| GM-007 | CHECK_URLS success gate, global preview, CAS intent/receipt, unknown hold, summary queue | tests เดิมผ่านและพบ receipt จริง 8 events; F1 ที่พบเพิ่มต้อง merge patch ก่อนถือว่าปิดเคสวันที่อนาคต |

## หลักฐาน runtime ที่อ่านอย่างเดียว

- Frozen state ล่าสุดเป็น JSON object ที่อ่านได้: monitor 174 เรื่อง, bookmarks 28 รายการ,
  bot state 2 fields; ไม่คัดลอกชื่อเรื่องหรือ target ลงรายงานนี้
- Ledger schema 1 ผ่าน validator: **8 card events เป็น accepted ทั้งหมด**,
  method sendPhoto, มี message ID แบบ integer ที่ถูกต้องทุก event,
  unknown/queued/rejected ณ snapshot นี้เป็น 0
- Ledger ขนาด 3,501 bytes จาก cap 750,000 bytes ไม่ได้หมายถึงความจุไม่จำกัด
- [commit d2cfae22](https://github.com/maiiqqq/manga-monitor/commit/d2cfae22d5da39f2fe5e81bc105fb7c555769713)
  มี unknown 1/accepted 7 และ
  [commit c45facbf](https://github.com/maiiqqq/manga-monitor/commit/c45facbf4bf17e20a265e07318208d99e8d45c57)
  เปลี่ยน event เดียวกันเป็น accepted จนครบ 8 เป็นหลักฐาน write-ahead/receipt ที่เกิดจาก
  workflow อัตโนมัติจริง ไม่ใช่การส่งที่ผู้ตรวจสั่งขึ้นมา
- [run 42495530887](https://github.com/maiiqqq/manga-monitor/actions/runs/42495530887),
  job 128776946814 จบ success; bot/snapshot/persist/self-chain steps success
  log execution ที่ตรวจมี Contents:write และ self-chain accepted/http204
  ไม่มี matched ERROR แต่ persistence ระบุ No state changes to commit และไม่มี UPDATE
  จึงไม่ใช้รอบนี้เพียงรอบเดียวเป็นหลักฐานว่ามีการส่ง card ใหม่หรือ push ใหม่
- Failure query ที่อ่านคืน 17 รายการ ล่าสุดยังเป็นเหตุเดิม 42128663395
  ไม่พบ failure ใหม่ในผลที่อ่านได้ ไม่รับรองทุก historical attempt หรือรอบในอนาคต
- Snapshot helper รันกับสำเนา state 3 ไฟล์ใน temporary directory: valid/included ทั้งหมด,
  bytes ต้นทางไม่เปลี่ยน; ไม่อัปโหลดหรือ restore artifact จริง

Receipt ยืนยันผลตอบกลับที่ระบบบันทึกจาก Telegram API ไม่ได้ตรวจห้องสนทนาโดยอิสระ
ไม่ยืนยันว่าผู้ใช้เห็น/อ่านข้อความ และไม่รับรอง exactly-once

## Finding GM-007-F1: CHECK_URLS ส่งตอนอนาคตเมื่อไม่มี visible chapter

ระดับ: Important; พบจาก author review ระหว่างตรวจรวม ไม่ใช่ failure จาก production

ก่อนแก้ `visible=[]` ยัง fallback เป็น `manga.chapters[0]` เมื่อเป็น first check หรือ
baseline สูงกว่าหมายเลขไซต์ จึงส่งตอนที่ลงวันที่พรุ่งนี้ และสร้าง/ลด baseline ได้
แม้ normal detection จะกรองวันที่อนาคตอยู่แล้ว

Reproduction ใช้ real bot/state/notifier logic กับ mocked HTTP และวันที่จำลอง:
today=2026-11-08, chapter 11 date=2026-11-09, state ว่าง → mock ส่ง 1 ครั้งและ
baseline กลายเป็น 11 อีกกรณี baseline 20 ก็อาจลดเหลือ 11

Patch จำกัดเฉพาะ guard ก่อนเลือก newest: ถ้าไม่มี visible numbered chapter ให้ข้าม
โดยไม่ส่งหรือแก้ baseline และยังคง request delay ส่วนกรณีมีทั้งตอนวันนี้กับอนาคต
เลือกเฉพาะตอนที่ visible เหมือนเดิม ไม่เปลี่ยน ledger/state schema หรือ workflow

เพิ่ม tests ก่อน implementation:

1. First-seen/future-only: ไม่ส่งและไม่สร้าง baseline — RED ก่อนแก้
2. Renumbered/future-only: ไม่ส่งและไม่ลด baseline — RED ก่อนแก้
3. Mixed visible/future: ส่งเฉพาะตอนที่ visible — ผ่านทั้งก่อนและหลังแก้

หลัง patch ทั้ง 3 cases ผ่าน; full suite **131 tests ผ่านใน 43.738s** พร้อม summary ครบ
Python compile, Bash syntax และ diff check ผ่าน; ตรวจ remote diff ก่อนส่งมอบ PR
ไม่ได้ merge patch หรือใช้ CHECK_URLS จริงระหว่างตรวจนี้

## วิธีตรวจและข้อจำกัดการ review

```bash
python -m unittest discover -v
python -m compileall -q bot_realtime.py go_manga_monitor.py delivery_ledger.py merge_state.py .github/scripts tests
bash -n .github/scripts/persist_state.sh
git diff --check
```

Fresh source-aligned baseline: 128 tests ผ่าน 44.046s หลัง resume
ใช้ runtime Python 3.12 ในสภาพแวดล้อมตรวจ; workflow กำหนด Python 3.11
ผล full suite หลัง patch: 131 tests ผ่าน 43.738s; บันทึกซ้ำใน ticket record/PR description

งานแต่ละ ticket มี independent review ก่อนส่ง PR เดิม แต่ fresh independent
whole-system review รอบนี้เริ่มไม่ได้เพราะข้อจำกัดการใช้งาน จึงจบรอบรวมด้วย author
self-review + regression tests ไม่อ้างว่ามี independent review รอบใหม่หรือหลบข้อจำกัด

## ประเด็นที่ยังติดตาม

- [คิวเก่า 42267403160](https://github.com/maiiqqq/manga-monitor/actions/runs/42267403160)
  ยัง queued บน code ก่อน GM-007; ไม่ได้ cancel/rerun ต้องตรวจ inputs/ความตั้งใจก่อนจัดการ
- Detailed protection ยังอ่านไม่ได้ผ่าน integration (403); ไม่แปลว่า workflow token ไม่มีสิทธิ์
  และไม่รับรอง global/manual/VM writers นอกข้อมูลที่ audit ตรวจ
- Ledger ต้องมีแผนขยาย/เก็บรักษาก่อน cap; storage ใช้ไม่ได้หรือเต็มทำให้พักการส่ง
- Minor ของ GM-006 เรื่องเก็บ correlation title/request ID ใน evidence ให้ครบยังค้าง
- รักษา unknown เพื่อ reconcile และรักษา evidence ระหว่าง rollback; ไม่ย้อน baseline/offset
  หรือใช้การลบ state/ledger เป็นวิธีบังคับ replay

นอกจาก patch F1 ที่เสนอ ไม่มีการเปลี่ยน permissions/PAT/protection/concurrency,
สั่ง workflow, ส่ง Telegram เอง, replay หรือ restore state จริง
