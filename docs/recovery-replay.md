# กู้คืน state และประเมิน replay (GM-005)

คู่มือนี้อ้างอิงพฤติกรรมหลัง GM-004 (PR #27) และการทดสอบข้อมูลจำลองใน
`tests/test_recovery_scenarios.py` ไม่มีการกู้คืนหรือส่ง Telegram จริงใน GM-005
การทดสอบยืนยันข้อจำกัดปัจจุบัน ไม่ได้เพิ่มระบบป้องกันข้อความซ้ำใน production

**ยังไม่ควร replay จริง:** ปัจจุบันไม่สามารถรับรองการส่งครั้งเดียวได้ โดยเฉพาะ
รายการที่ไม่รู้ว่า Telegram รับไปแล้วหรือยัง ต้องแยกหลักฐานรายรายการและทำ GM-007
ตามท้ายคู่มือนี้ก่อนเสนอ replay ส่วนการกู้คืน state ต้องผ่านการตรวจ candidate แยกต่างหาก

## 1. แยกสาเหตุและผลลัพธ์ก่อน

ตรวจ run ID, run attempt, branch, head SHA, เวลา และ inputs ของรอบที่ล้มเหลว
แยกผล `run_bot`, `snapshot_state`, `persist_state`, artifact upload และ self-chain
จาก log/step outcome จริง อย่าใช้สีรวมของ job เป็นหลักฐานว่าส่งครบ

- `run_bot` ใช้ `continue-on-error: true` เพื่อให้ยังบันทึก state ได้
  ต้องดู `outcome` เดิม ไม่ใช่เพียง conclusion หลังใช้ continue-on-error
- ใน normal loop มีการจับ exception ของ scrape cycle แล้ววนต่อจนหมดเวลา
  จึงอาจจบ process ด้วย exit 0 ทั้งที่มี `[ERROR] scrape cycle`
- `[UPDATE]` เกิดก่อนส่ง และ `[CHECK] ... notified` พิมพ์ได้แม้การส่งถูกปฏิเสธ
  ทั้งสองข้อความใช้ยืนยันการส่งไม่ได้
- Persistence failure หลังส่งสำเร็จไม่เท่ากับ bot ไม่ได้ส่ง; self-chain accepted
  ก็ไม่ยืนยันว่ารอบลูกเริ่มหรือทำงานสำเร็จ ดู [self-chain.md](self-chain.md)

| เส้นทางจริง | พฤติกรรม state / การส่ง | ผลต่อการกู้คืน |
|---|---|---|
| Normal, ส่งสำเร็จและ save สำเร็จ | baseline เลื่อนหลัง `send_update` คืน True | ถ้า push ล้มเหลว snapshot อาจใหม่กว่า master |
| Normal, Telegram ปฏิเสธชัดเจน | baseline ของรายการนั้นไม่เลื่อน | พิจารณาส่งใหม่ได้เมื่อไม่มี attempt อื่นที่กำกวม |
| Telegram รับแล้ว response หาย | notifier คืน False; baseline อาจยังเก่า | restart อาจส่งซ้ำ ต้องจัดเป็น unknown |
| ส่งสำเร็จแต่ save ล้มเหลว | ไม่มี state ที่บันทึกสำเร็จให้พึ่งพา | อาจค้างไฟล์เก่าหรือไฟล์เขียนไม่ครบ; snapshot อาจใช้ไม่ได้ |
| รูปถูกรับแล้ว response หาย | fallback ส่งข้อความทันที | อาจมีรูปและข้อความซ้ำในรอบเดียว |
| CHECK_URLS | ไม่ตรวจ bool จาก send_update ก่อนเลื่อน baseline | baseline ใหม่ไม่ยืนยันว่าส่งแล้ว; อาจข้ามรายการที่ส่งไม่สำเร็จ |
| Backfill | ส่งตามวันที่โดยไม่อ้าง baseline และไม่เลื่อน baseline | ทำซ้ำอาจส่งรายการเดิมซ้ำ |
| Favorites summary | ส่งแยกหลัง card; ไม่เก็บผลราย summary | card สำเร็จไม่ได้แปลว่า summary สำเร็จ; ไม่ replay card เพื่อชดเชย summary |

State เป็น baseline สำหรับตรวจตอนใหม่ ไม่ใช่ delivery ledger ไม่มี receipt/message ID
ที่บันทึกถาวรเพื่อจับคู่กับแต่ละรายการ และบางเส้นทางเลื่อน baseline โดยไม่ส่ง
เช่น first-seen เรื่องเก่า หรือไม่มีตอนใหม่ที่เข้าเกณฑ์วันที่

## 2. เก็บและตรวจหลักฐานแบบอ่านอย่างเดียว

1. เก็บ metadata และ log เฉพาะที่จำเป็น โดยไม่คัดลอก token, chat ID หรือ `.env`
   ลง ticket สาธารณะ
2. หา artifact `manga-state-recovery-<run_id>-<run_attempt>` จากรอบนั้น
   อายุเก็บ 7 วันและอาจอัปโหลดไม่สำเร็จ ดู [state-recovery.md](state-recovery.md)
3. เปิด `manifest.json` ตรวจ run/attempt, status, `artifact_included`, byte count
   และ SHA-256 ของไฟล์ที่มีจริงกับ bytes เดิม ห้ามแปลง JSON ก่อนตรวจ hash
   Hash ยืนยันความตรงกันของไฟล์กับ manifest ไม่ได้ยืนยันการส่ง Telegram
4. ถ้าไฟล์หาย, invalid, ถูกคัดออกเพราะข้อมูลอ่อนไหว หรือ hash ไม่ตรง ให้หยุดใช้ไฟล์นั้น
   ห้ามแทนด้วย `{}` และห้ามเดาว่ารายการที่ไม่มีใน artifact ยังไม่ส่ง
5. อ่าน state ล่าสุดบน `master` พร้อมบันทึก commit SHA และอ่าน base ของรอบเดิม
   ต้องเป็น base ที่ bot ใช้จริง; หากมีการ sync checkout ให้ตรวจ log ก่อน
6. เทียบประวัติ Telegram ในห้องเป้าหมายกับเรื่อง/ตอน/รูป/ข้อความ/summary ของแต่ละ attempt
   อย่าส่งข้อความทดลองเพื่อพิสูจน์ว่าเชื่อมต่อได้ ไม่มีข้อความใน log หรือหาไม่พบใน chat
   ยังไม่พอที่จะสรุปว่าไม่เคยส่ง

Artifact ของ public repository ไม่ใช่พื้นที่ส่วนตัว และอาจมีรายการ bookmark
originals ที่ถูกคัดออกเหลืออยู่แค่ runner และสูญหายเมื่อ runner ถูกทำลาย
ถ้าไม่มีหลักฐานเพียงพอ ให้บันทึก unknown แทนการสร้าง snapshot ทดแทน

## 3. ตารางตัดสินใจรายรายการ

ใช้หนึ่งแถวต่อ URL + ตอน/ช่วงตอน + ห้องเป้าหมาย + ประเภทข้อความ
ระบุ attempt ทั้งหมดที่เกี่ยวข้อง เพราะ batch card หนึ่งใบอาจรวมหลายตอน

| ช่องที่ต้องบันทึก | ตัวอย่างข้อมูลจำลอง |
|---|---|
| รายการ | example.invalid/manga/fixture, ตอน 11, card |
| Run / attempt / base SHA | ระบุค่าจริงจากหลักฐานรอบนั้น |
| Base / snapshot / latest master | 10 / 11 / 12 |
| หลักฐานส่ง | message link/ID ที่ตรวจได้ หรือ explicit rejection ของทุก attempt |
| ผลจัดประเภท | confirmed_sent / confirmed_not_sent / unknown |
| ข้อเสนอและผู้ตรวจ | คง baseline 12, ไม่ replay; ระบุเหตุผลและผู้ตรวจ |

- **confirmed_sent:** มีข้อความที่ตรงรายการและห้องเป้าหมายตรวจสอบได้
  ไม่ใช้ baseline หรือข้อความ log ว่า notified แทนหลักฐานส่ง
- **confirmed_not_sent:** มีหลักฐานปฏิเสธชัดเจนครบทุก attempt ที่เกี่ยวข้อง
  และไม่มี timeout, การส่งรูปที่กำกวม หรือ fallback/รอบอื่นที่อาจรับไปแล้ว
- **unknown:** หลักฐานไม่ครบ, timeout, save/push ล้มเหลวโดยไม่มี receipt,
  หรือข้อมูลขัดกัน ให้พักรายการ ไม่ replay และไม่เลื่อน baseline เพื่อกลบความไม่แน่นอน

หากล่าสุด master อยู่ตอน 12 และ snapshot ตอน 11 ห้ามถอยเป็น 11 แม้ snapshot
ผ่าน validation แล้ว รายการ renumber, เลขทศนิยม, วันที่ผิดปกติ และการลบ key
ต้องตรวจเอง ไม่ใช้กฎเลือกเลขมากสุดตัดสินแทนความหมายของข้อมูล

## 4. สร้าง candidate ในสำเนาเท่านั้น

การทำจริงต้องมีช่วงหยุด writers ที่ผู้ดูแลอนุมัติและตรวจว่าไม่มี run/VM/การแก้มือ
กำลังเขียนอยู่ รวมถึงรอบ queued และ self-chain ที่อาจตามมา การอ่าน snapshot
ไม่ต้องหยุดบริการ แต่ห้ามเขียน state กลับขณะมี writers ที่ยังควบคุมไม่ได้
GM-005 ไม่ได้หยุด run, disable workflow หรือเปลี่ยน secrets ใด ๆ

สร้าง candidate ใน branch/ไดเรกทอรีแยกจาก checkout ที่ bot ใช้
นำเฉพาะรายการที่มีหลักฐานและการตัดสินใจแล้วมาเสนอ diff เทียบ latest master
เก็บ base/snapshot/latest originals แยกไว้โดยไม่แก้ bytes เดิม

`merge_state.py` เป็นตัวช่วย three-way merge ไม่ใช่คำสั่ง restore ที่ปลอดภัยโดยตัวมันเอง:

- รับเฉพาะ base, ours, theirs ที่ตรวจว่าเป็น JSON object และตรงกับรอบจริงแล้ว
  ตัว helper ยอมรับ base/theirs ที่อ่านไม่ได้เสมือนไม่มีข้อมูล จึงห้ามใช้ไฟล์หายหรือ `-`
  สำหรับการกู้คืนที่ต้องรักษาข้อมูลใหม่
- helper เขียนทับ **target (ours)** จึงใช้ได้เฉพาะไฟล์สำเนาที่ตั้ง basename ถูกต้อง
  (`monitor_state.json`, `bookmarks.json`, `bot_state.json`) ไม่ใช่ state ที่กำลังใช้งาน
- เมื่อ monitor key ชนกันทั้งสองฝั่ง resolver เลือก chapter ที่สูงกว่า แล้วดูวันที่/เวลา
  แต่กฎนี้ไม่ได้ทำงานทุกกรณี: ถ้า remote ไม่เปลี่ยนจาก base จะเลือก ours แม้ต่ำกว่า
  ต้องตรวจ candidate ทุก key เทียบ latest อีกครั้ง ห้ามอ้างว่า merge รับรองไม่ถอยหลัง
- ตรวจ `telegram_offset` ว่าเป็น integer ที่ถูกต้องและไม่ต่ำกว่าค่าที่เชื่อถือได้ล่าสุด
  การถอย offset อาจประมวลผลคำสั่งเก่าซ้ำ; อย่า restore ทั้ง `bot_state.json` จากอดีต
- Bookmarks เป็นการกระทำผู้ใช้; conflict อาจเลือก ours และการลบอาจชนะ
  อย่านำ bookmark เก่าทับรายการใหม่ ให้ตรวจ diff/การเพิ่มลบแยก

ก่อนเสนอ state PR ต้องตรวจ latest master SHA ซ้ำและสร้าง candidate ใหม่เมื่อมีการขยับ
ห้าม force push หรือ reset master ย้อนหลัง เก็บข้อเสนอที่ยังไม่ยืนยันนอก runtime state
ไม่เลื่อนทั้งเรื่องถึงตอน N หากยังมีตอน/ประเภทข้อความก่อนหน้าที่ไม่ทราบผล

## 5. ตรวจ offline ก่อนเสนอการทำจริง

จาก checkout แยก ติดตั้ง dependencies แล้วรัน:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest tests.test_recovery_scenarios -v
python3 -m unittest discover -v
```

Scenario tests ใช้โมดูล bot จริงกับ synthetic list/detail, mock Telegram ที่ HTTP boundary,
token/chat จำลอง, state ใน temporary directory และ Git remote บน local filesystem
ไม่มีการอ่าน/เขียน state จริง ไม่มี HTTP ไป Telegram/เว็บมังงะ/GitHub
การติดตั้ง dependencies ต้องเข้าถึง package registry; ตัว tests ไม่ต้องใช้เครือข่ายภายนอก

**อย่าใช้ workflow backfill dry-run เป็นการตรวจ offline:**

- `BACKFILL_DRYRUN=true` ข้าม card/summary เฉพาะเมื่อเข้าทาง `BACKFILL_TODAY`
  แต่ก่อนเลือกโหมด bot ยังเรียก `setMyCommands` และ `getChat`
- flag เดี่ยว ๆ ไม่หยุด normal loop; `CHECK_URLS` มีลำดับก่อน backfill และยังส่งได้
- self-chain ใช้ `if: always()` และเมื่อมี PAT จะ POST ต่อได้ โดยรอบลูกไม่ได้รับ
  backfill/dry-run flags จึงอาจกลายเป็น normal monitor ที่ส่งจริง

อย่า dispatch/rerun หรือรัน `bot_realtime.py` โดยใช้ credentials จริงเพื่อ preview
ผล tests ที่ผ่านหมายถึงพฤติกรรมตรงกับคู่มือ รวมทั้งช่องโหว่ที่ตั้งใจบันทึกไว้
ไม่ได้หมายความว่าระบบแก้ duplicate/missed delivery แล้ว

## 6. เงื่อนไขก่อน replay, หลัง deploy และ rollback

ก่อนเสนอ replay ต้องมี GM-007 ที่ผ่านทดสอบ fault injection, ตารางหลักฐานครบทุกรายการ,
candidate state ที่ไม่ทับข้อมูลใหม่, การควบคุม writers, รายการส่งที่จำกัดชัดเจน
และผู้ใช้อนุญาตรายการส่งจริงนั้นโดยเฉพาะ รายการ unknown ต้องแยกตัดสินใจ
หากยอมรับความเสี่ยงส่งซ้ำ ให้บันทึกการตัดสินใจ ไม่เปลี่ยนชื่อเป็น confirmed_not_sent

หลัง deploy GM-005 ตรวจว่า diff มีแค่คู่มือ/tests/development dependencies และ test ผ่าน
ไม่จำเป็นต้อง dispatch bot เพื่อพิสูจน์การเปลี่ยนเอกสารนี้ หากภายหลังมีการกู้คืนที่อนุมัติแล้ว
ให้ตรวจ diff ที่ master, ความต่อเนื่องของ baseline/offset, ผล bot/snapshot/persist แยกกัน,
หลักฐาน Telegram รายรายการ และรอบที่ self-chain สร้างก่อนเปิด writers อีกครั้ง
ถ้าพบการส่งซ้ำหรือไม่ทราบผลใหม่ ให้หยุดการส่งเพิ่มตามแผน incident ที่อนุมัติไว้

Rollback GM-005 คือ revert เฉพาะเอกสาร/tests/development dependencies
ไม่ restore state เก่าหรือลด offset การลบ/revert state ไม่สามารถถอนข้อความที่ส่งแล้ว
หาก candidate state ที่อนุมัติในอนาคตผิด ให้ทำ corrective diff บน latest master
พร้อมตรวจข้อมูลใหม่อีกครั้ง ไม่ revert ทั้งไฟล์กลับ snapshot

## งานโค้ดที่ต้องตามต่อ: GM-007 (เสนอจากผล GM-005)

1. แก้ CHECK_URLS ให้เลื่อน baseline เมื่อยืนยันส่งสำเร็จเท่านั้น พร้อม regression test
2. ทำ preview ที่เลือกโหมดก่อน startup API และไม่ส่ง/เขียน state/dispatch self-chain
   รวมกรณี flags ขัดกัน และทำให้รอบลูกไม่หลุดเป็นโหมดส่งจริง
3. ออกแบบ delivery ledger/outbox พร้อม event identity รายเรื่อง/ตอน/ห้อง/ประเภทข้อความ,
   บันทึก receipt/message ID และแยก accepted/rejected/unknown
   หยุด auto retry และ photo fallback เมื่อผลส่งกำกวมจนกว่าจะ reconcile ได้
4. ทดสอบ ack หาย, save/push failure, restart, concurrent writer และ summary ส่งไม่สำเร็จ
   ใช้หลักฐานพิสูจน์ recovery ราย event ไม่อ้าง exactly-once จาก baseline เพียงอย่างเดียว

Receipt/outbox อย่างเดียวไม่ได้ขจัดช่วงส่งสำเร็จแต่บันทึก receipt ไม่สำเร็จ
การออกแบบต้องคง unknown เพื่อให้ตรวจสอบก่อน retry ระบบปัจจุบันไม่มีวิธีรับประกัน
exactly-once สำหรับการส่งที่ไม่ทราบผล GM-007 ต้องระบุขอบเขตนี้ตามจริง
