"""
Aplikasi Absensi Peserta PKL - Bank Sulselbar (versi siap deploy)
=================================================================
Fitur:
- Layar login di awal: pilih peran. Peserta (nama + NIM) -> beranda peserta; Admin (password) -> beranda admin
- Absen MASUK lalu PULANG: foto selfie + lokasi GPS (urutan otomatis)
- Foto harus ada wajah (deteksi wajah, bukan pengenalan); tanpa wajah ditolak dan tidak disimpan
- Peserta didaftarkan admin dengan nama + NIM (dipakai untuk login)
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
import hmac
import html as _html
import io
from datetime import datetime, date, time as dtime, timedelta, timezone
from math import radians, sin, cos, sqrt, atan2

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
    Column("nim", String(30), unique=True),
    Column("gedung", String(1), nullable=False),
    Column("divisi", String(100), nullable=False, default=""),
    Column("pin_hash", String(64), nullable=False, default=""),  # tidak dipakai lagi (login pakai nama + NIM)
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


def _pastikan_kolom_nim(eng):
    """Database lama (sebelum ada NIM) otomatis ditambah kolom nim, tanpa menghapus data."""
    from sqlalchemy import inspect, text
    if "nim" in {k["name"] for k in inspect(eng).get_columns("peserta")}:
        return
    with eng.begin() as c:
        c.execute(text("ALTER TABLE peserta ADD COLUMN nim VARCHAR(30)"))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_peserta_nim ON peserta (nim)"))


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
    _pastikan_kolom_nim(eng)
    return eng


def norm_nama(s):
    return " ".join(str(s).split()).casefold()


def norm_nim(s):
    return "".join(str(s).split()).casefold()


def nim_valid(nim):
    nim = "".join(str(nim).split())
    return 4 <= len(nim) <= 20 and nim.isalnum()


def list_peserta(eng):
    q = select(
        peserta_t.c.nama, peserta_t.c.nim, peserta_t.c.gedung, peserta_t.c.divisi,
    ).order_by(peserta_t.c.nama)
    with eng.connect() as c:
        df = pd.read_sql(q, c)
    df["nim"] = df["nim"].fillna("")
    return df


def get_peserta(eng, nama):
    with eng.connect() as c:
        return c.execute(select(peserta_t).where(peserta_t.c.nama == nama)).mappings().first()


def nim_sudah_dipakai(eng, nim, kecuali_nama=None):
    """Cek NIM ganda tanpa membedakan huruf besar/kecil dan spasi (sama seperti saat login)."""
    q = select(func.count()).select_from(peserta_t).where(
        func.replace(func.lower(peserta_t.c.nim), " ", "") == norm_nim(nim)
    )
    if kecuali_nama:
        q = q.where(peserta_t.c.nama != kecuali_nama)
    with eng.connect() as c:
        return c.execute(q).scalar() > 0


def add_peserta(eng, nama, nim, gedung, divisi):
    """Tambah peserta. Melempar ValueError kalau NIM sudah dipakai, IntegrityError kalau nama sudah ada."""
    if nim_sudah_dipakai(eng, nim):
        raise ValueError("NIM sudah dipakai")
    with eng.begin() as c:
        c.execute(insert(peserta_t).values(
            nama=" ".join(nama.split()), nim="".join(nim.split()),
            gedung=gedung, divisi=divisi.strip(), pin_hash="",
        ))


def ganti_nim(eng, nama, nim):
    """Ubah NIM peserta. Melempar ValueError kalau NIM itu sudah dipakai peserta lain."""
    if nim_sudah_dipakai(eng, nim, kecuali_nama=nama):
        raise ValueError("NIM sudah dipakai")
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(nim="".join(nim.split())))


def cek_login_peserta(eng, nama, nim):
    """Kembalikan nama peserta (persis seperti data admin) kalau NIM dan nama cocok, selain itu None."""
    kunci = norm_nim(nim)
    if not kunci:
        return None
    q = select(peserta_t.c.nama).where(func.replace(func.lower(peserta_t.c.nim), " ", "") == kunci)
    with eng.connect() as c:
        row = c.execute(q).first()
    if row is None or norm_nama(row[0]) != norm_nama(nama):
        return None
    return row[0]


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


def status_hari_ini(eng, nama, tanggal):
    """Jam masuk, jam pulang (yang berhasil) dan uang makan seorang peserta pada satu tanggal."""
    q = select(absensi_t.c.jenis, absensi_t.c.jam, absensi_t.c.uang_makan).where(and_(
        absensi_t.c.nama == nama, absensi_t.c.tanggal == tanggal, absensi_t.c.status == "berhasil",
    )).order_by(absensi_t.c.jam)
    masuk = pulang = None
    uang = 0
    with eng.connect() as c:
        for jenis, jam, u in c.execute(q).all():
            if jenis == "masuk" and masuk is None:
                masuk = jam
            elif jenis == "pulang" and pulang is None:
                pulang = jam
            uang += u or 0
    return {"masuk": masuk, "pulang": pulang, "uang": int(uang)}


def riwayat_peserta(eng, nama, batas=60):
    """Riwayat presensi yang berhasil milik satu peserta, per tanggal (terbaru dulu)."""
    q = select(
        absensi_t.c.tanggal, absensi_t.c.jam, absensi_t.c.jenis, absensi_t.c.uang_makan,
    ).where(and_(absensi_t.c.nama == nama, absensi_t.c.status == "berhasil"))
    with eng.connect() as c:
        df = pd.read_sql(q, c)
    kolom = ["Tanggal", "Masuk", "Pulang", "Uang Makan"]
    if df.empty:
        return pd.DataFrame(columns=kolom)
    out = pd.DataFrame({
        "Masuk": df[df["jenis"] == "masuk"].groupby("tanggal")["jam"].min().str[:5],
        "Pulang": df[df["jenis"] == "pulang"].groupby("tanggal")["jam"].max().str[:5],
        "Uang Makan": df.groupby("tanggal")["uang_makan"].sum(),
    }).sort_index(ascending=False).head(batas)
    out["Masuk"] = out["Masuk"].fillna("-")
    out["Pulang"] = out["Pulang"].fillna("-")
    out["Uang Makan"] = out["Uang Makan"].fillna(0).astype(int)
    out.index.name = "Tanggal"
    return out.reset_index()[kolom]


def ringkasan_hari_ini(eng, tanggal):
    """Angka ringkas untuk beranda admin."""
    ok = and_(absensi_t.c.tanggal == tanggal, absensi_t.c.status == "berhasil")
    with eng.connect() as c:
        total = c.execute(select(func.count()).select_from(peserta_t)).scalar()
        hadir = c.execute(select(func.count(func.distinct(absensi_t.c.nama))).where(and_(ok, absensi_t.c.jenis == "masuk"))).scalar()
        pulang = c.execute(select(func.count(func.distinct(absensi_t.c.nama))).where(and_(ok, absensi_t.c.jenis == "pulang"))).scalar()
        uang = c.execute(select(func.coalesce(func.sum(absensi_t.c.uang_makan), 0)).where(ok)).scalar()
    return {"total": int(total), "hadir": int(hadir), "pulang": int(pulang), "uang": int(uang)}


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
def halaman_absen(eng, nama):
    st.markdown(html_judul("Presensi", "Foto selfie + lokasi gedung"), unsafe_allow_html=True)

    peserta = get_peserta(eng, nama)
    if peserta is None:
        st.error("Data kamu tidak ditemukan (mungkin sudah dihapus admin). Silakan keluar lalu hubungi admin.")
        return
    gedung, divisi = peserta["gedung"], peserta["divisi"]

    st.text_input("Nama", value=nama, disabled=True)
    st.text_input(
        "Gedung & Divisi (terdaftar, tidak bisa diubah)",
        value=f"Gedung {gedung} • {divisi}",
        disabled=True,
    )

    # Urutan otomatis: belum absen -> MASUK, sudah masuk -> PULANG, sudah keduanya -> selesai
    hari_ini = datetime.now(WITA).strftime("%Y-%m-%d")
    sudah_masuk = sudah_berhasil(eng, nama, hari_ini, "masuk")
    if sudah_berhasil(eng, nama, hari_ini, "pulang"):
        st.success("✅ Absen hari ini sudah lengkap (masuk dan pulang). Terima kasih!")
        return

    jenis = "pulang" if sudah_masuk else "masuk"
    st.subheader(f"Absen {jenis.capitalize()}")
    if sudah_masuk:
        st.caption("Absen masuk hari ini sudah tercatat. Sekarang waktunya absen pulang.")

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
            st.session_state["layar_user"] = "beranda"  # kembali ke beranda yang sudah diperbarui
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
    st.markdown(html_judul("Kelola Peserta", "Tambah, ubah, dan hapus peserta PKL"), unsafe_allow_html=True)
    if not admin_login():
        return

    with st.form("tambah_peserta", clear_on_submit=True):
        nama = st.text_input("Nama peserta")
        nim = st.text_input("NIM", max_chars=20)
        divisi = st.selectbox("Divisi", list(DIVISI_GEDUNG.keys()))
        gedung = DIVISI_GEDUNG[divisi]
        kirim = st.form_submit_button("Tambah Peserta")

    if kirim:
        nama_rapi = " ".join(nama.split())
        if not nama_rapi:
            st.error("Nama wajib diisi.")
        elif not nim_valid(nim):
            st.error("NIM harus 4-20 karakter, hanya huruf dan angka.")
        else:
            try:
                add_peserta(eng, nama_rapi, nim, gedung, divisi)
                st.success(f"{nama_rapi} ditambahkan ke Gedung {gedung}. Peserta masuk dengan nama dan NIM ini.")
            except (IntegrityError, ValueError):
                st.error("Nama atau NIM itu sudah terdaftar.")

    pesan = st.session_state.pop("pesan_kelola", None)
    if pesan:
        st.success(pesan)

    df = list_peserta(eng)
    tanpa_nim = int((df["nim"] == "").sum())
    if tanpa_nim:
        st.warning(f"{tanpa_nim} peserta belum punya NIM, jadi belum bisa masuk. Isi lewat 'Ubah NIM' di bawah.")
    st.dataframe(
        beri_nomor(df.rename(columns={"nama": "Nama", "nim": "NIM", "gedung": "Gedung", "divisi": "Divisi"})),
        width="stretch",
    )

    if not df.empty:
        with st.expander("Edit Divisi / Ubah NIM / Hapus peserta"):
            target = st.selectbox("Pilih peserta", df["nama"], key="target_kelola")

            opsi_divisi = list(DIVISI_GEDUNG.keys())
            divisi_lama = df.loc[df["nama"] == target, "divisi"].values[0]
            idx_default = opsi_divisi.index(divisi_lama) if divisi_lama in opsi_divisi else 0
            divisi_baru = st.selectbox("Divisi", opsi_divisi, index=idx_default, key=f"divisi_baru_{target}")
            st.caption(f"Ditempatkan di: Gedung {DIVISI_GEDUNG[divisi_baru]}")
            if st.button("Simpan Divisi", key="simpan_divisi"):
                set_divisi(eng, target, divisi_baru, DIVISI_GEDUNG[divisi_baru])
                st.session_state["pesan_kelola"] = f"Divisi {target} diperbarui (Gedung {DIVISI_GEDUNG[divisi_baru]})."
                st.rerun()

            nim_lama = df.loc[df["nama"] == target, "nim"].values[0]
            nim_baru = st.text_input("NIM", value=nim_lama, max_chars=20, key=f"nim_baru_{target}")
            if st.button("Simpan NIM", key="simpan_nim"):
                if not nim_valid(nim_baru):
                    st.error("NIM harus 4-20 karakter, hanya huruf dan angka.")
                else:
                    try:
                        ganti_nim(eng, target, nim_baru)
                        st.session_state["pesan_kelola"] = f"NIM {target} diperbarui."
                        st.rerun()
                    except (IntegrityError, ValueError):
                        st.error("NIM itu sudah dipakai peserta lain.")

            hapus_absen_juga = st.checkbox(
                "Saat menghapus peserta, hapus juga seluruh riwayat absensinya",
                key="hapus_absen_juga",
            )
            if st.button("Hapus peserta", key="hapus_peserta"):
                hapus_peserta(eng, target, dengan_absensi=hapus_absen_juga)
                if hapus_absen_juga:
                    st.session_state["pesan_kelola"] = f"{target} dihapus beserta seluruh riwayat absensinya."
                else:
                    st.session_state["pesan_kelola"] = (
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
    st.markdown(html_judul("Rekap Kehadiran & Uang Makan", "Khusus admin"), unsafe_allow_html=True)
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
    st.markdown(html_judul("Bukti Foto Absensi", "Khusus admin"), unsafe_allow_html=True)
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
# ------------------------------------------------------------------
# TAMPILAN: LOGIN, BERANDA PESERTA, BERANDA ADMIN
# ------------------------------------------------------------------
HARI_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

CSS = """<style>
.ps-hero{background:#1d4ed8;background:-webkit-linear-gradient(top,#2563eb,#4338ca);background:linear-gradient(160deg,#2563eb 0%,#1d4ed8 55%,#4338ca 100%);border-radius:22px;padding:18px 20px 22px;color:#fff;margin-bottom:12px}
.ps-brand{font-size:1.2rem;font-weight:700;color:#fff}
.ps-ico{display:inline-block;width:36px;height:36px;line-height:36px;text-align:center;border-radius:11px;background:rgba(255,255,255,.22);margin-right:10px}
.ps-hi{margin-top:16px;font-size:1rem;color:#fff}
.ps-nama{font-size:1.4rem;font-weight:700;line-height:1.25;color:#fff;word-wrap:break-word}
.ps-sub{margin-top:3px;font-size:.85rem;color:#dbeafe}
.ps-card{background:#fff;color:#1f2937;border-radius:20px;padding:16px;box-shadow:0 6px 18px rgba(30,64,175,.14);margin-bottom:10px}
.ps-tgl{overflow:hidden;font-weight:600;font-size:1.02rem;color:#1f2937}
.ps-tgl .t{float:left;padding-top:3px}
.ps-badge{float:right;border-radius:999px;padding:4px 14px;font-size:.85rem;font-weight:700}
.ps-aktif{background:#d1fae5;color:#047857}
.ps-selesai{background:#e0e7ff;color:#3730a3}
.ps-grid{display:table;table-layout:fixed;width:100%;border-spacing:8px 0;margin-top:12px}
.ps-cell{display:table-cell}
.ps-box{border-radius:16px;padding:12px 4px 0;text-align:center;height:76px;box-sizing:border-box}
.ps-box .l{font-size:.85rem;font-weight:600}
.ps-box .v{font-size:1.25rem;font-weight:800;margin-top:6px}
.ps-merah .v{font-size:1.05rem}
.ps-hijau{background:#ecfdf5;color:#047857}
.ps-kuning{background:#fffbeb;color:#b45309}
.ps-merah{background:#fef2f2;color:#b91c1c}
.ps-info{margin-top:14px;background:#eff6ff;color:#1e40af;border-radius:14px;padding:12px 14px;text-align:center;font-size:.92rem;line-height:1.5}
.ps-judul{padding:14px 18px;margin-bottom:14px}
.ps-judul .ps-brand{font-size:1.1rem}
.ps-judul .ps-ico{float:left;margin-right:12px}
.ps-jt{overflow:hidden;line-height:1.3;padding-top:2px}
.ps-judul .ps-sub{margin-top:3px;font-weight:400}
div[data-testid="stButton"] button,div[data-testid="stFormSubmitButton"] button{border-radius:14px;min-height:3rem;font-weight:600}
button[kind="primary"],button[kind="primaryFormSubmit"],[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{background:#2563eb !important;border-color:#2563eb !important;color:#fff !important}
</style>"""


def tanggal_id(d):
    return f"{HARI_ID[d.weekday()]}, {d.day} {BULAN_ID[d.month - 1]} {d.year}"


def sapaan(jam):
    if jam < 11:
        return "Selamat Pagi"
    if jam < 15:
        return "Selamat Siang"
    if jam < 18:
        return "Selamat Sore"
    return "Selamat Malam"


def jam_menit(jam):
    """'07:28:05' -> '07:28'; kosong -> '--:--'."""
    return jam[:5] if jam else "--:--"


def lama_kerja(masuk, pulang):
    """masuk/pulang berupa 'HH:MM:SS'. Hasil: '8 jam 12 menit', atau '-' kalau belum lengkap."""
    if not masuk or not pulang:
        return "-"
    fmt = "%H:%M:%S"
    menit = int((datetime.strptime(pulang, fmt) - datetime.strptime(masuk, fmt)).total_seconds() // 60)
    if menit < 0:
        return "-"
    return f"{menit // 60} jam {menit % 60} menit"


def _e(teks):
    return _html.escape(str(teks))


def html_hero(brand, sapa, nama, sub=""):
    sub_html = f'<div class="ps-sub">{_e(sub)}</div>' if sub else ""
    return (
        f'<div class="ps-hero"><div class="ps-brand"><span class="ps-ico">📍</span>{_e(brand)}</div>'
        f'<div class="ps-hi">{_e(sapa)} 👋</div><div class="ps-nama">{_e(nama)}</div>{sub_html}</div>'
    )


def html_judul(judul, sub=""):
    """Banner biru ringkas untuk judul halaman (selaras dengan beranda)."""
    sub_html = f'<div class="ps-sub">{_e(sub)}</div>' if sub else ""
    return (
        f'<div class="ps-hero ps-judul"><div class="ps-brand"><span class="ps-ico">📍</span>'
        f'<div class="ps-jt">{_e(judul)}{sub_html}</div></div></div>'
    )


def html_kartu(tanggal_teks, badge, kelas_badge, kotak, info_html):
    """kotak = [(kelas_warna, judul, nilai), ...]; info_html sudah aman (dibuat di kode ini)."""
    sel = "".join(
        f'<div class="ps-cell"><div class="ps-box {k}"><div class="l">{_e(j)}</div><div class="v">{_e(v)}</div></div></div>'
        for k, j, v in kotak
    )
    return (
        f'<div class="ps-card"><div class="ps-tgl"><span class="t">📅 {_e(tanggal_teks)}</span>'
        f'<span class="ps-badge {kelas_badge}">{_e(badge)}</span></div>'
        f'<div class="ps-grid">{sel}</div><div class="ps-info">{info_html}</div></div>'
    )


def keluar():
    """Logout: hapus peran & data login, kembali ke layar login."""
    for k in ("peran", "nama_login", "admin_ok", "layar_user", "layar_admin", "flash_absen", "pesan_kelola"):
        st.session_state.pop(k, None)
    st.rerun()


def baris_navigasi(awalan, kunci_layar, kembali):
    kiri, kanan = st.columns(2)
    if kembali and kiri.button("← Beranda", key=f"{awalan}_beranda", width="stretch"):
        st.session_state[kunci_layar] = "beranda"
        st.rerun()
    if kanan.button("Keluar", key=f"{awalan}_keluar", icon=":material/logout:", width="stretch"):
        keluar()


def halaman_login(eng):
    """Layar pertama: pilih peran. Peserta -> nama + NIM, Admin -> password."""
    jam = datetime.now(WITA).hour
    st.markdown(html_hero("Presensi", sapaan(jam), "Silakan masuk", "Bank Sulselbar"), unsafe_allow_html=True)

    peran = st.radio("Masuk sebagai", ["Peserta PKL", "Admin"], horizontal=True, key="login_peran")

    if peran == "Peserta PKL":
        with st.form("login_peserta"):
            nama = st.text_input("Nama", placeholder="Nama lengkap sesuai pendaftaran")
            nim = st.text_input("NIM", max_chars=20)
            kirim = st.form_submit_button("Masuk", type="primary", width="stretch")
        if kirim:
            nama_db = cek_login_peserta(eng, nama, nim)
            if nama_db:
                st.session_state.peran = "user"
                st.session_state.nama_login = nama_db
                st.session_state.layar_user = "beranda"
                st.rerun()
            else:
                st.error("Nama atau NIM tidak cocok. Periksa lagi, atau hubungi admin.")
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
                st.session_state.layar_admin = "beranda"
                st.rerun()
            else:
                st.error("Password salah.")


# ---------------------------- peserta ----------------------------
def beranda_peserta(eng, nama):
    peserta = get_peserta(eng, nama)
    if peserta is None:
        st.error("Data kamu tidak ditemukan (mungkin sudah dihapus admin). Silakan keluar lalu hubungi admin.")
        return

    sekarang = datetime.now(WITA)
    s = status_hari_ini(eng, nama, sekarang.strftime("%Y-%m-%d"))
    lengkap = bool(s["masuk"] and s["pulang"])
    bagian = [f"NIM {peserta['nim']}" if peserta["nim"] else "", f"Gedung {peserta['gedung']}", peserta["divisi"]]
    sub = " • ".join(b for b in bagian if b)

    st.markdown(html_hero("Presensi", sapaan(sekarang.hour), nama, sub), unsafe_allow_html=True)

    flash = st.session_state.pop("flash_absen", None)
    if flash:
        st.success(flash[0])
        for baris in flash[1:]:
            st.info(baris)

    kotak = [
        ("ps-hijau", "Masuk", jam_menit(s["masuk"])),
        ("ps-kuning", "Pulang", jam_menit(s["pulang"])),
        ("ps-merah", "Uang Makan", rupiah(s["uang"]) if s["uang"] else "-"),
    ]
    info = (
        f"Total jam kerja: <b>{_e(lama_kerja(s['masuk'], s['pulang']))}</b><br>"
        f"Waktu presensi: masuk sebelum {BATAS_JAM_MASUK.strftime('%H:%M')} WITA"
    )
    st.markdown(
        html_kartu(tanggal_id(sekarang.date()), "Selesai" if lengkap else "Aktif",
                   "ps-selesai" if lengkap else "ps-aktif", kotak, info),
        unsafe_allow_html=True,
    )

    if lengkap:
        st.success("✅ Presensi hari ini sudah lengkap. Terima kasih!")
    else:
        label = "Presensi Pulang" if s["masuk"] else "Presensi Masuk"
        if st.button(label, type="primary", icon=":material/fingerprint:", width="stretch", key="cta_presensi"):
            st.session_state.layar_user = "presensi"
            st.rerun()

    st.subheader("Menu")
    kiri, kanan = st.columns(2)
    if kiri.button("Riwayat", icon=":material/history:", width="stretch", key="menu_riwayat"):
        st.session_state.layar_user = "riwayat"
        st.rerun()
    if kanan.button("Profil", icon=":material/person:", width="stretch", key="menu_profil"):
        st.session_state.layar_user = "profil"
        st.rerun()


def halaman_riwayat(eng, nama):
    st.markdown(html_judul("Riwayat Presensi", "Presensi yang berhasil"), unsafe_allow_html=True)
    df = riwayat_peserta(eng, nama)
    if df.empty:
        st.info("Belum ada presensi yang berhasil.")
        return
    st.dataframe(beri_nomor(df), width="stretch")


def halaman_profil(eng, nama):
    st.markdown(html_judul("Profil", "Data diri peserta"), unsafe_allow_html=True)
    peserta = get_peserta(eng, nama)
    if peserta is None:
        st.error("Data kamu tidak ditemukan. Silakan keluar lalu hubungi admin.")
        return
    for label, nilai in [
        ("Nama", nama),
        ("NIM", peserta["nim"] or "-"),
        ("Gedung", f"Gedung {peserta['gedung']}"),
        ("Divisi", peserta["divisi"] or "-"),
    ]:
        st.text_input(label, value=nilai, disabled=True, key=f"profil_{label}")
    st.caption("Data ini diatur oleh admin dan tidak bisa diubah dari sini.")


def area_peserta(eng, nama):
    layar = st.session_state.get("layar_user", "beranda")
    baris_navigasi("user", "layar_user", kembali=(layar != "beranda"))
    if layar == "presensi":
        halaman_absen(eng, nama)
    elif layar == "riwayat":
        halaman_riwayat(eng, nama)
    elif layar == "profil":
        halaman_profil(eng, nama)
    else:
        beranda_peserta(eng, nama)


# ------------------------------ admin ------------------------------
def beranda_admin(eng):
    sekarang = datetime.now(WITA)
    r = ringkasan_hari_ini(eng, sekarang.strftime("%Y-%m-%d"))
    st.markdown(html_hero("Presensi", sapaan(sekarang.hour), "Administrator", "Bank Sulselbar"), unsafe_allow_html=True)

    kotak = [
        ("ps-hijau", "Hadir", str(r["hadir"])),
        ("ps-kuning", "Pulang", str(r["pulang"])),
        ("ps-merah", "Uang Makan", rupiah(r["uang"])),
    ]
    belum = max(r["total"] - r["hadir"], 0)
    info = f"<b>{r['total']}</b> peserta terdaftar<br>{belum} belum absen masuk hari ini"
    st.markdown(html_kartu(tanggal_id(sekarang.date()), "Aktif", "ps-aktif", kotak, info), unsafe_allow_html=True)

    st.subheader("Menu")
    a, b, c = st.columns(3)
    if a.button("Peserta", icon=":material/group:", width="stretch", key="menu_kelola"):
        st.session_state.layar_admin = "kelola"
        st.rerun()
    if b.button("Rekap", icon=":material/bar_chart:", width="stretch", key="menu_rekap"):
        st.session_state.layar_admin = "rekap"
        st.rerun()
    if c.button("Foto", icon=":material/photo_camera:", width="stretch", key="menu_foto"):
        st.session_state.layar_admin = "foto"
        st.rerun()


def area_admin(eng):
    layar = st.session_state.get("layar_admin", "beranda")
    baris_navigasi("admin", "layar_admin", kembali=(layar != "beranda"))
    if layar == "kelola":
        halaman_kelola_peserta(eng)
    elif layar == "rekap":
        halaman_rekap(eng)
    elif layar == "foto":
        halaman_foto(eng)
    else:
        beranda_admin(eng)


def main():
    st.set_page_config(page_title="Presensi PKL - Bank Sulselbar", page_icon="📍")
    st.markdown(CSS, unsafe_allow_html=True)
    eng = get_engine()

    peran = st.session_state.get("peran")
    nama = st.session_state.get("nama_login")
    if peran == "user" and nama:
        area_peserta(eng, nama)
    elif peran == "admin" and st.session_state.get("admin_ok"):
        area_admin(eng)
    else:
        halaman_login(eng)


if __name__ == "__main__":
    main()
