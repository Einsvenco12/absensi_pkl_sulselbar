"""
Aplikasi Absensi Peserta PKL - Bank Sulselbar (Versi Pembaruan UI & Fitur Izin)
=============================================================================
Jalanin lokal: streamlit run app.py
"""

import calendar
import hashlib
import hmac
import io
import cv2
import numpy as np
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
# KONFIGURASI - UTAMA
# ------------------------------------------------------------------
UANG_MAKAN = 15000
RADIUS_METER = 150  
BATAS_JAM_MASUK = dtime(8, 0)  
WITA = timezone(timedelta(hours=8))  

# Koordinat Gedung A dan B Bank Sulselbar
GEDUNG_COORD = {
    "A": (-5.150070542826675, 119.41568236911743),  
    "B": (-5.150163347858429, 119.41329137592547),  
}

DIVISI_GEDUNG = {
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
    "Divisi Kepatuhan": "B",
    "Divisi Dana dan Jasa": "B",
    "Divisi Syariah": "B",
    "Divisi Manajemen Risiko": "B",
    "Divisi SKAI": "B",
}

BULAN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
            "September", "Oktober", "November", "Desember"]

# ------------------------------------------------------------------
# HELPER: SECRETS
# ------------------------------------------------------------------
def secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

# ------------------------------------------------------------------
# DATABASE (SQLAlchemy Hibrida: SQLite Lokal / Postgres Cloud)
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
    Column("tanggal", String(10), nullable=False, index=True),  
    Column("jam", String(8), nullable=False),                   
    Column("jenis", String(10), nullable=False, default="masuk"),  
    Column("lat", Float),
    Column("lon", Float),
    Column("akurasi", Float),
    Column("jarak", Float),
    Column("status", String(20), nullable=False),               
    Column("uang_makan", Integer, nullable=False, default=0),
    Column("foto", LargeBinary),
)

izin_t = Table(
    "izin", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nama", String(100), nullable=False, index=True),
    Column("tanggal", String(10), nullable=False, index=True),  
    Column("alasan", String(50), nullable=False),
    Column("keterangan", String(255)),
    Column("status", String(20), nullable=False, default="Pending"),
)

@st.cache_resource
def get_engine():
    url = secret("database_url")
    if url:
        url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        eng = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    else:
        eng = create_engine("sqlite:///absensi_pkl_v3.db", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    return eng

def hash_pin(nama, pin):
    garam = secret("salt", "pkl-sulselbar")
    return hashlib.sha256(f"{nama.strip().lower()}|{pin}|{garam}".encode()).hexdigest()

def list_peserta(eng):
    with eng.connect() as c:
        return pd.read_sql(select(peserta_t.c.nama, peserta_t.c.gedung, peserta_t.c.divisi).order_by(peserta_t.c.nama), c)

def get_peserta(eng, nama):
    with eng.connect() as c:
        return c.execute(select(peserta_t).where(peserta_t.c.nama == nama)).mappings().first()

def add_peserta(eng, nama, gedung, divisi, pin):
    with eng.begin() as c:
        c.execute(insert(peserta_t).values(nama=nama.strip(), gedung=gedung, divisi=divisi.strip(), pin_hash=hash_pin(nama, pin)))

def reset_pin(eng, nama, pin_baru):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(pin_hash=hash_pin(nama, pin_baru)))

def set_divisi(eng, nama, divisi, gedung):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(divisi=divisi.strip(), gedung=gedung))

def hapus_peserta(eng, nama, dengan_absensi=False):
    with eng.begin() as c:
        c.execute(delete(peserta_t).where(peserta_t.c.nama == nama))
        if dengan_absensi:
            c.execute(delete(absensi_t).where(absensi_t.c.nama == nama))
            c.execute(delete(izin_t).where(izin_t.c.nama == nama))

def daftar_nama_absensi(eng):
    q = select(absensi_t.c.nama).distinct().order_by(absensi_t.c.nama)
    with eng.connect() as c:
        return [r[0] for r in c.execute(q).all()]

def hapus_absensi(eng, nama=None):
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
    q = select(
        absensi_t.c.nama, absensi_t.c.tanggal, absensi_t.c.jam, absensi_t.c.jenis,
        absensi_t.c.status, absensi_t.c.jarak, absensi_t.c.lat, absensi_t.c.lon, absensi_t.c.uang_makan,
    ).where(absensi_t.c.tanggal.like(prefix + "%")).order_by(absensi_t.c.tanggal, absensi_t.c.jam)
    with eng.connect() as c:
        return pd.read_sql(q, c)

def get_absensi_hari_foto(eng, tanggal):
    q = select(
        absensi_t.c.nama, absensi_t.c.jam, absensi_t.c.jenis, absensi_t.c.status,
        absensi_t.c.jarak, absensi_t.c.foto,
    ).where(absensi_t.c.tanggal == tanggal).order_by(absensi_t.c.jam)
    with eng.connect() as c:
        return c.execute(q).mappings().all()

def jam_absen_tercatat(eng, nama, tanggal, jenis):
    q = select(absensi_t.c.jam).where(and_(
        absensi_t.c.nama == nama, absensi_t.c.tanggal == tanggal,
        absensi_t.c.jenis == jenis, absensi_t.c.status == "berhasil"
    ))
    with eng.connect() as c:
        res = c.execute(q).scalar()
        return res[:5] if res else "--:--"

# ------------------------------------------------------------------
# LOGIKA: LOKASI & COMPUTER VISION DETEKSI WAJAH
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

@st.cache_resource
def _muat_detektor_wajah():
    detektor = [
        cv2.CascadeClassifier(cv2.data.haarcascades + nama)
        for nama in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
    ]
    if any(d.empty() for d in detektor):
        raise RuntimeError("file detektor wajah tidak ditemukan")
    return detektor

def jumlah_wajah(data: bytes):
    try:
        detektor = _muat_detektor_wajah()
    except Exception:
        return None

    gambar = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gambar is None:
        return 0
    h, w = gambar.shape
    if max(h, w) > 640:  
        skala = 640 / max(h, w)
        gambar = cv2.resize(gambar, (int(w * skala), int(h * skala)))
        h, w = gambar.shape
    gambar = cv2.equalizeHist(gambar)  
    ukuran_min = max(30, int(min(h, w) * 0.10))

    def cari(g, tetangga):
        return max(len(d.detectMultiScale(g, scaleFactor=1.1, minNeighbors=tetangga, minSize=(ukuran_min, ukuran_min))) for d in detektor)

    n = cari(gambar, 4)
    if n: return n
    for sudut in (15, -15):
        putar = cv2.getRotationMatrix2D((w / 2, h / 2), sudut, 1.0)
        miring = cv2.warpAffine(gambar, putar, (w, h), borderValue=int(gambar.mean()))
        n = cari(miring, 8)
        if n: return n
    return 0

def kompres_foto(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((640, 640))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70, optimize=True)
    return out.getvalue()

# ------------------------------------------------------------------
# LOGIKA DATA MANIPULATION (PANDAS & EXCEL EXPORT MODULAR)
# ------------------------------------------------------------------
def beri_nomor(df):
    df = df.copy()

"""
Aplikasi Absensi Peserta PKL - Bank Sulselbar (Versi Pembaruan UI & Fitur Izin)
=============================================================================
Jalanin lokal: streamlit run app.py
"""

import calendar
import hashlib
import hmac
import io
import cv2
import numpy as np
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
# KONFIGURASI - UTAMA
# ------------------------------------------------------------------
UANG_MAKAN = 15000
RADIUS_METER = 150  
BATAS_JAM_MASUK = dtime(8, 0)  
WITA = timezone(timedelta(hours=8))  

# Koordinat Gedung A dan B Bank Sulselbar
GEDUNG_COORD = {
    "A": (-5.150070542826675, 119.41568236911743),  
    "B": (-5.150163347858429, 119.41329137592547),  
}

DIVISI_GEDUNG = {
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
    "Divisi Kepatuhan": "B",
    "Divisi Dana dan Jasa": "B",
    "Divisi Syariah": "B",
    "Divisi Manajemen Risiko": "B",
    "Divisi SKAI": "B",
}

BULAN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
            "September", "Oktober", "November", "Desember"]

# ------------------------------------------------------------------
# HELPER: SECRETS
# ------------------------------------------------------------------
def secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

# ------------------------------------------------------------------
# DATABASE (SQLAlchemy Hibrida: SQLite Lokal / Postgres Cloud)
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
    Column("tanggal", String(10), nullable=False, index=True),  
    Column("jam", String(8), nullable=False),                   
    Column("jenis", String(10), nullable=False, default="masuk"),  
    Column("lat", Float),
    Column("lon", Float),
    Column("akurasi", Float),
    Column("jarak", Float),
    Column("status", String(20), nullable=False),               
    Column("uang_makan", Integer, nullable=False, default=0),
    Column("foto", LargeBinary),
)

izin_t = Table(
    "izin", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nama", String(100), nullable=False, index=True),
    Column("tanggal", String(10), nullable=False, index=True),  
    Column("alasan", String(50), nullable=False),
    Column("keterangan", String(255)),
    Column("status", String(20), nullable=False, default="Pending"),
)

@st.cache_resource
def get_engine():
    url = secret("database_url")
    if url:
        url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        eng = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    else:
        eng = create_engine("sqlite:///absensi_pkl_v3.db", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    return eng

def hash_pin(nama, pin):
    garam = secret("salt", "pkl-sulselbar")
    return hashlib.sha256(f"{nama.strip().lower()}|{pin}|{garam}".encode()).hexdigest()

def list_peserta(eng):
    with eng.connect() as c:
        return pd.read_sql(select(peserta_t.c.nama, peserta_t.c.gedung, peserta_t.c.divisi).order_by(peserta_t.c.nama), c)

def get_peserta(eng, nama):
    with eng.connect() as c:
        return c.execute(select(peserta_t).where(peserta_t.c.nama == nama)).mappings().first()

def add_peserta(eng, nama, gedung, divisi, pin):
    with eng.begin() as c:
        c.execute(insert(peserta_t).values(nama=nama.strip(), gedung=gedung, divisi=divisi.strip(), pin_hash=hash_pin(nama, pin)))

def reset_pin(eng, nama, pin_baru):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(pin_hash=hash_pin(nama, pin_baru)))

def set_divisi(eng, nama, divisi, gedung):
    with eng.begin() as c:
        c.execute(update(peserta_t).where(peserta_t.c.nama == nama).values(divisi=divisi.strip(), gedung=gedung))

def hapus_peserta(eng, nama, dengan_absensi=False):
    with eng.begin() as c:
        c.execute(delete(peserta_t).where(peserta_t.c.nama == nama))
        if dengan_absensi:
            c.execute(delete(absensi_t).where(absensi_t.c.nama == nama))
            c.execute(delete(izin_t).where(izin_t.c.nama == nama))

def daftar_nama_absensi(eng):
    q = select(absensi_t.c.nama).distinct().order_by(absensi_t.c.nama)
    with eng.connect() as c:
        return [r[0] for r in c.execute(q).all()]

def hapus_absensi(eng, nama=None):
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
    q = select(
        absensi_t.c.nama, absensi_t.c.tanggal, absensi_t.c.jam, absensi_t.c.jenis,
        absensi_t.c.status, absensi_t.c.jarak, absensi_t.c.lat, absensi_t.c.lon, absensi_t.c.uang_makan,
    ).where(absensi_t.c.tanggal.like(prefix + "%")).order_by(absensi_t.c.tanggal, absensi_t.c.jam)
    with eng.connect() as c:
        return pd.read_sql(q, c)

def get_absensi_hari_foto(eng, tanggal):
    q = select(
        absensi_t.c.nama, absensi_t.c.jam, absensi_t.c.jenis, absensi_t.c.status,
        absensi_t.c.jarak, absensi_t.c.foto,
    ).where(absensi_t.c.tanggal == tanggal).order_by(absensi_t.c.jam)
    with eng.connect() as c:
        return c.execute(q).mappings().all()

def jam_absen_tercatat(eng, nama, tanggal, jenis):
    q = select(absensi_t.c.jam).where(and_(
        absensi_t.c.nama == nama, absensi_t.c.tanggal == tanggal,
        absensi_t.c.jenis == jenis, absensi_t.c.status == "berhasil"
    ))
    with eng.connect() as c:
        res = c.execute(q).scalar()
        return res[:5] if res else "--:--"

# ------------------------------------------------------------------
# LOGIKA: LOKASI & COMPUTER VISION DETEKSI WAJAH
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

@st.cache_resource
def _muat_detektor_wajah():
    detektor = [
        cv2.CascadeClassifier(cv2.data.haarcascades + nama)
        for nama in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
    ]
    if any(d.empty() for d in detektor):
        raise RuntimeError("file detektor wajah tidak ditemukan")
    return detektor

def jumlah_wajah(data: bytes):
    try:
        detektor = _muat_detektor_wajah()
    except Exception:
        return None

    gambar = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gambar is None:
        return 0
    h, w = gambar.shape
    if max(h, w) > 640:  
        skala = 640 / max(h, w)
        gambar = cv2.resize(gambar, (int(w * skala), int(h * skala)))
        h, w = gambar.shape
    gambar = cv2.equalizeHist(gambar)  
    ukuran_min = max(30, int(min(h, w) * 0.10))

    def cari(g, tetangga):
        return max(len(d.detectMultiScale(g, scaleFactor=1.1, minNeighbors=tetangga, minSize=(ukuran_min, ukuran_min))) for d in detektor)

    n = cari(gambar, 4)
    if n: return n
    for sudut in (15, -15):
        putar = cv2.getRotationMatrix2D((w / 2, h / 2), sudut, 1.0)
        miring = cv2.warpAffine(gambar, putar, (w, h), borderValue=int(gambar.mean()))
        n = cari(miring, 8)
        if n: return n
    return 0

def kompres_foto(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((640, 640))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70, optimize=True)
    return out.getvalue()

# ------------------------------------------------------------------
# LOGIKA DATA MANIPULATION (PANDAS & EXCEL EXPORT MODULAR)
# ------------------------------------------------------------------
def beri_nomor(df):
    df = df.copy()
Gunakan kode dengan hati-hati.
df.index = range(1, len(df) + 1)
df.index.name = "No"
return df
def rupiah(n):
return f"Rp{int(n):,}".replace(",", ".")
def rekap_peserta(df):
ok = df[df["status"] == "berhasil"]
masuk = ok[ok["jenis"] == "masuk"].groupby("nama")["tanggal"].nunique()
pulang = ok[ok["jenis"] == "pulang"].groupby("nama")["tanggal"].nunique()
uang = ok.groupby("nama")["uang_makan"].sum()
out = pd.DataFrame({"Hari Hadir": masuk, "Hari Pulang": pulang, "Total Uang Makan": uang})
out = out.fillna(0).astype(int).sort_index()
out.index.name = "Nama"
return out.reset_index()
def rekap_harian(df):
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
row = {"Nama": nama, "Gedung": info["gedung"].get(nama, "-"), "Divisi": info["divisi"].get(nama, "-")}
for d in range(1, jumlah_hari + 1):
if d in masuk and d in pulang: row[str(d)] = "✓"
elif d in masuk: row[str(d)] = "M"
elif d in pulang: row[str(d)] = "P"
else: row[str(d)] = ""
row["Hadir"] = len(masuk)
row["Total Uang Makan"] = int(sub["uang_makan"].sum())
baris.append(row)
return pd.DataFrame(baris, columns=kolom)
def tabel_detail(df):
d = df.copy()
d["Peta"] = [f"google.com{la},{lo}" if pd.notna(la) and pd.notna(lo) else "" for la, lo in zip(d["lat"], d["lon"])]
d["lat"], d["lon"], d["jarak"] = d["lat"].round(6), d["lon"].round(6), d["jarak"].round(0)
d["jenis"] = d["jenis"].str.capitalize()
d["status"] = d["status"].map({"berhasil": "Berhasil", "gagal_lokasi": "Gagal (di luar area)"}).fillna(d["status"])
d = d.rename(columns={
"nama": "Nama", "tanggal": "Tanggal", "jam": "Jam", "jenis": "Jenis", "status": "Status",
"jarak": "Jarak ke Gedung (m)", "lat": "Latitude", "lon": "Longitude", "uang_makan": "Uang Makan",
})
return d[["Nama", "Tanggal", "Jam", "Jenis", "Status", "Jarak ke Gedung (m)", "Latitude", "Longitude", "Uang Makan", "Peta"]]
def excel_bytes(df, peserta_df, tahun, bulan):
"""Menghasilkan file Excel dasar multi-sheet yang aman dan cepat tanpa openpyxl custom fill."""
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as w:
matriks_kehadiran(df, peserta_df, tahun, bulan).to_excel(w, sheet_name="Rekap Kehadiran", index=False)
rekap_harian(df).to_excel(w, sheet_name="Uang Makan Harian", index=False)
rekap_peserta(df).to_excel(w, sheet_name="Rekap per Peserta", index=False)
tabel_detail(df).to_excel(w, sheet_name="Detail Absensi", index=False)
return buf.getvalue()

------------------------------------------------------------------


SECURITY & ACCESS CONTROL


------------------------------------------------------------------

def password_admin():
pw = secret("admin_password")
if pw: return str(pw)
if secret("database_url"): return None
return "admin123"
def cek_pin(eng, nama, pin):
peserta = get_peserta(eng, nama)
return peserta is not None and hmac.compare_digest(peserta["pin_hash"], hash_pin(nama, pin))
def admin_login():
if st.session_state.get("admin_ok"): return True
pw_benar = password_admin()
if pw_benar is None:
st.error("admin_password belum diatur di Secrets.")
return False
pw = st.text_input("Password admin", type="password")
if st.button("Masuk sebagai admin"):
if hmac.compare_digest(pw, str(pw_benar)):
st.session_state.admin_ok = True
st.rerun()
else: st.error("Password salah.")
return False

------------------------------------------------------------------


FITUR BARU: FORM PENGAJUAN IZIN USER


------------------------------------------------------------------

def form_izin_peserta(eng, nama):
st.subheader("📝 Pengajuan Izin PKL")
hari_ini = datetime.now(WITA).strftime("%Y-%m-%d")
with st.form("form_pengajuan_izin", clear_on_submit=True):
alasan = st.selectbox("Alasan Utama", ["Sakit (Butuh Istirahat)", "Keperluan Keluarga Mendesak", "Urusan Kampus / Sekolah"])
keterangan = st.text_area("Detail Keterangan Tambahan")
tombol_kirim = st.form_submit_button("Kirim Pengajuan Izin", type="primary")
if tombol_kirim:
with eng.begin() as c:
c.execute(insert(izin_t).values(nama=nama, tanggal=hari_ini, alasan=alasan, keterangan=keterangan, status="Pending"))
st.success("✅ Pengajuan izin Anda berhasil dikirim ke Admin.")

------------------------------------------------------------------


HALAMAN USER: PRESENSI & MENU GRID ADAPTIF (GAMBAR REFERENSI)


------------------------------------------------------------------

def halaman_absen(eng, nama):
peserta = get_peserta(eng, nama)
if peserta is None:
st.error("Data kamu tidak ditemukan. Silakan keluar lalu hubungi admin.")
return
gedung, divisi = peserta["gedung"], peserta["divisi"]
# Visual Desain Atas Sesuai Gambar Referensi 1 & 2
st.markdown(f"### Selamat Siang 👋\n## {nama}\n{divisi} • Gedung {gedung}", unsafe_allow_html=True)
hari_ini_dt = datetime.now(WITA)
hari_ini = hari_ini_dt.strftime("%Y-%m-%d")
jam_masuk_txt = jam_absen_tercatat(eng, nama, hari_ini, "masuk")
sudah_masuk = sudah_berhasil(eng, nama, hari_ini, "masuk")
sudah_pulang = sudah_berhasil(eng, nama, hari_ini, "pulang")
# Kartu Indikator Utama Berbasis Grid Box Sesuai Gambar Referensi
st.markdown("---")
c1, c2, c3 = st.columns(3)
with c1:
st.markdown(f"➡️ Masuk{jam_masuk_txt}", unsafe_allow_html=True)
with c2:
uang_makan_txt = rupiah(UANG_MAKAN) if sudah_pulang else "-"
st.markdown(f"📄 Uang Makan{uang_makan_txt}", unsafe_allow_html=True)
with c3:
st.markdown(f"🕒 Batas Masuk{BATAS_JAM_MASUK.strftime('%H:%M')}", unsafe_allow_html=True)
st.markdown("---")
st.markdown("#### 🕒 Status presensi")
flash = st.session_state.pop("flash_absen", None)
if flash:
st.info("\n".join(flash))
elif sudah_pulang:
st.success("Absen hari ini lengkap (Masuk & Pulang). Terima kasih!")
elif sudah_masuk:
st.warning("Absen masuk tercatat. Jangan lupa absen pulang saat jam kerja selesai.")
else:
st.write("Belum ada riwayat absensi untuk hari ini.")
# Menu Sub-Fitur (Mengambil Menu Izin Saja Sesuai Permintaan Gambar 2)
st.markdown("---")
st.markdown("#### Menu")
col_iz, _ = st.columns([1, 2])
with col_iz:
buka_izin = st.button("📋 Izin", use_container_width=True)
if "buka_form_izin" not in st.session_state:
st.session_state.buka_form_izin = False
if buka_izin:
st.session_state.buka_form_izin = not st.session_state.buka_form_izin
if st.session_state.buka_form_izin:
form_izin_peserta(eng, nama)
st.markdown("---")
if sudah_pulang:
return
jenis = "pulang" if sudah_masuk else "masuk"
st.subheader(f"Ambil Foto untuk Absen {jenis.capitalize()}")
lokasi = get_geolocation()
coords = (lokasi or {}).get("coords")
foto = st.camera_input("Foto selfie untuk verifikasi", key=f"cam_{nama}{hari_ini}{jenis}")
if st.button(f"Kirim Absen {jenis.capitalize()}", type="primary", use_container_width=True):
if not coords:
st.error("Gagal mendeteksi lokasi. Aktifkan GPS/izin browser Anda.")
return
if foto is None:
st.error("Foto wajah wajib diambil.")
return
data_foto = foto.getvalue()
wajah = jumlah_wajah(data_foto)
if wajah is None:
st.error("Sistem detektor wajah server sedang tidak aktif.")
return
if wajah == 0:
st.error("❌ Wajah tidak terdeteksi. Pastikan wajah tegak dan jelas.")
return
lat, lon = coords["latitude"], coords["longitude"]
cocok, jarak = cek_lokasi(lat, lon, gedung)
status = "berhasil" if cocok else "gagal_lokasi"
uang = UANG_MAKAN if (cocok and jenis == "pulang") else 0
simpan_absen(
eng, nama=nama, tanggal=hari_ini, jam=hari_ini_dt.strftime("%H:%M:%S"), jenis=jenis,
lat=lat, lon=lon, akurasi=coords.get("accuracy"), jarak=jarak,
status=status, uang_makan=uang, foto=kompres_foto(data_foto),
)
if cocok:
pesan = [f"✅ Absen {jenis} berhasil disimpan."]
if uang: pesan.append(f"💰 Uang makan berhasil dikunci: {rupiah(uang)}")
st.session_state["flash_absen"] = pesan
st.rerun()
else:
st.error(f"❌ Absen gagal: Anda berada di luar jangkauan area Gedung {gedung} (Terdeteksi selisih {jarak:.0f} meter).")

------------------------------------------------------------------


PANEL KENDALI ADMINISTRATOR (CRUD, REKAP & PERSETUJUAN IZIN)


------------------------------------------------------------------

def halaman_kelola_peserta(eng):
st.header("👥 Kelola Peserta PKL")
with st.form("tambah_peserta", clear_on_submit=True):
nama = st.text_input("Nama peserta")
divisi = st.selectbox("Divisi", list(DIVISI_GEDUNG.keys()))
gedung = DIVISI_GEDUNG[divisi]
pin = st.text_input("PIN (4-6 angka)", max_chars=6)
kirim = st.form_submit_button("Tambah Peserta")
if kirim:
if not nama.strip(): st.error("Nama wajib diisi.")
elif not (pin.isdigit() and 4 <= len(pin) <= 6): st.error("PIN harus 4-6 angka.")
else:
try:
add_peserta(eng, nama, gedung, divisi, pin)
st.success(f"{nama.strip()} ditambahkan ke Gedung {gedung}.")
except IntegrityError: st.error("Nama itu sudah terdaftar.")
df = list_peserta(eng)
st.dataframe(beri_nomor(df.rename(columns={"nama": "Nama", "gedung": "Gedung", "divisi": "Divisi"})), use_container_width=True)
if not df.empty:
with st.expander("Edit / Hapus Akun Peserta"):
target = st.selectbox("Pilih peserta", df["nama"])
opsi_divisi = list(DIVISI_GEDUNG.keys())
divisi_lama = df.loc[df["nama"] == target, "divisi"].values[0]
idx_default = opsi_divisi.index(divisi_lama) if divisi_lama in opsi_divisi else 0
divisi_baru = st.selectbox("Divisi Baru", opsi_divisi, index=idx_default)
if st.button("Perbarui Divisi"):
set_divisi(eng, target, divisi_baru, DIVISI_GEDUNG[divisi_baru])
st.success("Divisi berhasil diubah.")
st.rerun()
pin_baru = st.text_input("PIN baru", max_chars=6)
hapus_absen_juga = st.checkbox("Hapus riwayat absensi & izin juga")
c1, c2 = st.columns(2)
if c1.button("Ganti PIN"):
if pin_baru.isdigit() and 4 <= len(pin_baru) <= 6:
reset_pin(eng, target, pin_baru)
st.success("PIN diperbarui.")
else: st.error("PIN tidak valid.")
if c2.button("Hapus Akun"):
hapus_peserta(eng, target, dengan_absensi=hapus_absen_juga)
st.rerun()
def halaman_verifikasi_izin_admin(eng):
st.header("📋 Validasi Surat Izin Peserta")
with eng.connect() as c:
daftar_iz = c.execute(select(izin_t).where(izin_t.c.status == "Pending")).mappings().all()
if not daftar_iz:
st.info("Tidak ada pengajuan surat izin pending saat ini.")
return
for iz in daftar_iz:
with st.container(border=True):
st.write(f"Nama: {iz['nama']} | Tanggal: {iz['tanggal']}")
st.write(f"Kategori: {iz['alasan']}")
st.write(f"Keterangan Detail: {iz['keterangan']}")
if st.button(f"Setujui & Sahkan Izin {iz['nama']}", key=f"iz_{iz['id']}"):
with eng.begin() as c:
c.execute(update(izin_t).where(izin_t.c.id == iz['id']).values(status="Disetujui"))
st.success("Izin disahkan.")
st.rerun()
def halaman_rekap(eng):
st.header("📊 Rekap Kehadiran & Anggaran")
hari_ini = datetime.now(WITA).date()
c1, c2 = st.columns(2)
bulan = c1.selectbox("Bulan", list(range(1, 13)), index=hari_ini.month - 1, format_func=lambda m: BULAN_ID[m - 1])
tahun = c2.number_input("Tahun", value=hari_ini.year, step=1)
df = get_absensi_bulan(eng, int(tahun), int(bulan))
if df.empty:
st.info("Belum ada data rekapan absensi pada periode bulan ini.")
return
peserta_df = list_peserta(eng)
harian = rekap_harian(df)
per_peserta = rekap_peserta(df)
m1, m2, m3 = st.columns(3)
m1.metric("Dana Uang Makan", rupiah(per_peserta["Total Uang Makan"].sum()))
m2.metric("Peserta Aktif Hadir", int(df[df["status"] == "berhasil"]["nama"].nunique()))
m3.metric("Absen Ditolak", int((df["status"] != "berhasil").sum()))
t_uang, t_hadir, t_det = st.tabs(["💰 Keuangan", "🗓️ Kehadiran Matriks", "📍 Log Koordinat GPS"])
with t_uang:
st.bar_chart(harian.set_index("Tanggal")["Uang Makan Hari Ini"])
st.line_chart(harian.set_index("Tanggal")["Accumulasi Uang Makan"] if "Accumulasi Uang Makan" in harian.columns else harian.set_index("Tanggal")["Akumulasi Uang Makan"])
st.dataframe(beri_nomor(per_peserta), use_container_width=True)
with t_hadir:
st.dataframe(beri_nomor(matriks_kehadiran(df, peserta_df, int(tahun), int(bulan))), use_container_width=True)
with t_det:
st.dataframe(beri_nomor(tabel_detail(df)), use_container_width=True, column_config={"Peta": st.column_config.LinkColumn("Peta", display_text="Buka peta")})
st.download_button("⬇️ Unduh Excel (.xlsx)", data=excel_bytes(df, peserta_df, int(tahun), int(bulan)), file_name=f"rekap_absensi_{tahun}_{bulan:02d}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
def halaman_foto(eng):
st.header("🖼️ Galeri Bukti Foto Wajah")
tgl = st.date_input("Pilih Tanggal Peninjauan", value=datetime.now(WITA).date())
rows = get_absensi_hari_foto(eng, tgl.strftime("%Y-%m-%d"))
if not rows:
st.info("Tidak ditemukan log foto absensi pada tanggal tersebut.")
return
kolom = st.columns(3)
for i, r in enumerate(rows):
with kolom[i % 3]:
if r["foto"]: st.image(bytes(r["foto"]), use_container_width=True)
st.caption(f"{'✅' if r['status'] == 'berhasil' else '❌'} {r['nama']}\n\n{r['jenis'].upper()} • {r['jam']} • {r['jarak']:.0f}m")

------------------------------------------------------------------


MASTER CONTROLLER & MAIN ENTRY POINT


------------------------------------------------------------------

def keluar():
for k in ("peran", "nama_login", "admin_ok", "buka_form_izin"):
st.session_state.pop(k, None)
st.rerun()
def halaman_login(eng):
st.title("📍 Absensi PKL")
st.caption("Bank Sulselbar • Sistem Verifikasi Terintegrasi")
peran = st.radio("Pilih Hak Akses Login", ["Peserta PKL", "Administrator"], horizontal=True)
if peran == "Peserta PKL":
df = list_peserta(eng)
if df.empty:
st.warning("Database kosong. Daftarkan akun peserta lewat login Administrator default terlebih dahulu.")
return
with st.form("login_peserta"):
nama = st.selectbox("Pilih Nama Anda", df["nama"])
pin = st.text_input("Masukkan PIN Keamanan", type="password", max_chars=6)
if st.form_submit_button("Masuk Ke Beranda", type="primary", use_container_width=True):
if cek_pin(eng, nama, pin):
st.session_state.peran = "user"
st.session_state.nama_login = nama
st.rerun()
else: st.error("PIN Keamanan salah.")
else:
pw_benar = password_admin()
with st.form("login_admin"):
pw = st.text_input("Password Admin Server", type="password")
if st.form_submit_button("Masuk Ke Panel Kendali", type="primary", use_container_width=True):
if hmac.compare_digest(pw, pw_benar):
st.session_state.peran = "admin"
st.session_state.admin_ok = True
st.rerun()
else: st.error("Password Admin tidak cocok.")
def halaman_admin(eng):
kiri, kanan = st.columns([4, 1])
kiri.caption("🛡️ Sesi Kontrol Administrator")
if kanan.button("Keluar", use_container_width=True): keluar()
menu = st.tabs(["👥 Manajemen User", "📋 Validasi Izin", "📊 Laporan Rekap", "🖼️ Kontrol Foto"])
with menu[0]: halaman_kelola_peserta(eng)
with menu[1]: halaman_verifikasi_izin_admin(eng)
with menu[2]: halaman_rekap(eng)
with menu[3]: halaman_foto(eng)
def main():
st.set_page_config(page_title="Absensi PKL - Bank Sulselbar", page_icon="📍", layout="centered")
eng = get_engine()
peran = st.session_state.get("peran")
nama = st.session_state.get("nama_login")
if peran == "user" and nama:
kiri, kanan = st.columns([4, 1])
kiri.caption(f"Masuk sebagai {nama}")
if kanan.button("Keluar", key="logout_u", use_container_width=True): keluar()
halaman_absen(eng, nama)
elif peran == "admin" and st.session_state.get("admin_ok"):
halaman_admin(eng)
else:
halaman_login(eng)
if name == "main":
main()
