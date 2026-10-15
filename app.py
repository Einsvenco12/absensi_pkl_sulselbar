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
    st.set_page_config(page_title="Presensi PKL - Bank Sulselbar", page_icon="📍", layout="wide")
    tambah_css()
    eng = get_engine()

    # Login peserta tampil pertama kali. Dashboard baru dibuka setelah Nama + NIM valid.
    if not st.session_state.get("peserta_ok"):
        halaman_login(eng)
        return

    dashboard_peserta(eng)

    # Fitur admin tetap memakai fitur yang memang tersedia pada aplikasi.
    st.sidebar.divider()
    st.sidebar.subheader("Administrasi")
    menu_admin = st.sidebar.selectbox("Fitur admin", ["Dashboard peserta", "Kelola Peserta", "Rekap & Export", "Bukti Foto"])
    if menu_admin == "Kelola Peserta":
        halaman_kelola_peserta(eng)
    elif menu_admin == "Rekap & Export":
        halaman_rekap(eng)
    elif menu_admin == "Bukti Foto":
        halaman_foto(eng)

    if st.session_state.get("admin_ok") and st.sidebar.button("Keluar admin"):
        st.session_state.admin_ok = False
        st.rerun()


if __name__ == "__main__":
    main()
