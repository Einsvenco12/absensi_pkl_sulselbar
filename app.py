"""
Aplikasi Absensi Peserta PKL - Bank Sulselbar (versi siap deploy)
=================================================================
Fitur:
- Absen MASUK saja (tidak ada absen pulang): foto selfie + lokasi GPS
- Login PIN per peserta (dibuat admin)
- Cek lokasi: harus di sekitar gedung yang terdaftar untuk peserta (A / B)
- Setiap peserta juga punya Divisi (dipilih dari daftar, Gedung ikut otomatis)
- Batas waktu: absen masuk hanya bisa sebelum jam BATAS_JAM_MASUK
- Uang makan Rp15.000 otomatis saat absen masuk berhasil
- Blokir absen dobel di hari yang sama
- Admin (pakai password): kelola peserta, rekap + grafik + export Excel, bukti foto
- Database: SQLite (lokal) atau Postgres/Supabase (cloud) - otomatis

Jalanin lokal:   streamlit run app.py
"""

import hashlib
import hmac
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

BATAS_JAM_MASUK = dtime(12, 0)  # absen masuk harus sebelum jam ini (WITA), lewat = ditolak

WITA = timezone(timedelta(hours=8))  # server cloud pakai UTC, jadi jam dipaksa WITA


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


def hapus_peserta(eng, nama):
    with eng.begin() as c:
        c.execute(delete(peserta_t).where(peserta_t.c.nama == nama))


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
            absensi_t.c.status, absensi_t.c.jarak, absensi_t.c.uang_makan,
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
def admin_login():
    if st.session_state.get("admin_ok"):
        return True

    pw_benar = secret("admin_password")
    if not pw_benar:
        if secret("database_url"):
            st.error("admin_password belum diatur di Secrets. Hubungi pengelola aplikasi.")
            return False
        pw_benar = "admin123"
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
def halaman_absen(eng):
    st.header("📍 Absen PKL")

    df = list_peserta(eng)
    if df.empty:
        st.warning("Belum ada peserta terdaftar. Hubungi admin untuk didaftarkan.")
        return

    nama = st.selectbox("Nama", df["nama"])
    row = df.loc[df["nama"] == nama].iloc[0]
    gedung, divisi = row["gedung"], row["divisi"]
    st.text_input(
        "Gedung & Divisi (terdaftar, tidak bisa diubah)",
        value=f"Gedung {gedung} • {divisi}",
        disabled=True,
    )

    pin = st.text_input("PIN", type="password", max_chars=6)

    lokasi = get_geolocation()
    coords = (lokasi or {}).get("coords")
    if coords:
        st.success(f"📡 Lokasi terdeteksi (akurasi ±{coords.get('accuracy', 0):.0f} m)")
    else:
        st.info("Menunggu lokasi... klik **Allow/Izinkan** saat browser meminta akses lokasi.")

    foto = st.camera_input("Foto selfie untuk absen")

    if st.button("Absen Sekarang", type="primary", width="stretch"):
        if not coords:
            st.error("Lokasi belum terdeteksi. Aktifkan GPS/izin lokasi lalu coba lagi.")
            return
        if foto is None:
            st.error("Foto belum diambil.")
            return

        peserta = get_peserta(eng, nama)
        if peserta is None or not hmac.compare_digest(peserta["pin_hash"], hash_pin(nama, pin)):
            st.error("PIN salah.")
            return

        now = datetime.now(WITA)
        tanggal, jam = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")

        if sudah_berhasil(eng, nama, tanggal, "masuk"):
            st.warning("Kamu sudah absen masuk hari ini.")
            return

        if now.time() > BATAS_JAM_MASUK:
            st.error(
                f"❌ Absen ditolak: sudah lewat jam {BATAS_JAM_MASUK.strftime('%H:%M')} WITA "
                f"(sekarang {now.strftime('%H:%M')})."
            )
            return

        lat, lon = coords["latitude"], coords["longitude"]
        cocok, jarak = cek_lokasi(lat, lon, gedung)
        status = "berhasil" if cocok else "gagal_lokasi"
        uang = UANG_MAKAN if cocok else 0

        simpan_absen(
            eng, nama=nama, tanggal=tanggal, jam=jam, jenis="masuk",
            lat=lat, lon=lon, akurasi=coords.get("accuracy"), jarak=jarak,
            status=status, uang_makan=uang, foto=kompres_foto(foto.getvalue()),
        )

        if cocok:
            st.success(f"✅ Absen masuk berhasil pukul {jam} WITA.")
            st.info(f"💰 Uang makan hari ini: Rp{uang:,}".replace(",", "."))
        else:
            st.error(
                f"❌ Absen GAGAL: kamu ~{jarak:.0f} m dari Gedung {gedung}, "
                "di luar area yang diizinkan. Pastikan kamu sudah di gedung lalu coba lagi."
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

    df = list_peserta(eng)
    st.dataframe(df.rename(columns={"nama": "Nama", "gedung": "Gedung", "divisi": "Divisi"}), width="stretch")

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
            c1, c2 = st.columns(2)
            if c1.button("Reset PIN"):
                if pin_baru.isdigit() and 4 <= len(pin_baru) <= 6:
                    reset_pin(eng, target, pin_baru)
                    st.success("PIN diperbarui.")
                else:
                    st.error("PIN harus 4-6 angka.")
            if c2.button("Hapus peserta"):
                hapus_peserta(eng, target)
                st.success(f"{target} dihapus (riwayat absensinya tetap tersimpan).")
                st.rerun()


# ------------------------------------------------------------------
# HALAMAN ADMIN: REKAP, GRAFIK, EXPORT
# ------------------------------------------------------------------
def halaman_rekap(eng):
    st.header("📊 Rekap Absensi Masuk & Uang Makan")
    if not admin_login():
        return

    hari_ini = datetime.now(WITA).date()
    c1, c2 = st.columns(2)
    bulan = c1.selectbox("Bulan", list(range(1, 13)), index=hari_ini.month - 1)
    tahun = c2.number_input("Tahun", value=hari_ini.year, step=1)

    df = get_absensi_bulan(eng, int(tahun), int(bulan))
    if df.empty:
        st.info("Belum ada data absensi di bulan ini.")
        return

    berhasil = df[df["status"] == "berhasil"]
    rekap = (
        berhasil
        .groupby("nama")
        .agg(hari_hadir=("tanggal", "nunique"), total_uang_makan=("uang_makan", "sum"))
        .reset_index()
    )
    rekap.columns = ["Nama", "Hari Hadir", "Total Uang Makan"]

    m1, m2 = st.columns(2)
    m1.metric("Total uang makan bulan ini", f"Rp{int(rekap['Total Uang Makan'].sum()):,}".replace(",", "."))
    m2.metric("Percobaan absen gagal lokasi/telat", int((df["status"] != "berhasil").sum()))

    st.subheader("Total Uang Makan per Peserta")
    st.dataframe(rekap, width="stretch")

    st.subheader("Grafik")
    if not rekap.empty:
        st.caption("Hari hadir per peserta")
        st.bar_chart(rekap.set_index("Nama")["Hari Hadir"])
    harian = berhasil.groupby("tanggal").size()
    if not harian.empty:
        st.caption("Jumlah peserta yang hadir per hari")
        st.bar_chart(harian)

    st.subheader("Detail Absensi")
    detail = df.rename(columns={
        "nama": "Nama", "tanggal": "Tanggal", "jam": "Jam", "jenis": "Jenis",
        "status": "Status", "jarak": "Jarak (m)", "uang_makan": "Uang Makan",
    })
    detail["Jarak (m)"] = detail["Jarak (m)"].round(0)
    st.dataframe(detail, width="stretch")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        rekap.to_excel(w, sheet_name="Rekap Uang Makan", index=False)
        detail.to_excel(w, sheet_name="Detail Absensi", index=False)
    st.download_button(
        "⬇️ Download Excel", data=buf.getvalue(),
        file_name=f"rekap_absensi_{int(tahun)}_{int(bulan):02d}.xlsx",
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
def main():
    st.set_page_config(page_title="Absensi PKL - Bank Sulselbar", page_icon="📍")
    eng = get_engine()

    st.sidebar.title("Absensi PKL")
    menu = st.sidebar.radio("Menu", ["Absen", "Kelola Peserta (admin)", "Rekap & Export (admin)", "Bukti Foto (admin)"])

    if menu == "Absen":
        halaman_absen(eng)
    elif menu.startswith("Kelola"):
        halaman_kelola_peserta(eng)
    elif menu.startswith("Rekap"):
        halaman_rekap(eng)
    else:
        halaman_foto(eng)

    if st.session_state.get("admin_ok"):
        if st.sidebar.button("Keluar admin"):
            st.session_state.admin_ok = False
            st.rerun()


if __name__ == "__main__":
    main()
