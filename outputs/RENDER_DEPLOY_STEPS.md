# Render Deploy Steps

Use this setup to run the LINE webhook without ngrok.

## 1. Upload Project To GitHub

Create a GitHub repository and upload this whole project folder.

Important files:

- `Dockerfile`
- `requirements.txt`
- `outputs/line_expense_bot.py`
- `outputs/line_bot_config.render.json`
- `outputs/thai_vat_monthly_tracker_line_active.xlsx`

## 2. Create Render Web Service

1. Open Render.
2. Click `New`.
3. Choose `Web Service`.
4. Connect the GitHub repository.
5. Runtime: `Docker`.
6. Keep default Docker settings.

## 3. Add Environment Variables

In Render > Environment, add:

```text
LINE_CHANNEL_SECRET=your LINE channel secret
LINE_CHANNEL_ACCESS_TOKEN=your LINE channel access token
GOOGLE_APPS_SCRIPT_URL=https://script.google.com/macros/s/AKfycbzpbJYKNGnlNwgz2FL_7x8V_eVzRpSqc28ydqPTTxUe_T8QCeXzMxT-YB3qSrIRCxON/exec
GOOGLE_APPS_SCRIPT_SECRET=KJao-VAT-Line-2026-Secret
VAT_RATE=0.07
```

Do not add `PORT`; Render sets it automatically.

## 4. Deploy

Click `Deploy Web Service`.

After deploy succeeds, Render will show a URL like:

```text
https://your-service-name.onrender.com
```

## 5. Update LINE Webhook URL

In LINE Developers > Messaging API > Webhook URL, use:

```text
https://your-service-name.onrender.com/callback
```

Then enable:

- `Use webhook`
- `Webhook redelivery`, optional but recommended

Click `Verify`.

## 6. Test

Send `1`, `2`, or `3` to the LINE OA.

Expected response:

```text
กรุณาส่งเอกสารเพื่อลงรายละเอียดในระบบได้เลยค่ะ
```

If Render free service sleeps, the first message may take 30-60 seconds.
