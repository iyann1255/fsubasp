BOT_TOKEN=8554561714:AAHhTP_tY35sUeEVxoEFOjJuBoGd21LSxvQ
OWNER_ID=1914062296

# Admin user ids (comma/space separated)
ADMINS=8187005937,1914062296

# ===== MULTI DB TARGETS (channel/grup) =====
# Bot harus punya akses kirim pesan (group) / admin (channel)
DB_TARGETS=-1003316849632

# ===== FORCE SUB TARGETS (unlimited) =====
# Bisa:
# 1) @publicchannel
# 2) -100id|https://t.me/+invite (recommended for private)
# 3) -100id (bot auto-create invite link)
FORCE_SUB1=-1002657307543
FORCE_SUB2=-1002526526505
FORCE_SUB3=-1003617219639

# Buttons UI
BUTTONS_PER_ROW=2
BUTTONS_JOIN_TEXT=ᴊᴏɪɴ
MAX_JOIN_BUTTONS=4
ROTATE_SECONDS=30

# Messages (HTML parse)
START_MESSAGE=<b>Hai {mention}</b>\nKirim file ke aku, nanti aku kasih link aman.\n\n<i>Note:</i> Kamu wajib join semua channel di bawah dulu ya.
FORCE_SUB_MESSAGE=<b>Wajib join dulu ya</b>\nSetelah join semua, klik <i>✅ Sudah Join</i>.

# Deep-link security
SECRET_KEY=abcdefghijklmnopqrstuvwxyz

# Storage backend: mongo | sqlite
STORAGE_BACKEND=mongo
MONGO_URI=mongodb+srv://aseppp:aseppp@cluster0.bocyf5q.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
MONGO_DB=aseppp

# SQLite (kalau STORAGE_BACKEND=sqlite)
SQLITE_PATH=data.db
