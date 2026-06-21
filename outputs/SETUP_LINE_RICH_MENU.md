# ตั้งค่า Rich Menu สำหรับ LINE OA

LINE ไม่ส่ง webhook ตอนผู้ใช้แค่เปิดหน้าแชท ดังนั้นบอทไม่สามารถส่งเมนูอัตโนมัติในจังหวะ “เข้าแชทเฉยๆ” ได้ วิธีที่ถูกต้องคือสร้าง Rich Menu ให้แสดงอยู่ด้านล่างของห้องแชทตลอดเวลา

## สำคัญ

การ deploy บอทบน Render ยังไม่ทำให้ Rich Menu แสดงเอง ต้องรันสคริปต์นี้ 1 ครั้งเพื่อเรียก LINE API และตั้งค่า Rich Menu ให้ LINE OA

## วิธีรัน

เปิด PowerShell ที่โฟลเดอร์โปรเจกต์ แล้วใส่ Channel access token ก่อน:

```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN="ใส่ Channel access token ของ LINE OA"
python outputs\setup_line_rich_menu.py --delete-existing
```

เมื่อสำเร็จจะขึ้นประมาณนี้:

```text
Generated rich menu image: outputs\line_rich_menu.png
Created and set default rich menu: richmenu-xxxxxxxx
```

จากนั้นเปิดห้องแชท LINE OA ใหม่ เมนูหลักจะอยู่ด้านล่างแบบแนวตั้ง 1 ตัวเลือกต่อ 1 บรรทัด สามารถกด:

- บัญชี
- สต็อค
- HR
- สินค้า

เมื่อกด `บัญชี` ระบบจะแสดงเมนูงานบัญชีต่อ:

- 1 บิลรายรับ
- 2 บิลรายจ่าย
- 3 เรียกดูรายละเอียดบัญชี
- 4 ยกเลิกการทำรายการ

ถ้าเมนูยังไม่ขึ้น ให้ปิดห้องแชทแล้วเปิดใหม่ หรือกดแถบคำว่า `เมนูหลัก` ด้านล่างแชท
