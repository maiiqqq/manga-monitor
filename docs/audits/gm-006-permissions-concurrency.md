# GM-006: ผลตรวจสิทธิ์และ concurrent writers

วันที่ตรวจ: 8 พฤศจิกายน 2569 (เวลาทั้งหมดในรายงานเป็น UTC; เวลาไทยบวก 7 ชั่วโมง)
Repository: maiiqqq/manga-monitor, master ที่เริ่มตรวจ
`8b0310e52ec5295e02f61ae720ba705e7ba6e4ad` หลัง GM-007 PR #29 merge แล้ว
ผู้ใช้สั่งให้ทำ audit นี้โดยตรง จึงตรวจแม้ไม่มี failure ใหม่เข้าเงื่อนไขเดิม

**ผลตัดสิน:** ยังไม่มีหลักฐานให้เพิ่มสิทธิ์ token, เปลี่ยน PAT, ปิด protection หรือ
เปลี่ยน concurrency เพื่อแก้เหตุเดิม GM-006 ส่งมอบผลตรวจและ 8 merge fixtures
โดยไม่มี production code/config/state change ข้อสรุปจำกัดตามข้อมูลที่อ่านได้
ไม่ได้รับรอง settings ทุกชนิดหรือการส่ง Telegram จริง

หลักฐานที่คัดเฉพาะ metadata/สัญญาณ log อยู่ใน
[gm-006-evidence.json](gm-006-evidence.json) ไม่เก็บ raw logs, secrets หรือ chat ID
การเก็บเกิดหลายคำขอ จึงมี run ที่เปลี่ยนจาก in_progress เป็น completed ระหว่างตรวจ

## 1. สิทธิ์และกฎ branch

| หลักฐาน | ผลที่อ่านได้ | ขอบเขตข้อสรุป |
|---|---|---|
| [master metadata](https://api.github.com/repos/maiiqqq/manga-monitor/branches/master) | protected=false | เป็นค่าจาก branch metadata ณ ตอนอ่าน |
| [rulesets รวม parents](https://api.github.com/repos/maiiqqq/manga-monitor/rulesets?includes_parents=true) | [] | ไม่พบ ruleset ในผลที่ connector อ่านได้ |
| [protection รายละเอียด](https://api.github.com/repos/maiiqqq/manga-monitor/branches/master/protection) | 403 Resource not accessible by integration | อ่านรายละเอียดไม่ได้; เป็นข้อจำกัด integration ไม่ใช่ workflow token ถูกปฏิเสธ |
| Workflow ใน master และ SHA ของคิวเก่า | job permissions: contents:write | ไม่พบการเปลี่ยนนโยบายสิทธิ์ระหว่างสอง revision ที่เทียบ |
| Setup logs ของ job ที่ตรวจ | Contents:write, Metadata:read | ยืนยันสิทธิ์ที่ runner รายงานสำหรับรอบเหล่านั้น ไม่ใช่สิทธิ์ของ connector |

ตรวจ permission จาก log ของ job เหล่านี้:

- [127599340635](https://github.com/maiiqqq/manga-monitor/actions/runs/42128663395/job/127599340635): รอบเดิมล้มเหลว
- [127676407229](https://github.com/maiiqqq/manga-monitor/actions/runs/42151756685/job/127676407229): รอบฟื้นตัว
- [128671603145](https://github.com/maiiqqq/manga-monitor/actions/runs/42461511060/job/128671603145): รอบล่าสุดก่อน GM-007
- [128672566969](https://github.com/maiiqqq/manga-monitor/actions/runs/42461825529/job/128672566969): chain รุ่นเก่าที่จบระหว่าง audit
- [128673560980](https://github.com/maiiqqq/manga-monitor/actions/runs/42462149624/job/128673560980): รอบที่ใช้ GM-007 แล้ว

รอบล้มเหลวและรอบฟื้นตัวเริ่มจาก SHA เดียวกัน `95adfb29...` และได้ Contents:write
เหมือนกัน รอบเดิมถูก remote ตอบ `fatal error in commit_refs_rules` 5 ครั้ง ส่วน
รอบฟื้นตัว push `95adfb2..9c83806 HEAD -> master` สำเร็จครั้งแรกเมื่อ
6 พ.ย. 02:44:17 UTC จึงมีหลักฐานการ push สำเร็จจริง ไม่ใช่แค่ประกาศสิทธิ์ใน YAML
ไม่พบ non-fast-forward หรือ explicit permission/rule denial ใน log เหตุเดิมที่ตรวจ
แต่ error นี้เพียงอย่างเดียวไม่ได้ยืนยันสาเหตุภายใน GitHub

รอบใหม่ 42462149624 จบสำเร็จและ self-chain accepted HTTP204 แต่ persistence
ระบุ **No state changes to commit** และไม่มี UPDATE ใน log ที่ตรวจ จึงยังไม่ใช่
การพิสูจน์ว่า ledger PUT/การส่ง card ทำงานจริงในรอบนั้น ไม่มีการสร้างงานส่งเพื่อทดสอบ

ขณะแยกสัญญาณ log ต้องตัดบรรทัด shell script ที่ runner พิมพ์ออกมาก่อน:
การพบข้อความ `echo "Push succeeded..."` ในตัว script ไม่ใช่หลักฐานว่า push สำเร็จ
รายงานนี้ใช้เฉพาะข้อความ execution/output จริงและ SHA ที่ remote ยืนยัน

## 2. ตารางเวลาและการเขียนซ้อน

อ่าน recent master page แรก 100 จาก total_count 2500 ช่วง created_at
8 พ.ย. 01:03:38–08:11:03 UTC: 97 success, 2 cancelled, 1 in_progress ณ ตอนอ่าน
เป็น workflow_dispatch 98 และ schedule 2 ไม่ได้ตรวจทุก 2500 รอบ
Failure query คืน 17 รายการและล่าสุดยังเป็น 42128663395 (5 พ.ย. UTC/6 พ.ย. ไทย)
ไม่พบ failure ใหม่กว่าเหตุเดิมในผลที่อ่านได้ ไม่ใช้ job สีเขียวแทนหลักฐานส่งครบ

`workflow_dispatch` อย่างเดียวแยก manual กับ self-chain ไม่ได้ โดยเฉพาะ run เก่า
ที่ยังไม่มี correlation title/inputs ให้ตรวจ ในคู่ด้านล่างใช้ chain title และ parent
dispatch log ประกอบกับ job/step timestamps

| Run / revision | Bot step | Persist step | Job ทั้งรอบ |
|---|---|---|---|
| [42461511060](https://github.com/maiiqqq/manga-monitor/actions/runs/42461511060), c18c360 | 08:06:51–08:11:01 | 08:11:01–08:11:02 | 08:06:42–08:11:05 |
| [42461825529](https://github.com/maiiqqq/manga-monitor/actions/runs/42461825529), c18c360 | 08:11:15–08:15:28 | 08:15:28–08:15:29 | 08:11:08–08:15:34 |
| [42462149624](https://github.com/maiiqqq/manga-monitor/actions/runs/42462149624), 8b0310e | 08:15:42–08:19:58 | 08:19:58–08:19:59 | 08:15:36–08:20:02 |

ในสอง handoff นี้ job และช่วงที่ bot/persist เขียนข้อมูลไม่ซ้อนกัน
การสร้าง child run เกิดก่อน cleanup ของ parent จบเล็กน้อย จึงทำให้ run-level
created/updated interval ดูซ้อน แต่ไม่ใช่หลักฐานว่ามี active writers พร้อมกัน
เป็นข้อสรุปของคู่ตัวอย่างนี้ ไม่ใช่การรับรองทุก run หรือทุก VM

[Schedule 42461016515](https://github.com/maiiqqq/manga-monitor/actions/runs/42461016515)
มี conclusion=cancelled แต่ returned jobs=0 จึงไม่มี job ที่แสดงว่าเริ่มเขียน state
การถูกยกเลิกระหว่างรอคิวสอดคล้องได้กับนโยบาย pending replacement ของ concurrency
แต่ไม่มี log ระบุผู้ยกเลิก จึงไม่สรุปว่าเกิดจากนโยบายนั้นแน่นอน

### คิวเก่าที่ควรจัดการแยก

[42267403160](https://github.com/maiiqqq/manga-monitor/actions/runs/42267403160)
created 6 พ.ย. 18:04:45 UTC, SHA `61fd4cd6d02d02329c8296a70b25c08e998bba4a`
ยังถูก API รายงาน queued และ jobs=0 ขณะตรวจ ไม่พบหลักฐานว่าเป็น active writer
แต่หากเริ่มภายหลังจะใช้ code ก่อน GM-007 จึงมีความเสี่ยงตอนเปลี่ยนรุ่น
ไม่ทราบเหตุที่คิวเก่าค้าง และไม่ทราบ inputs.chained ของ run นี้จาก metadata ที่อ่านได้
จึงยังระบุกลุ่ม concurrency ที่มันรออยู่ไม่ได้

ข้อเสนอ: ผู้ดูแลตรวจ inputs/ความตั้งใจของ run นี้ แล้วพิจารณายกเลิก run เก่า
แทนการปล่อยหรือ rerun แบบไม่ตรวจหลักฐาน บันทึกผลก่อนปิดประเด็นคิวเก่า
**audit นี้ไม่ได้ cancel, rerun หรือ dispatch** และไม่ถือว่าความเสี่ยงคิวเก่าถูกแก้แล้ว

## 3. นโยบาย concurrency ที่มีอยู่

ทั้ง revision ของคิวเก่าและ master ปัจจุบันใช้ group expression เดียวกัน:

```yaml
group: manga-monitor-${{ (github.event_name == 'workflow_dispatch' && inputs.chained != 'yes') && github.run_id || 'state' }}
cancel-in-progress: false
```

- schedule และ chained=yes ใช้กลุ่ม state ร่วมกัน
- workflow_dispatch ที่ chained ไม่ใช่ yes ใช้กลุ่มแยกตาม run_id จึงอาจซ้อนกับ chain ได้
  รวม manual ที่ไม่มี diagnostic flags และเข้า normal long-poll loop
- false ไม่ได้หมายความว่าทุก pending run จะอยู่ในคิวตลอดไป: นโยบายเริ่มต้นยังแทนที่
  pending เดิมได้ ดู [GitHub concurrency docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- ไม่เพิ่ม queue:max หรือเปลี่ยนทุก manual เป็นกลุ่มเดียวใน audit นี้ เพราะยังไม่มี
  หลักฐานว่าจำเป็น และจะเปลี่ยนเวลารอของ diagnostics/backfill รวมทั้งพฤติกรรม fallback

GM-007 ใช้ ledger CAS ช่วยกัน card/favorite ซ้ำสำหรับ code ใหม่ที่ใช้ remote store
เดียวกัน แต่ไม่ได้ทำให้ bookmark commands จากหลาย long-pollersมีลำดับตามเวลา
ถ้ามีหลักฐาน manual normal writer ซ้อนจริง ให้เปิดงานปรับ group เฉพาะเส้นทางนั้น
พร้อมทดสอบผลต่อ command latency และ diagnostics แทนการเพิ่มสิทธิ์ token

## 4. ทดสอบ merge พร้อมกันและการลบ

เพิ่ม [test_merge_state_audit.py](../../tests/test_merge_state_audit.py) เรียก CLI จริง
ใน temporary directory ด้วย synthetic JSON ไม่เชื่อมต่อ GitHub/Telegram
8 tests ครอบคลุม:

1. ต่าง key เปลี่ยนพร้อมกันและ remote เพิ่ม key: รักษาทั้งสองฝั่ง
2. เรื่องเดียวชนกันและสลับ ours/theirs: conflict เลือกตอนที่สูงกว่า
3. ตอนเท่ากัน: ใช้ chapter date แล้ว last_check
4. Bookmark สำเนา ours ไม่ได้แก้: รักษาการแก้/ลบ/เพิ่มจาก remote
5. ลบ bookmark ฝั่ง ours: การลบชนะ concurrent edit; remote-only addition ยังอยู่
6. Bookmark ทั้งสองฝั่งแก้ key เดียว: ours ชนะตาม policy ปัจจุบัน ไม่รับรองลำดับ tap จริง
7. ลบ monitor key ฝั่ง ours: ลบชนะแม้ remote เลื่อนไปตอนใหม่ เป็น characterization
   ของ policy ที่ต้องระวังในการกู้คืน ไม่ใช่กฎรับรองว่า state ไม่ถอย/ไม่หาย
8. Bot offset เลือกค่าสูงสุดและ commands_registered ใช้ OR; remote-only key ยังอยู่

ข้อ 7 ทำให้ห้ามใช้การลบ monitor key เป็นวิธีกู้คืนเพื่อบังคับตรวจใหม่
ร่วมกับข้อจำกัด merge ที่ GM-005 ทดสอบไว้ ต้องตรวจ candidate ทุก key กับ latest
ก่อนเสนอ state recovery ไม่เปลี่ยน merge policy จาก audit นี้โดยไม่มีเหตุใช้งานจริง
ชุดเดิมยังมี local Git tests สำหรับ non-fast-forward, push response หาย และ remote ledger
ใหม่ที่ต้องไม่ถูก checkout เก่าทับ จึงทดสอบร่วมทั้งชุด

ผลตรวจ: 8 fixtures ผ่าน; full suite **128 tests ผ่านใน 44.415s**
`py_compile`, JSON evidence validation และ `git diff --check` ต้องผ่านก่อนส่ง PR
Baseline log ที่ไม่มี summary ครบไม่นับเป็นหลักฐานผ่าน ใช้ fresh full-suite result ข้างต้น

## 5. ปิด audit และเปิดใหม่เมื่อใด

ปิดงานตรวจ GM-006 ด้วยผล **ยังไม่พบเหตุให้เปลี่ยน production permissions/concurrency**
พร้อมข้อจำกัด detailed protection ที่อ่านไม่ได้ และประเด็นคิวเก่าที่ยังไม่ได้จัดการ
ไม่ปิดความเสี่ยงเหล่านั้นด้วยข้อสันนิษฐานว่า job success หรือ protected=false เพียงอย่างเดียว

เปิดตรวจซ้ำเมื่อมี explicit permission/rule denial, non-fast-forward เกิดซ้ำพร้อม
job/step overlap, หรือคิวเก่าเริ่มรันก่อน GM-007 โดยรักษาหลักฐาน run/attempt/SHA,
granted permissions และ remote SHA ก่อนแก้ หากจำเป็นต้องอ่าน protection เพิ่ม
ให้ผู้ดูแลอ่าน settings ผ่านสิทธิ์เดิมก่อน ไม่เพิ่ม PAT scopes หรือปิดกฎเพียงเพราะ
connector อ่าน endpoint นั้นไม่ได้

Rollback ของ PR นี้: revert เฉพาะรายงาน/evidence/tests ไม่ย้อน runtime state
และไม่เปลี่ยนสิทธิ์ กฎ branch หรือ concurrency เพราะ audit นี้ไม่ได้แก้ค่าเหล่านั้น
