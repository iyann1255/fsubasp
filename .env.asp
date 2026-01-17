# =====================
# BOT CORE
# =====================
BOT_TOKEN=8554561714:AAHhTP_tY35sUeEVxoEFOjJuBoGd21LSxvQ
OWNER_ID=1914062296

# Admin user ids (comma / space separated)
ADMINS=8187005937,1914062296


# =====================
# DB TARGETS (PENYIMPANAN FILE ASLI)
# =====================
# Channel / grup INTERNAL
# User TIDAK lihat ini
# Bot wajib admin / bisa kirim pesan
DB_TARGETS=-1003316849632


# =====================
# FORCE SUB TARGETS
# (WAJIB JOIN — HANYA SAAT AMBIL FILE)
# =====================
# Bisa:
# 1) @publicchannel
# 2) -100id|https://t.me/+invite   (private, paling aman)
# 3) -100id                        (bot auto-create invite, bot harus admin)
FORCE_SUB1=-1002657307543
FORCE_SUB2=-1002526526505
FORCE_SUB3=-1003617219639


# =====================
# FSUB BUTTON UI
# =====================
BUTTONS_PER_ROW=2
BUTTONS_JOIN_TEXT=ᴊᴏɪɴ
MAX_JOIN_BUTTONS=4
ROTATE_SECONDS=30


# =====================
# START & FSUB MESSAGES
# =====================
# /start → WELCOME ONLY (TIDAK ADA KATA WAJIB JOIN)
START_MESSAGE=<b>Hai {mention}</b>\n\nKirim file ke sini, nanti aku simpan dan kasih link aman.

# MUNCUL HANYA SAAT USER AMBIL FILE
FORCE_SUB_MESSAGE=<b>Akses File</b>\n\nGunakan tombol di bawah untuk membuka akses, lalu tekan <b>✅ Sudah Join</b>.


# =====================
# POST CHANNEL (UPLOAD OTOMATIS PILIHAN)
# =====================
# Channel/grup tujuan upload (PILIH VIA BUTTON)
# BUKAN DB, BUKAN FSUB
POST_CHANNEL_IDS=-1003205833632,-1003638398897,-1003480063647,-1003420376235

# Judul tombol (opsional)
# Kalau kurang, sisanya auto CH1, CH2, dst
POST_CHANNEL_TITLES=EXO VVIP,VVIP CAMPURAN,VVIP PAP,VVIP CONTENT


# =====================
# DEEP LINK SECURITY
# =====================
SECRET_KEY=abcdefghijklmnopqrstuvwxyz


# =====================
# STORAGE BACKEND
# =====================
# Pilih: mongo | sqlite
STORAGE_BACKEND=mongo

# MongoDB (kalau pakai mongo)
MONGO_URI=mongodb+srv://aseppp:aseppp@cluster0.bocyf5q.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
MONGO_DB=aseppp

# SQLite (kalau STORAGE_BACKEND=sqlite)
SQLITE_PATH=data.db
