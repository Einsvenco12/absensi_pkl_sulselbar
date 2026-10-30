

def kelola_peserta_lengkap(eng):
    st.title("👥 Kelola Peserta PKL")
    if not admin_login():
        return
    df = list_peserta(eng)
    with st.expander("➕ Tambah Peserta", expanded=df.empty):
        with st.form("form_tambah_lengkap", clear_on_submit=True):
            nama = st.text_input("Nama lengkap")
            nim = st.text_input("NIM")
            kampus = st.text_input("Kampus / Sekolah")
            jurusan = st.text_input("Jurusan")
            semester = st.text_input("Kelas / Semester")
            norek = st.text_input("Nomor rekening")
            divisi = st.selectbox("Divisi", list(DIVISI_GEDUNG))
            pin = st.text_input("PIN (4-6 angka)", type="password", max_chars=6)
            foto = st.file_uploader("Foto profil (opsional)", type=["jpg","jpeg","png"])
            submit = st.form_submit_button("Simpan Peserta")
        if submit:
            if not nama.strip() or not (pin.isdigit() and 4 <= len(pin) <= 6):
                st.error("Nama wajib diisi dan PIN harus 4-6 angka.")
            else:
                try:
                    with eng.begin() as c:
                        c.execute(insert(peserta_t).values(nama=nama.strip(), nim=nim, kampus=kampus, jurusan=jurusan, semester=semester, norek=norek, divisi=divisi, gedung=DIVISI_GEDUNG[divisi], pin_hash=hash_pin(nama,pin), status_magang="aktif", foto_profil=kompres_foto(foto.getvalue()) if foto else None))
                    st.success("Peserta ditambahkan.")
                    st.rerun()
                except IntegrityError:
                    st.error("Nama sudah digunakan.")
    if df.empty:
        return
    cari = st.text_input("🔎 Cari peserta", key="cari_kelola").lower()
    pilihan = df[df["nama"].str.lower().str.contains(cari, regex=False)]
    if pilihan.empty:
        st.info("Tidak ada peserta yang cocok.")
        return
    nama = st.selectbox("Pilih peserta untuk edit", pilihan["nama"], key="edit_peserta")
    p = get_peserta(eng, nama)
    if p["foto_profil"]:
        st.image(bytes(p["foto_profil"]), width=120)
    with st.form("edit_lengkap"):
        nama_baru = st.text_input("Nama", value=p["nama"])
        nim = st.text_input("NIM", value=p["nim"] or "")
        kampus = st.text_input("Kampus/Sekolah", value=p["kampus"] or "")
        jurusan = st.text_input("Jurusan", value=p["jurusan"] or "")
        semester = st.text_input("Kelas/Semester", value=p["semester"] or "")
        norek = st.text_input("No. Rekening", value=p["norek"] or "")
        divisi = st.selectbox("Divisi", list(DIVISI_GEDUNG), index=list(DIVISI_GEDUNG).index(p["divisi"]) if p["divisi"] in DIVISI_GEDUNG else 0)
        status = st.selectbox("Status Magang", ["aktif","selesai"], index=1 if p["status_magang"] == "selesai" else 0)
        pin = st.text_input("PIN baru (kosongkan jika tidak diubah)", type="password", max_chars=6)
        foto = st.file_uploader("Ganti foto profil (opsional)", type=["jpg","jpeg","png"], key="foto_edit")
        simpan = st.form_submit_button("💾 Simpan Perubahan")
    if simpan:
        if not nama_baru.strip() or (pin and not(pin.isdigit() and 4 <= len(pin) <= 6)):
            st.error("Nama tidak boleh kosong. PIN baru harus 4-6 angka.")
        else:
            nilai = dict(nama=nama_baru.strip(), nim=nim, kampus=kampus, jurusan=jurusan, semester=semester, norek=norek, divisi=divisi, gedung=DIVISI_GEDUNG[divisi], status_magang=status)
            if pin:
                nilai["pin_hash"] = hash_pin(nama_baru,pin)
            elif nama_baru.strip() != nama:
                st.error("Saat mengganti nama, isi PIN baru agar login tetap berfungsi.")
                st.stop()
            if foto:
                nilai["foto_profil"] = kompres_foto(foto.getvalue())
            try:
                with eng.begin() as c:
                    c.execute(update(peserta_t).where(peserta_t.c.id == p["id"]).values(**nilai))
                    if nama_baru.strip() != nama:
                        c.execute(update(absensi_t).where(absensi_t.c.nama == nama).values(nama=nama_baru.strip()))
                st.success("Profil diperbarui.")
                st.rerun()
            except IntegrityError:
                st.error("Nama sudah digunakan peserta lain.")
    with st.expander("🗑️ Hapus peserta"):
        konfirmasi = st.checkbox(f"Saya yakin ingin menghapus {nama}")
        if st.button("Hapus peserta", disabled=not konfirmasi):
            hapus_peserta(eng,nama)
            st.success("Peserta dihapus. Riwayat absensi tetap tersimpan.")
            st.rerun()


def main():
    st.set_page_config(page_title="Absensi PKL - Bank Sulselbar", page_icon="🏦", layout="wide")
    eng = get_engine()
    st.sidebar.title("🏦 Absensi PKL")
    menu = st.sidebar.radio("Menu", ["Absen Peserta", "Dashboard Admin", "Kelola Peserta", "Rekap & Grafik Lama", "Bukti Foto"])
    if menu == "Absen Peserta":
        halaman_absen(eng)
    elif menu == "Dashboard Admin":
        dashboard_admin(eng)
    elif menu == "Kelola Peserta":
        kelola_peserta_lengkap(eng)
    elif menu == "Rekap & Grafik Lama":
        halaman_rekap(eng)
    else:
        halaman_foto(eng)
    if st.session_state.get("admin_ok") and st.sidebar.button("Keluar admin"):
        st.session_state.admin_ok = False
        st.rerun()


if __name__ == "__main__":
    main()
