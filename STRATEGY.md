# Meme Paper — Strategi & Aturan Evaluasi Mandiri

Bot paper-trading meme coin Solana (PAPER ONLY — tidak ada wallet, tidak ada
transaksi asli). File ini adalah catatan resmi gaya strategi yang berjalan,
alasan angkanya, dan aturan kapan bot dianggap gagal dan harus ganti gaya.

Mandat Eka (WhatsApp, 2026-11-22 02:55 WIB): *"Klo lost terus, bikin ubah
aturan sendiri aja... klo salah ubah gaya strateginya."* → Bot boleh (dan
wajib) mengubah aturan/gaya strateginya sendiri bila evaluasi membuktikan
gaya yang berjalan kalah. Setiap iterasi dilaporkan ke Eka.

---

## Strategi aktif: `pullback-recovery-v1` (mulai 2026-11-22 02:58 WIB)

**Modal awal strategi:** Rp21.346 (sisa modal paper setelah strategi lama).
**Baseline trade:** 12 trade tertutup pertama di trades.jsonl adalah milik
strategi lama dan TIDAK dihitung dalam evaluasi strategi ini. Setiap baris
trades.jsonl sejak strategi ini membawa field `"strategy"`.

### Ide dasar
Berhenti mengejar pump token baru. Beli token yang sudah **bertahan**
(survivor) saat harganya **pullback dari puncak dan mulai pulih** — beli di
pemulihan awal, bukan di pucuk.

### Universe (semua dari params.json)
- Pair umur **60–720 menit** (1–12 jam) — sudah melewati fase dump pembuka.
- Likuiditas **≥ $100.000**; volume h1 **≥ $25.000**, volume m5 **≥ $3.000**.
- Bot menemukan token saat baru lahir (DexScreener profiles/boosts) lalu
  **terus melacaknya** saat menua (maks 60 pair, prioritas likuiditas
  terdalam), sambil mencatat harga puncak & lembah pullback yang ia amati
  sendiri (`state.pair_track`).

### Entry (semua gerbang harus lolos)
1. Harga turun **−10% s.d. −30%** dari puncak yang teramati (di luar itu:
   belum pullback, atau terlalu dalam = potensi death spiral).
2. Pemulihan terkonfirmasi: harga sudah memantul **≥ +3%** dari lembah
   pullback DAN priceChange m5 **≥ +1%**.
3. Bukan freefall: priceChange h1 **≥ −60%**.
4. buys m5 > sells m5; buys h1 ≥ sells h1.
5. **Proteksi LP Eka (tidak berubah):** konfirmasi LP ~90 detik sebelum
   entry — batal bila likuiditas turun > 3% selama jendela itu.

### Exit
- Take profit **+18%** · Stop loss **−8%**
- Trailing: aktif di **+10%**, kunci bila turun **5%** dari puncak posisi
- Time stop **40 menit**
- **LP-pull (tidak berubah):** keluar bila likuiditas turun ≥ 25% dari entry
- Ukuran posisi **25%** modal, satu posisi dalam satu waktu, cooldown 5 mnt.

---

## Kenapa gaya lama dibuang — post-mortem `new-pair-momentum` (20–22 Nov)

12 trade tertutup: **3 menang / 9 kalah**, modal Rp100.000 → Rp21.346
(**−78,7%**). Total PnL bersih **−Rp78.654**.

- Semua kemenangan exit `time_stop` dengan gerak harga hanya +13,6% /
  +16,2% / +21,6% — **TP +35% tidak pernah tercapai**. Menang dipotong
  waktu, kalah dibiarkan sampai SL penuh.
- Semua kekalahan exit `stop_loss` −12% (gerak harga −13% s.d. −17%
  setelah slippage); **5 dari 9 kalah kena SL dalam ≤90 detik** setelah
  entry (DCTY 82 dtk, XV 55 dtk, Air 55 dtk, UNI 83 dtk, HOUSE 87 dtk) —
  bukti entry persis di pucuk lokal.
- Rata-rata menang **+Rp723** vs rata-rata kalah **−Rp8.980** (rasio
  1 : 12,4) → ekspektasi **−Rp6.554 per trade**. Ini cacat struktural
  (beli setelah naik + TP tak terjangkau + SL lebar), bukan cacat tuning —
  dua kali tuning parameter (likuiditas 15k→30k→50k, ukuran posisi
  50%→25%) tidak mengubah pola kekalahan.

### Alasan angka strategi baru
- **TP +18%**: duduk di dalam rentang gerak kemenangan lama yang terbukti
  terjadi (+13,6% s.d. +21,6%); TP lama +35% di luar jangkauan.
- **SL −8%**: kekalahan lama berjalan ke −13% s.d. −17%; SL lebih ketat
  memangkas ekor kerugian ±1/3.
- **Trailing arm +10% / drop 5%**: ketiga kemenangan lama semuanya sempat
  menembus +13% — trailing yang aktif lebih awal mengunci sebagian
  pemulihan sebelum memudar, sesuatu yang tidak pernah dilakukan gaya lama.
- **Time stop 40 mnt**: pemulihan pullback butuh waktu lebih panjang
  daripada scalp momentum 15–20 mnt; proteksi LP-pull tetap menjaga
  dari rug selama menunggu.
- **Pullback −10%..−30% + bounce ≥3%**: membeli diskon dari puncak pada
  token yang likuiditas & volumenya sudah terbukti, dengan bukti
  pembalikan — kebalikan persis dari cacat entry gaya lama.

---

## ⚠️ Catatan friksi (temuan struktural, di luar strategi)

Model friksi bot: fee 0,25%/sisi + slippage 1,5%/sisi (likuiditas ≥ $50k)
+ gas Rp1.000/transaksi × 2. Pada modal tersisa Rp21.346 dan posisi 25%
(Rp5.336), **gas saja = Rp2.000 = 37,5% dari ukuran posisi** — harga harus
naik ≈ **+42%** hanya untuk balik modal pada satu trade. Artinya selama
modal paper sekecil ini, hasil bersih didominasi friksi, bukan strategi.
Ini tidak diubah sepihak (mengutak-atik model friksi = memalsukan hasil);
keputusan soal basis modal/ukuran posisi ada di Eka pada verdict 27 Nov.
Strategi tetap dievaluasi apa adanya dengan aturan di bawah.

---

## Aturan evaluasi mandiri (berlaku sejak strategi ini)

- Evaluasi berjalan **setiap blok 10 trade tertutup** di bawah strategi
  yang aktif (dihitung otomatis di `trader.py` → `state.strategy`,
  hasil di `state.strategy.last_eval`, FAIL juga dikirim ke Telegram).
- Strategi dinyatakan **GAGAL** bila salah satu terpenuhi pada evaluasi:
  - **Win rate blok < 40%**, ATAU
  - **Drawdown dari puncak modal (selama strategi berjalan) > 25%**.
- Bila GAGAL: gaya strategi **wajib diganti** (bukan sekadar tuning
  angka) pada iterasi berikutnya, dikerjakan otomatis dan dilaporkan ke
  Eka — sesuai mandat 22 Nov 2026 di atas.
- Bila PASS: strategi lanjut ke blok 10 trade berikutnya.
- Apa pun hasilnya, ini tetap **eksperimen paper**. Keputusan uang asli
  tetap verdict Eka tanggal **27 Nov 2026** — bot tidak pernah punya
  jalur real-money.

## Riwayat iterasi
| Tanggal | Strategi | Hasil |
|---|---|---|
| 20 Nov 2026 | `new-pair-momentum` (TP +35 / SL −12 / TS 15–20 mnt) | 12 trade: 3W/9L, −78,7% → **DIBUANG** |
| 22 Nov 2026 | `pullback-recovery-v1` (dokumen ini) | berjalan |
