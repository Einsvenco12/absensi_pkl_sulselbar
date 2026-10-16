"""
Aplikasi Absensi Peserta PKL - Bank Sulselbar (versi siap deploy)
=================================================================
Fitur:
- Layar login di awal: pilih peran. Peserta (nama + PIN) -> halaman absen saja; Admin (password) -> area admin
- Absen MASUK lalu PULANG: foto selfie + lokasi GPS (urutan otomatis)
- Foto harus ada wajah (deteksi wajah, bukan pengenalan); tanpa wajah ditolak dan tidak disimpan
- Login PIN per peserta (dibuat admin)
- Cek lokasi: harus di sekitar gedung yang terdaftar untuk peserta (A / B)
- Setiap peserta juga punya Divisi (dipilih dari daftar, Gedung ikut otomatis)
- Batas waktu: absen masuk hanya bisa sebelum jam BATAS_JAM_MASUK
- Lokasi dan waktu absensi hanya dilihat admin (peserta tidak melihat jarak/koordinat)
- Uang makan Rp15.000 otomatis saat absen PULANG berhasil; admin melihat akumulasinya per hari
- Blokir absen dobel di hari yang sama
- Admin (pakai password): kelola peserta, rekap + grafik + export Excel, bukti foto
- Database: SQLite (lokal) atau Postgres/Supabase (cloud) - otomatis

Jalanin lokal:   streamlit run app.py
"""

import calendar
import hashlib
import hmac
import io
from datetime import datetime, date, time as dtime, timedelta, timezone
from math import radians, sin, cos, sqrt, atan2
from html import escape

import pandas as pd
import streamlit as st
from PIL import Image
from sqlalchemy import (
    Column, Integer, String, Float, LargeBinary, MetaData, Table,
    create_engine, select, insert, update, delete, func, and_,
)
from sqlalchemy.exc import IntegrityError
from streamlit_js_eval import get_geolocation

# ------------------------------------------------------------------
# KONFIGURASI - SESUAIKAN BAGIAN INI
# ------------------------------------------------------------------
UANG_MAKAN = 15000

# Koordinat asli Gedung A dan B Bank Sulselbar
GEDUNG_COORD = {
    "A": (-5.150070542826675, 119.41568236911743),  # (latitude, longitude)
    "B": (-5.150163347858429, 119.41329137592547),  # (latitude, longitude)
}

RADIUS_METER = 150  # area longgar di sekitar gedung (bukan angka ketat)

# Daftar Divisi asli beserta gedungnya ("A" atau "B").
# Peserta tinggal pilih Divisi, Gedung otomatis ikut sesuai daftar ini.
DIVISI_GEDUNG = {
    # Gedung A
    "Divisi Korporasi dan Komersial": "A",
    "Divisi Ritel dan Konsumer": "A",
    "Divisi Corporate Secretary (CORSEC)": "A",
    "Divisi Human Capital": "A",
    "Divisi SIMO": "A",
    "Divisi Keuangan dan Akuntansi": "A",
    "Divisi Treasury": "A",
    "Divisi Teknologi dan Informasi": "A",
    "Divisi International Banking": "A",
    "Divisi Digitalisasi dan Layanan": "A",
    "Divisi Umum": "A",
    # Gedung B
    "Divisi Kepatuhan": "B",
    "Divisi Dana dan Jasa": "B",
    "Divisi Syariah": "B",
    "Divisi Manajemen Risiko": "B",
    "Divisi SKAI": "B",
}

BATAS_JAM_MASUK = dtime(8, 0)  # absen masuk harus sebelum jam ini (WITA), lewat = ditolak

WITA = timezone(timedelta(hours=8))  # server cloud pakai UTC, jadi jam dipaksa WITA

BULAN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
            "September", "Oktober", "November", "Desember"]


# ------------------------------------------------------------------
# HELPER: SECRETS
# ------------------------------------------------------------------
def secret(key, default=None):
    """Ambil nilai dari Streamlit Secrets; aman kalau file secrets belum ada."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# ------------------------------------------------------------------
# DATABASE (SQLAlchemy: jalan di SQLite lokal DAN Postgres cloud)
# ------------------------------------------------------------------
metadata = MetaData()

peserta_t = Table(
    "peserta", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nama", String(100), nullable=False, unique=True),
    Column("gedung", String(1), nullable=False),
    Column("divisi", String(100), nullable=False, default=""),
    Column("pin_hash", String(64), nullable=False),
)

absensi_t = Table(
    "absensi", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nama", String(100), nullable=False, index=True),
    Column("tanggal", String(10), nullable=False, index=True),  # YYYY-MM-DD
    Column("jam", String(8), nullable=False),                   # HH:MM:SS
    Column("jenis", String(10), nullable=False, default="masuk"),  # selalu "masuk"
    Column("lat", Float),
    Column("lon", Float),
    Column("akurasi", Float),
    Column("jarak", Float),
    Column("status", String(20), nullable=False),               # berhasil / gagal_lokasi
    Column("uang_makan", Integer, nullable=False, default=0),
    Column("foto", LargeBinary),
)


@st.cache_resource
def get_engine():
    url = secret("database_url")
    if url:
        url = url.replace("postgres://", "postgresql://", 1)
        # Paksa driver psycopg2 secara eksplisit, supaya SQLAlchemy tidak
        # mencoba driver lain (mis. psycopg v3) yang belum tentu terinstall.
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        eng = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    else:
        # nama file baru (v2) supaya tidak bentrok dengan database versi lama
        eng = create_engine(
            "sqlite:///absensi_pkl_v2.db",
            connect_args={"check_same_thread": False},
        )
    metadata.create_all(eng)
    return eng


def hash_pin(nama, pin):
    garam = secret("salt", "pkl-sulselbar")
    return hashlib.sha256(f"{nama.strip().lower()}|{pin}|{garam}".encode()).hexdigest()


def list_peserta(eng):
    with eng.connect() as c:
        return pd.read_sql(
            select(peserta_t.c.nama, peserta_t.c.gedung, peserta_t.c.divisi).order_by(peserta_t.c.nama), c
        )


def get_peserta(eng, nama):
    with eng.connect() as c:
        return c.execute(select(peserta_t).where(peserta_t.c.nama == nama)).mappings().first()


def add_peserta(eng, nama, gedung, divisi, pin):
    with eng.begin() as c:
        c.execute(insert(peserta_t).values(
            nama=nama.strip(), gedung=gedung, divisi=divisi.strip(), pin_hash=hash_pin(nama, pin)
        ))


def reset_pin(eng, nama, pin_baru):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(pin_hash=hash_pin(nama, pin_baru)))


def set_divisi(eng, nama, divisi, gedung):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(divisi=divisi.strip(), gedung=gedung))


def hapus_peserta(eng, nama, dengan_absensi=False):
    """Hapus peserta. Kalau dengan_absensi=True, riwayat absensinya ikut dihapus."""
    with eng.begin() as c:
        c.execute(delete(peserta_t).where(peserta_t.c.nama == nama))
        if dengan_absensi:
            c.execute(delete(absensi_t).where(absensi_t.c.nama == nama))


def daftar_nama_absensi(eng):
    """Semua nama yang punya riwayat absensi (termasuk peserta yang sudah dihapus)."""
    q = select(absensi_t.c.nama).distinct().order_by(absensi_t.c.nama)
    with eng.connect() as c:
        return [r[0] for r in c.execute(q).all()]


def hapus_absensi(eng, nama=None):
    """Hapus riwayat absensi (beserta fotonya). nama=None berarti SEMUA peserta.
    Mengembalikan jumlah baris yang terhapus."""
    stmt = delete(absensi_t)
    if nama is not None:
        stmt = stmt.where(absensi_t.c.nama == nama)
    with eng.begin() as c:
        return c.execute(stmt).rowcount


def sudah_berhasil(eng, nama, tanggal, jenis):
    q = select(func.count()).select_from(absensi_t).where(and_(
        absensi_t.c.nama == nama, absensi_t.c.tanggal == tanggal,
        absensi_t.c.jenis == jenis, absensi_t.c.status == "berhasil",
    ))
    with eng.connect() as c:
        return c.execute(q).scalar() > 0


def simpan_absen(eng, **nilai):
    with eng.begin() as c:
        c.execute(insert(absensi_t).values(**nilai))


def get_absensi_bulan(eng, tahun, bulan):
    prefix = f"{tahun:04d}-{bulan:02d}-"
    q = (
        select(
            absensi_t.c.nama, absensi_t.c.tanggal, absensi_t.c.jam, absensi_t.c.jenis,
            absensi_t.c.status, absensi_t.c.jarak, absensi_t.c.lat, absensi_t.c.lon,
            absensi_t.c.uang_makan,
        )
        .where(absensi_t.c.tanggal.like(prefix + "%"))
        .order_by(absensi_t.c.tanggal, absensi_t.c.jam)
    )
    with eng.connect() as c:
        return pd.read_sql(q, c)


def get_absensi_hari_foto(eng, tanggal):
    q = (
        select(
            absensi_t.c.nama, absensi_t.c.jam, absensi_t.c.jenis, absensi_t.c.status,
            absensi_t.c.jarak, absensi_t.c.foto,
        )
        .where(absensi_t.c.tanggal == tanggal)
        .order_by(absensi_t.c.jam)
    )
    with eng.connect() as c:
        return c.execute(q).mappings().all()


# ------------------------------------------------------------------
# LOGIKA: LOKASI & FOTO
# ------------------------------------------------------------------
def jarak_meter(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def cek_lokasi(lat, lon, gedung):
    target = GEDUNG_COORD[gedung]
    jarak = jarak_meter(lat, lon, target[0], target[1])
    return jarak <= RADIUS_METER, jarak


def beri_nomor(df):
    """Nomor urut tabel mulai dari 1 (bukan 0)."""
    df = df.copy()
    df.index = range(1, len(df) + 1)
    df.index.name = "No"
    return df


def rupiah(n):
    return f"Rp{int(n):,}".replace(",", ".")


def rekap_peserta(df):
    """Per peserta: hari hadir (absen masuk berhasil), hari pulang, total uang makan."""
    ok = df[df["status"] == "berhasil"]
    masuk = ok[ok["jenis"] == "masuk"].groupby("nama")["tanggal"].nunique()
    pulang = ok[ok["jenis"] == "pulang"].groupby("nama")["tanggal"].nunique()
    uang = ok.groupby("nama")["uang_makan"].sum()
    out = pd.DataFrame({"Hari Hadir": masuk, "Hari Pulang": pulang, "Total Uang Makan": uang})
    out = out.fillna(0).astype(int).sort_index()
    out.index.name = "Nama"
    return out.reset_index()


def rekap_harian(df):
    """Per tanggal: jumlah peserta masuk/pulang, uang makan hari itu, dan akumulasinya."""
    ok = df[df["status"] == "berhasil"]
    out = pd.DataFrame({
        "Hadir": ok[ok["jenis"] == "masuk"].groupby("tanggal")["nama"].nunique(),
        "Pulang": ok[ok["jenis"] == "pulang"].groupby("tanggal")["nama"].nunique(),
        "Uang Makan Hari Ini": ok.groupby("tanggal")["uang_makan"].sum(),
    }).fillna(0).astype(int).sort_index()
    out["Akumulasi Uang Makan"] = out["Uang Makan Hari Ini"].cumsum()
    out.index.name = "Tanggal"
    return out.reset_index()


def matriks_kehadiran(df, peserta_df, tahun, bulan):
    """Rekapan kehadiran: baris = peserta, kolom = tanggal 1..akhir bulan.
    ✓ = absen masuk & pulang, M = hanya absen masuk, kosong = tidak hadir."""
    jumlah_hari = calendar.monthrange(tahun, bulan)[1]
    hari = [str(d) for d in range(1, jumlah_hari + 1)]
    kolom = ["Nama", "Gedung", "Divisi"] + hari + ["Hadir", "Total Uang Makan"]
    ok = df[df["status"] == "berhasil"]
    info = peserta_df.set_index("nama")
    baris = []
    for nama in sorted(set(peserta_df["nama"]) | set(ok["nama"])):
        sub = ok[ok["nama"] == nama]
        masuk = {int(t[8:10]) for t in sub[sub["jenis"] == "masuk"]["tanggal"]}
        pulang = {int(t[8:10]) for t in sub[sub["jenis"] == "pulang"]["tanggal"]}
        row = {
            "Nama": nama,
            "Gedung": info["gedung"].get(nama, "-"),
            "Divisi": info["divisi"].get(nama, "-"),
        }
        for d in range(1, jumlah_hari + 1):
            if d in masuk and d in pulang:
                row[str(d)] = "✓"
            elif d in masuk:
                row[str(d)] = "M"
            elif d in pulang:
                row[str(d)] = "P"
            else:
                row[str(d)] = ""
        row["Hadir"] = len(masuk)
        row["Total Uang Makan"] = int(sub["uang_makan"].sum())
        baris.append(row)
    return pd.DataFrame(baris, columns=kolom)


def tabel_detail(df):
    """Detail semua absensi termasuk lokasi & waktu (hanya untuk admin)."""
    d = df.copy()
    d["Peta"] = [
        f"https://www.google.com/maps?q={la},{lo}" if pd.notna(la) and pd.notna(lo) else ""
        for la, lo in zip(d["lat"], d["lon"])
    ]
    d["lat"], d["lon"], d["jarak"] = d["lat"].round(6), d["lon"].round(6), d["jarak"].round(0)
    d["jenis"] = d["jenis"].str.capitalize()
    d["status"] = d["status"].map({"berhasil": "Berhasil", "gagal_lokasi": "Gagal (di luar area)"}).fillna(d["status"])
    d = d.rename(columns={
        "nama": "Nama", "tanggal": "Tanggal", "jam": "Jam", "jenis": "Jenis", "status": "Status",
        "jarak": "Jarak ke Gedung (m)", "lat": "Latitude", "lon": "Longitude", "uang_makan": "Uang Makan",
    })
    return d[["Nama", "Tanggal", "Jam", "Jenis", "Status", "Jarak ke Gedung (m)",
              "Latitude", "Longitude", "Uang Makan", "Peta"]]


KOLOM_RUPIAH = {"Total Uang Makan", "Uang Makan Hari Ini", "Akumulasi Uang Makan", "Uang Makan"}


def _tulis_sheet(writer, nama_sheet, df, judul, periode, keterangan=None, total_kolom=()):
    """Tulis satu sheet Excel dengan judul, header berwarna, border, lebar kolom, dan baris TOTAL."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    n = beri_nomor(df)
    n.to_excel(writer, sheet_name=nama_sheet, startrow=3)
    ws = writer.sheets[nama_sheet]

    header_row, jumlah_baris = 4, len(df)
    nama_kolom = ["No"] + list(df.columns)
    garis = Side(style="thin", color="999999")
    border = Border(left=garis, right=garis, top=garis, bottom=garis)

    ws["A1"] = judul
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = periode
    ws["A2"].font = Font(size=11)
    if keterangan:
        ws["A3"] = keterangan
        ws["A3"].font = Font(italic=True, size=10, color="555555")

    for i, nm in enumerate(nama_kolom, start=1):
        c = ws.cell(row=header_row, column=i)
        c.fill = PatternFill("solid", fgColor="1F4E79")
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border

    for r in range(header_row + 1, header_row + 1 + jumlah_baris):
        for i, nm in enumerate(nama_kolom, start=1):
            c = ws.cell(row=r, column=i)
            c.border = border
            if nm in KOLOM_RUPIAH:
                c.number_format = '"Rp"#,##0'
            if nm.isdigit() or nm == "No":
                c.alignment = Alignment(horizontal="center")

    if total_kolom and jumlah_baris:
        r = header_row + jumlah_baris + 1
        ws.cell(row=r, column=2, value="TOTAL")
        for nm in total_kolom:
            if nm in df.columns:
                c = ws.cell(row=r, column=nama_kolom.index(nm) + 1, value=int(df[nm].sum()))
                if nm in KOLOM_RUPIAH:
                    c.number_format = '"Rp"#,##0'
        for i in range(1, len(nama_kolom) + 1):
            c = ws.cell(row=r, column=i)
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="DDEBF7")
            c.border = border

    if "Peta" in nama_kolom:  # tautan Google Maps (khusus lembar Detail)
        kol = nama_kolom.index("Peta") + 1
        for r in range(header_row + 1, header_row + 1 + jumlah_baris):
            c = ws.cell(row=r, column=kol)
            if c.value:
                c.hyperlink = c.value
                c.value = "Buka peta"
                c.font = Font(color="0563C1", underline="single")

    for i, nm in enumerate(nama_kolom, start=1):
        huruf = ws.cell(row=header_row, column=i).column_letter
        if nm.isdigit():
            ws.column_dimensions[huruf].width = 4.5
        else:
            isi = [len(str(nm))] + [len(str(v)) for v in n.iloc[:, i - 2]] if i > 1 else [4]
            ws.column_dimensions[huruf].width = min(max(isi) + 3, 42)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=3)
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def excel_bytes(df, peserta_df, tahun, bulan):
    """Excel siap pakai (4 lembar): Rekap Kehadiran, Uang Makan Harian, Rekap per Peserta, Detail Absensi."""
    periode = f"Periode: {BULAN_ID[bulan - 1]} {tahun}"
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        _tulis_sheet(
            w, "Rekap Kehadiran", matriks_kehadiran(df, peserta_df, tahun, bulan),
            "REKAP KEHADIRAN PESERTA PKL - BANK SULSELBAR", periode,
            "Keterangan: ✓ = absen masuk dan pulang | M = hanya absen masuk (belum/tidak pulang) | kosong = tidak hadir",
            total_kolom=("Hadir", "Total Uang Makan"),
        )
        _tulis_sheet(
            w, "Uang Makan Harian", rekap_harian(df),
            "AKUMULASI UANG MAKAN HARIAN PESERTA PKL - BANK SULSELBAR", periode,
            "Uang makan dihitung saat peserta berhasil absen pulang.",
            total_kolom=("Hadir", "Pulang", "Uang Makan Hari Ini"),
        )
        _tulis_sheet(
            w, "Rekap per Peserta", rekap_peserta(df),
            "REKAP PER PESERTA PKL - BANK SULSELBAR", periode,
            total_kolom=("Hari Hadir", "Hari Pulang", "Total Uang Makan"),
        )
        _tulis_sheet(
            w, "Detail Absensi", tabel_detail(df),
            "DETAIL ABSENSI (LOKASI & WAKTU) - BANK SULSELBAR", periode,
            "Informasi lokasi dan waktu hanya untuk admin.",
            total_kolom=("Uang Makan",),
        )
    return buf.getvalue()


@st.cache_resource
def _muat_detektor_wajah():
    import cv2
    detektor = [
        cv2.CascadeClassifier(cv2.data.haarcascades + nama)
        for nama in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
    ]
    if any(d.empty() for d in detektor):
        raise RuntimeError("file detektor wajah tidak ditemukan")
    return detektor


def jumlah_wajah(data: bytes):
    """Jumlah wajah (menghadap depan) yang terdeteksi di foto; 0 kalau tidak ada wajah.
    Mengembalikan None kalau pendeteksi tidak tersedia di server.
    Ini DETEKSI saja (ada wajah atau tidak), bukan pengenalan siapa orangnya."""
    try:
        import cv2
        import numpy as np
        detektor = _muat_detektor_wajah()
    except Exception:
        return None

    gambar = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gambar is None:
        return 0
    h, w = gambar.shape
    if max(h, w) > 640:  # perkecil supaya cepat di server gratis
        skala = 640 / max(h, w)
        gambar = cv2.resize(gambar, (int(w * skala), int(h * skala)))
        h, w = gambar.shape
    gambar = cv2.equalizeHist(gambar)  # ratakan kontras (cahaya kurang / terlalu terang)
    ukuran_min = max(30, int(min(h, w) * 0.10))

    def cari(g, tetangga):
        return max(
            len(d.detectMultiScale(g, scaleFactor=1.1, minNeighbors=tetangga, minSize=(ukuran_min, ukuran_min)))
            for d in detektor
        )

    n = cari(gambar, 4)
    if n:
        return n
    # kepala sedikit miring: coba putar ±15° dengan ambang lebih ketat supaya tidak salah deteksi
    for sudut in (15, -15):
        putar = cv2.getRotationMatrix2D((w / 2, h / 2), sudut, 1.0)
        miring = cv2.warpAffine(gambar, putar, (w, h), borderValue=int(gambar.mean()))
        n = cari(miring, 8)
        if n:
            return n
    return 0


def kompres_foto(data: bytes) -> bytes:
    """Kecilkan foto (maks 640px, JPEG q70) supaya database tidak cepat penuh."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((640, 640))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70, optimize=True)
    return out.getvalue()


# ------------------------------------------------------------------
# ADMIN LOGIN
# ------------------------------------------------------------------
def password_admin():
    """Password admin yang berlaku. None = belum diatur di Secrets (mode cloud)."""
    pw = secret("admin_password")
    if pw:
        return str(pw)
    if secret("database_url"):
        return None
    return "admin123"  # mode lokal (tanpa database cloud)


def cek_pin(eng, nama, pin):
    peserta = get_peserta(eng, nama)
    return peserta is not None and hmac.compare_digest(peserta["pin_hash"], hash_pin(nama, pin))


def admin_login():
    """Penjaga halaman admin. Login normal lewat layar login di awal; ini cadangan
    kalau halaman admin terbuka tanpa lewat login."""
    if st.session_state.get("admin_ok"):
        return True

    pw_benar = password_admin()
    if pw_benar is None:
        st.error("admin_password belum diatur di Secrets. Hubungi pengelola aplikasi.")
        return False
    if not secret("admin_password") and not secret("database_url"):
        st.info("Mode lokal: password admin default = admin123")

    pw = st.text_input("Password admin", type="password")
    if st.button("Masuk sebagai admin"):
        if hmac.compare_digest(pw, str(pw_benar)):
            st.session_state.admin_ok = True
            st.rerun()
        else:
            st.error("Password salah.")
    return False


# ------------------------------------------------------------------
# HALAMAN: ABSEN (untuk peserta PKL)
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# DESAIN DASHBOARD PESERTA
# ------------------------------------------------------------------
HARI_ID = [
    "Senin", "Selasa", "Rabu", "Kamis",
    "Jumat", "Sabtu", "Minggu",
]


def css_dashboard_peserta():
    st.markdown(
        """
        <style>
        .stApp {
            background: #f3f5fc;
        }

        .block-container {
            max-width: 780px;
            padding-top: 1.2rem;
            padding-bottom: 3rem;
        }

        .presensi-hero {
            background: linear-gradient(
                120deg, #1564ee 0%, #254cdd 55%, #4136c9 100%
            );
            border-radius: 26px;
            padding: 28px 26px 58px;
            color: white;
        }

        .hero-top {
            display: flex;
            align-items: center;
            gap: 13px;
            margin-bottom: 30px;
        }

        .hero-icon {
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.18);
            font-size: 27px;
        }

        .hero-title {
            font-size: 26px;
            font-weight: 700;
            line-height: 1.2;
        }

        .hero-brand {
            margin-top: 5px;
            font-size: 12px;
            color: #dbe6ff;
        }

        .hero-greeting {
            font-size: 17px;
            color: #e4edff;
            margin-bottom: 7px;
        }

        .hero-name {
            font-size: 27px;
            font-weight: 750;
            line-height: 1.3;
            overflow-wrap: anywhere;
        }

        .hero-meta {
            margin-top: 9px;
            font-size: 13px;
            color: #e2eaff;
            line-height: 1.7;
        }

        .attendance-card {
            position: relative;
            margin: -30px 14px 24px;
            padding: 24px;
            background: #ffffff;
            border-radius: 25px;
            box-shadow: 0 12px 35px rgba(38, 59, 120, 0.07);
        }

        .attendance-date-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 22px;
        }

        .attendance-date {
            color: #59616f;
            font-size: 15px;
            line-height: 1.5;
        }

        .active-badge {
            flex-shrink: 0;
            background: #dcf9e9;
            color: #1d956c;
            padding: 6px 13px;
            border-radius: 99px;
            font-size: 13px;
            font-weight: 700;
        }

        .attendance-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
        }

        .attendance-status {
            min-height: 148px;
            padding: 20px 10px;
            border-radius: 21px;
            text-align: center;
        }

        .status-masuk {
            background: #eafbf4;
            color: #24a987;
        }

        .status-pulang {
            background: #fff0f3;
            color: #dc708b;
        }

        .status-icon {
            font-size: 28px;
            margin-bottom: 8px;
        }

        .status-label {
            font-size: 15px;
            margin-bottom: 8px;
        }

        .status-value {
            font-size: 18px;
            font-weight: 750;
            line-height: 1.4;
        }

        .attendance-notice {
            margin-top: 17px;
            padding: 16px;
            border-radius: 17px;
            background: #eff4ff;
            color: #607cae;
            text-align: center;
            font-size: 14px;
            line-height: 1.7;
        }

        .participant-menu {
            background: white;
            padding: 23px;
            margin-bottom: 20px;
            border-radius: 24px;
            box-shadow: 0 8px 28px rgba(38, 59, 120, 0.04);
        }

        .participant-menu-title {
            font-size: 21px;
            font-weight: 700;
            color: #293247;
            margin-bottom: 21px;
        }

        .participant-menu-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
        }

        .participant-menu-item {
            text-align: center;
            font-size: 12px;
            color: #6b7280;
            line-height: 1.5;
        }

        .participant-menu-icon {
            width: 60px;
            height: 60px;
            margin: 0 auto 10px;
            border-radius: 19px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 27px;
        }

        .menu-pink { background: #fff0f4; }
        .menu-blue { background: #eaf8ff; }
        .menu-green { background: #eaf9f2; }
        .menu-purple { background: #f1edff; }

        .menu-note {
            margin-top: 18px;
            font-size: 12px;
            color: #8a93a5;
        }

        div.stButton > button[kind="primary"] {
            background: #2858e6;
            border: 0;
            border-radius: 14px;
            min-height: 48px;
            font-weight: 650;
        }

        div[data-testid="stCameraInput"] {
            border-radius: 18px;
            overflow: hidden;
        }

        @media (max-width: 480px) {
            .block-container {
                padding-left: 1rem;
                padding-right: 1rem;
            }

            .presensi-hero {
                padding: 24px 20px 52px;
            }

            .hero-name {
                font-size: 23px;
            }

            .attendance-card {
                margin-left: 8px;
                margin-right: 8px;
                padding: 18px;
            }

            .attendance-date {
                font-size: 12px;
            }

            .status-value {
                font-size: 16px;
            }

            .participant-menu {
                padding: 20px 14px;
            }

            .participant-menu-icon {
                width: 48px;
                height: 48px;
                font-size: 23px;
                border-radius: 15px;
            }

            .participant-menu-item {
                font-size: 11px;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def dashboard_peserta(nama, peserta, sudah_masuk, sudah_pulang):
    css_dashboard_peserta()

    sekarang = datetime.now(WITA)

    if sekarang.hour < 11:
        sapaan = "Selamat Pagi"
    elif sekarang.hour < 15:
        sapaan = "Selamat Siang"
    elif sekarang.hour < 18:
        sapaan = "Selamat Sore"
    else:
        sapaan = "Selamat Malam"

    tanggal = (
        f"{HARI_ID[sekarang.weekday()]}, "
        f"{sekarang.day} {BULAN_ID[sekarang.month - 1]} "
        f"{sekarang.year}"
    )

    # Escape data dinamis sebelum dimasukkan ke HTML.
    nama_html = escape(str(nama))
    divisi_html = escape(str(peserta["divisi"]))
    gedung_html = escape(str(peserta["gedung"]))

    # Tetap bisa digunakan jika database saat ini belum memiliki NIM.
    nim = peserta.get("nim")
    nim_html = (
        f"NIM: {escape(str(nim))}<br>"
        if nim else ""
    )

    status_masuk = "Sudah absen" if sudah_masuk else "Belum absen"
    status_pulang = "Sudah absen" if sudah_pulang else "Belum absen"

    if sudah_pulang:
        keterangan = "Presensi hari ini sudah lengkap. Terima kasih!"
    elif sudah_masuk:
        keterangan = "Absen masuk tercatat. Silakan lakukan absen pulang."
    else:
        keterangan = "Silakan lakukan absen masuk dengan selfie dan GPS."

    st.markdown(
        f"""
        <div class="presensi-hero">
            <div class="hero-top">
                <div class="hero-icon">◎</div>
                <div>
                    <div class="hero-title">Presensi</div>
                    <div class="hero-brand">PKL · BANK SULSELBAR</div>
                </div>
            </div>
            <div class="hero-greeting">{sapaan} 👋</div>
            <div class="hero-name">{nama_html}</div>
            <div class="hero-meta">
                {nim_html}
                Gedung {gedung_html} · {divisi_html}
            </div>
        </div>

        <div class="attendance-card">
            <div class="attendance-date-row">
                <div class="attendance-date">▢ {tanggal}</div>
                <div class="active-badge">Aktif</div>
            </div>

            <div class="attendance-grid">
                <div class="attendance-status status-masuk">
                    <div class="status-icon">↪</div>
                    <div class="status-label">Masuk</div>
                    <div class="status-value">{status_masuk}</div>
                </div>
                <div class="attendance-status status-pulang">
                    <div class="status-icon">↗</div>
                    <div class="status-label">Pulang</div>
                    <div class="status-value">{status_pulang}</div>
                </div>
            </div>

            <div class="attendance-notice">
                {keterangan}<br>
                Waktu dan lokasi pencatatan hanya dapat dilihat admin.
            </div>
        </div>

        <div class="participant-menu">
            <div class="participant-menu-title">Menu</div>
            <div class="participant-menu-grid">
                <div class="participant-menu-item">
                    <div class="participant-menu-icon menu-pink">📅</div>
                    Izin
                </div>
                <div class="participant-menu-item">
                    <div class="participant-menu-icon menu-blue">📥</div>
                    Approval<br>Izin
                </div>
                <div class="participant-menu-item">
                    <div class="participant-menu-icon menu-green">✈️</div>
                    Dinas Luar
                </div>
                <div class="participant-menu-item">
                    <div class="participant-menu-icon menu-purple">📄</div>
                    Usulan
                </div>
            </div>
            <div class="menu-note">
                Menu izin, approval, dinas luar, dan usulan belum tersedia.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
def halaman_absen(eng, nama):
    peserta = get_peserta(eng, nama)

    if peserta is None:
        st.error(
            "Data kamu tidak ditemukan. "
            "Silakan keluar lalu hubungi admin."
        )
        return

    gedung = peserta["gedung"]
    divisi = peserta["divisi"]

    hari_ini = datetime.now(WITA).strftime("%Y-%m-%d")

    sudah_masuk = sudah_berhasil(
        eng, nama, hari_ini, "masuk"
    )
    sudah_pulang = sudah_berhasil(
        eng, nama, hari_ini, "pulang"
    )

    dashboard_peserta(
        nama=nama,
        peserta=peserta,
        sudah_masuk=sudah_masuk,
        sudah_pulang=sudah_pulang,
    )

    flash = st.session_state.pop("flash_absen", None)
    if flash:
        st.success(flash[0])
        for baris in flash[1:]:
            st.info(baris)

    if sudah_pulang:
        if not flash:
            st.success(
                "✅ Absen hari ini sudah lengkap "
                "(masuk dan pulang). Terima kasih!"
            )
        return

    jenis = "pulang" if sudah_masuk else "masuk"

    st.subheader(f"📷 Absen {jenis.capitalize()}")
    st.caption(
        "Ambil foto selfie dan izinkan akses lokasi "
        "untuk melakukan presensi."
    )

    # Lanjutkan kode lama mulai dari:
    # lokasi = get_geolocation()

    lokasi = get_geolocation()
    coords = (lokasi or {}).get("coords")
    if coords:
        st.success("📡 Lokasi terdeteksi.")
    else:
        st.info("Menunggu lokasi... klik **Allow/Izinkan** saat browser meminta akses lokasi.")

    st.caption("Pastikan wajahmu terlihat jelas dan menghadap kamera. Foto tanpa wajah akan ditolak.")
    # key berbeda untuk masuk/pulang & tiap hari, supaya foto lama tidak ikut terpakai
    foto = st.camera_input("Foto selfie untuk absen", key=f"foto_{nama}_{hari_ini}_{jenis}")

    if st.button(f"Absen {jenis.capitalize()} Sekarang", type="primary", width="stretch"):
        if not coords:
            st.error("Lokasi belum terdeteksi. Aktifkan GPS/izin lokasi lalu coba lagi.")
            return
        if foto is None:
            st.error("Foto belum diambil.")
            return

        now = datetime.now(WITA)
        tanggal, jam = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")

        if sudah_berhasil(eng, nama, tanggal, jenis):
            st.warning(f"Kamu sudah absen {jenis} hari ini.")
            return
        if jenis == "pulang" and not sudah_berhasil(eng, nama, tanggal, "masuk"):
            st.error("Belum ada absen masuk yang berhasil hari ini, jadi absen pulang belum bisa.")
            return
        if jenis == "masuk" and now.time() > BATAS_JAM_MASUK:
            st.error(
                f"❌ Absen masuk ditolak: sudah lewat batas jam masuk "
                f"({BATAS_JAM_MASUK.strftime('%H:%M')} WITA)."
            )
            return

        data_foto = foto.getvalue()
        wajah = jumlah_wajah(data_foto)
        if wajah is None:
            st.error("Pendeteksi wajah sedang tidak aktif di server. Hubungi admin.")
            return
        if wajah == 0:
            st.error(
                "❌ Wajah tidak terdeteksi di foto. Pastikan wajahmu terlihat jelas, menghadap kamera, "
                "dan cahaya cukup, lalu ambil foto lagi."
            )
            return

        lat, lon = coords["latitude"], coords["longitude"]
        cocok, jarak = cek_lokasi(lat, lon, gedung)
        status = "berhasil" if cocok else "gagal_lokasi"
        uang = UANG_MAKAN if (cocok and jenis == "pulang") else 0  # uang makan muncul saat absen pulang

        simpan_absen(
            eng, nama=nama, tanggal=tanggal, jam=jam, jenis=jenis,
            lat=lat, lon=lon, akurasi=coords.get("accuracy"), jarak=jarak,
            status=status, uang_makan=uang, foto=kompres_foto(data_foto),
        )

        if cocok:
            pesan = [f"✅ Absen {jenis} berhasil."]
            if uang:
                pesan.append(f"💰 Uang makan hari ini tercatat: {rupiah(uang)}")
            st.session_state["flash_absen"] = pesan
            st.rerun()
        else:
            st.error(
                f"❌ Absen gagal: kamu terdeteksi di luar area Gedung {gedung}. "
                "Pastikan kamu sudah berada di gedung lalu coba lagi."
            )


# ------------------------------------------------------------------
# HALAMAN ADMIN: KELOLA PESERTA
# ------------------------------------------------------------------
def halaman_kelola_peserta(eng):
    st.header("👥 Kelola Peserta PKL")
    if not admin_login():
        return

    with st.form("tambah_peserta", clear_on_submit=True):
        nama = st.text_input("Nama peserta")
        divisi = st.selectbox("Divisi", list(DIVISI_GEDUNG.keys()))
        gedung = DIVISI_GEDUNG[divisi]
        pin = st.text_input("PIN (4-6 angka)", max_chars=6)
        kirim = st.form_submit_button("Tambah Peserta")

    if kirim:
        if not nama.strip():
            st.error("Nama wajib diisi.")
        elif not (pin.isdigit() and 4 <= len(pin) <= 6):
            st.error("PIN harus 4-6 angka.")
        else:
            try:
                add_peserta(eng, nama, gedung, divisi, pin)
                st.success(f"{nama.strip()} ditambahkan ke Gedung {gedung}. Kasih tahu PIN-nya ke yang bersangkutan.")
            except IntegrityError:
                st.error("Nama itu sudah terdaftar.")

    pesan = st.session_state.pop("pesan_hapus", None)
    if pesan:
        st.success(pesan)

    df = list_peserta(eng)
    st.dataframe(
        beri_nomor(df.rename(columns={"nama": "Nama", "gedung": "Gedung", "divisi": "Divisi"})),
        width="stretch",
    )

    if not df.empty:
        with st.expander("Edit Divisi / Reset PIN / Hapus peserta"):
            target = st.selectbox("Pilih peserta", df["nama"], key="target_kelola")

            opsi_divisi = list(DIVISI_GEDUNG.keys())
            divisi_lama = df.loc[df["nama"] == target, "divisi"].values[0]
            idx_default = opsi_divisi.index(divisi_lama) if divisi_lama in opsi_divisi else 0
            divisi_baru = st.selectbox("Divisi", opsi_divisi, index=idx_default, key="divisi_baru")
            st.caption(f"Ditempatkan di: Gedung {DIVISI_GEDUNG[divisi_baru]}")
            if st.button("Simpan Divisi"):
                set_divisi(eng, target, divisi_baru, DIVISI_GEDUNG[divisi_baru])
                st.success("Divisi & Gedung diperbarui.")
                st.rerun()

            pin_baru = st.text_input("PIN baru (4-6 angka)", max_chars=6, key="pin_baru")
            hapus_absen_juga = st.checkbox(
                "Saat menghapus peserta, hapus juga seluruh riwayat absensinya",
                key="hapus_absen_juga",
            )
            c1, c2 = st.columns(2)
            if c1.button("Reset PIN"):
                if pin_baru.isdigit() and 4 <= len(pin_baru) <= 6:
                    reset_pin(eng, target, pin_baru)
                    st.success("PIN diperbarui.")
                else:
                    st.error("PIN harus 4-6 angka.")
            if c2.button("Hapus peserta"):
                hapus_peserta(eng, target, dengan_absensi=hapus_absen_juga)
                if hapus_absen_juga:
                    st.session_state["pesan_hapus"] = f"{target} dihapus beserta seluruh riwayat absensinya."
                else:
                    st.session_state["pesan_hapus"] = (
                        f"{target} dihapus dari daftar peserta. Riwayat absensinya masih tersimpan "
                        "(hapus lewat bagian 'Hapus data absensi' di bawah kalau perlu)."
                    )
                st.rerun()

    with st.expander("🗑️ Hapus data absensi (tidak bisa dibatalkan)"):
        nama_absen = daftar_nama_absensi(eng)
        if not nama_absen:
            st.info("Belum ada data absensi.")
        else:
            semua = "(SEMUA peserta)"
            pilihan = st.selectbox("Hapus absensi milik", [semua] + nama_absen, key="hapus_absen_pilihan")
            konfirmasi = st.text_input("Ketik HAPUS (huruf besar) untuk konfirmasi", key="hapus_absen_konfirmasi")
            if st.button("Hapus data absensi", key="tombol_hapus_absen"):
                if konfirmasi.strip() != "HAPUS":
                    st.error("Konfirmasi belum benar. Ketik persis: HAPUS")
                else:
                    jumlah = hapus_absensi(eng, None if pilihan == semua else pilihan)
                    st.success(f"{jumlah} data absensi (beserta fotonya) dihapus.")


# ------------------------------------------------------------------
# HALAMAN ADMIN: REKAP, GRAFIK, EXPORT
# ------------------------------------------------------------------
def halaman_rekap(eng):
    st.header("📊 Rekap Kehadiran & Uang Makan")
    if not admin_login():
        return

    hari_ini = datetime.now(WITA).date()
    c1, c2 = st.columns(2)
    bulan = c1.selectbox("Bulan", list(range(1, 13)), index=hari_ini.month - 1, format_func=lambda m: BULAN_ID[m - 1])
    tahun = c2.number_input("Tahun", value=hari_ini.year, step=1)
    bulan, tahun = int(bulan), int(tahun)

    df = get_absensi_bulan(eng, tahun, bulan)
    if df.empty:
        st.info("Belum ada data absensi di bulan ini.")
        return

    peserta_df = list_peserta(eng)
    harian = rekap_harian(df)
    per_peserta = rekap_peserta(df)
    berhasil = df[df["status"] == "berhasil"]

    m1, m2, m3 = st.columns(3)
    m1.metric("Total uang makan bulan ini", rupiah(per_peserta["Total Uang Makan"].sum()))
    m2.metric("Peserta yang hadir", int(berhasil[berhasil["jenis"] == "masuk"]["nama"].nunique()))
    m3.metric("Percobaan gagal / ditolak", int((df["status"] != "berhasil").sum()))

    tab_uang, tab_hadir, tab_detail = st.tabs(["💰 Uang Makan", "🗓️ Kehadiran", "📍 Lokasi & Waktu"])

    with tab_uang:
        st.subheader("Akumulasi Uang Makan per Hari")
        if harian.empty:
            st.info("Belum ada absen pulang yang berhasil. Uang makan tercatat saat peserta absen pulang.")
        else:
            st.dataframe(beri_nomor(harian), width="stretch")
            st.caption("Uang makan per hari")
            st.bar_chart(harian.set_index("Tanggal")["Uang Makan Hari Ini"])
            st.caption("Akumulasi uang makan")
            st.line_chart(harian.set_index("Tanggal")["Akumulasi Uang Makan"])
        st.subheader("Total per Peserta")
        st.dataframe(beri_nomor(per_peserta), width="stretch")

    with tab_hadir:
        st.subheader("Rekapan Kehadiran")
        st.caption("✓ = masuk dan pulang  •  M = hanya masuk  •  kosong = tidak hadir")
        st.dataframe(beri_nomor(matriks_kehadiran(df, peserta_df, tahun, bulan)), width="stretch")

    with tab_detail:
        st.subheader("Lokasi & Waktu Absensi")
        st.caption("Informasi ini hanya bisa dilihat admin.")
        st.dataframe(
            beri_nomor(tabel_detail(df)),
            width="stretch",
            column_config={"Peta": st.column_config.LinkColumn("Peta", display_text="Buka peta")},
        )

    st.download_button(
        "⬇️ Download Excel",
        data=excel_bytes(df, peserta_df, tahun, bulan),
        file_name=f"rekap_absensi_{tahun}_{bulan:02d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ------------------------------------------------------------------
# HALAMAN ADMIN: BUKTI FOTO
# ------------------------------------------------------------------
def halaman_foto(eng):
    st.header("🖼️ Bukti Foto Absensi")
    if not admin_login():
        return

    tgl = st.date_input("Tanggal", value=datetime.now(WITA).date())
    rows = get_absensi_hari_foto(eng, tgl.strftime("%Y-%m-%d"))
    if not rows:
        st.info("Tidak ada absensi di tanggal ini.")
        return

    kolom = st.columns(3)
    for i, r in enumerate(rows):
        with kolom[i % 3]:
            if r["foto"]:
                st.image(bytes(r["foto"]), width="stretch")
            ikon = "✅" if r["status"] == "berhasil" else "❌"
            st.caption(f"{ikon} {r['nama']}\n\n{r['jenis']} • {r['jam']} • {r['jarak']:.0f} m")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def keluar():
    """Logout: hapus peran & data login, kembali ke layar login."""
    for k in ("peran", "nama_login", "admin_ok"):
        st.session_state.pop(k, None)
    st.rerun()


def halaman_login(eng):
    """Layar pertama: pilih peran, lalu login. Peserta -> halaman absen, Admin -> area admin."""
    st.title("📍 Absensi PKL")
    st.caption("Bank Sulselbar")

    peran = st.radio("Masuk sebagai", ["Peserta PKL", "Admin"], horizontal=True, key="login_peran")

    if peran == "Peserta PKL":
        df = list_peserta(eng)
        if df.empty:
            st.warning("Belum ada peserta terdaftar. Hubungi admin untuk didaftarkan.")
            return
        with st.form("login_peserta"):
            nama = st.selectbox("Nama", df["nama"])
            pin = st.text_input("PIN", type="password", max_chars=6)
            kirim = st.form_submit_button("Masuk", type="primary", width="stretch")
        if kirim:
            if cek_pin(eng, nama, pin):
                st.session_state.peran = "user"
                st.session_state.nama_login = nama
                st.rerun()
            else:
                st.error("PIN salah.")
    else:
        pw_benar = password_admin()
        if pw_benar is None:
            st.error("admin_password belum diatur di Secrets. Hubungi pengelola aplikasi.")
            return
        if not secret("admin_password") and not secret("database_url"):
            st.info("Mode lokal: password admin default = admin123")
        with st.form("login_admin"):
            pw = st.text_input("Password admin", type="password")
            kirim = st.form_submit_button("Masuk", type="primary", width="stretch")
        if kirim:
            if hmac.compare_digest(pw, pw_benar):
                st.session_state.peran = "admin"
                st.session_state.admin_ok = True
                st.rerun()
            else:
                st.error("Password salah.")


def halaman_admin(eng):
    """Area admin (setelah login): Kelola Peserta / Rekap / Bukti Foto."""
    kiri, kanan = st.columns([4, 1])
    kiri.caption("Masuk sebagai admin")
    if kanan.button("Keluar", key="keluar_admin", width="stretch"):
        keluar()

    menu = st.radio(
        "Menu admin",
        ["Kelola Peserta", "Rekap & Export", "Bukti Foto"],
        horizontal=True,
        label_visibility="collapsed",
        key="menu_admin",
    )
    if menu == "Kelola Peserta":
        halaman_kelola_peserta(eng)
    elif menu == "Rekap & Export":
        halaman_rekap(eng)
    else:
        halaman_foto(eng)


def main():
    st.set_page_config(page_title="Absensi PKL - Bank Sulselbar", page_icon="📍")
    eng = get_engine()

    peran = st.session_state.get("peran")
    nama = st.session_state.get("nama_login")
   if peran == "user" and nama:
    kiri, kanan = st.columns([3, 1])

    kiri.caption("BANK SULSELBAR · PRESENSI PKL")

    if kanan.button(
        "Keluar ↗",
        key="keluar_user",
        width="stretch",
    ):
        keluar()

    halaman_absen(eng, nama)
    elif peran == "admin" and st.session_state.get("admin_ok"):
        halaman_admin(eng)
    else:
        halaman_login(eng)


if __name__ == "__main__":
    main()
