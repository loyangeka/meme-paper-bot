# Meme Paper Trader (Solana) — PAPER ONLY

Simulasi scalping meme coin keluar-masuk-cepet di Solana dengan modal virtual
**Rp100.000**, jalan 1 minggu (mulai 20 Nov 2026) untuk mengukur win-rate dan
PnL bersih setelah biaya — sebelum Eka memutuskan apa pun soal uang asli.

**Pivot 20 Nov 2026:** awalnya BSC, tapi discovery BSC terlalu tipis
(token-profiles/boosts cuma membawa 1–2 token BSC per siklus dan pair-nya
hampir selalu di luar jendela umur 90 menit — 0 watch pair selama siklus
awal). Eka memilih pindah ke pair Solana ("trading di pair solana aja").
State di-reset bersih untuk run Solana (tidak ada trade BSC yang pernah
tutup, jadi tidak ada data yang hilang). Siklus Solana pertama: 47 token
baru, 25 watch pair — arus kandidat jauh lebih kaya.

**Tidak ada wallet, tidak ada private key, tidak ada signing, tidak ada
transaksi on-chain di kode ini.** Daemon hanya membaca data publik DexScreener
dan mensimulasikan fill. Mode paper di-hard-code.

## File

| File | Isi |
|---|---|
| `trader.py` | Daemon (python3 stdlib + curl subprocess, pola sama dengan fcfs-poller) |
| `params.json` | SEMUA parameter strategi — tuning = edit file ini |
| `state.json` | Modal virtual, posisi terbuka, watch list, statistik agregat |
| `trades.jsonl` | Satu baris per trade selesai (harga, durasi, alasan keluar, PnL) |
| `trader.log` | Log poll + keputusan filter (auto-truncate di 500 KB) |

## Strategi "new-pair momentum" (default di params.json)

Discovery: DexScreener `token-profiles/latest` + `token-boosts/latest` tiap
±60 detik → token baru di chain sesuai `params.json` (`chain: "solana"`) →
pair-nya masuk watch list selama di jendela umur.

Entry — SEMUA harus lolos:
- chainId sesuai params (`solana`); umur pair 2–90 menit
- likuiditas ≥ $15.000
- transaksi m5: buys > sells
- priceChange m5 ≥ +3%; priceChange h1 ≤ +150% (hindari pucuk habis)
- volume m5 ≥ $2.000

Satu posisi sekali jalan, ukuran = seluruh modal (compounding), cooldown
5 menit setelah exit.

Exit:
- Take-profit **+35%** · Stop-loss **−12%**
- Trailing: setelah naik +20%, keluar bila harga turun 10% dari puncak
- Time-stop **20 menit**

## Model gesekan (jujur, diterapkan di setiap fill)

- Fee DEX 0,25% per sisi
- Slippage per sisi: 1,5% bila likuiditas ≥ $50k, selain itu 3% — selalu
  melawan kita
- Gas Rp1.000 per transaksi (Solana base + priority fee) × 2 per round
  trip, dipotong dalam IDR
- Kurs tetap Rp18.300/USD (parameter `usd_idr_rate`)

## Statistik & loop tuning ("kalau kalah, perbaiki mesinnya")

`state.json` → `stats` memecah hasil:
- `by_exit_reason`: trades / wins / losses / PnL untuk `take_profit`,
  `stop_loss`, `trailing`, `time_stop` — terlihat bagian mana yang bocor
- `by_day`: rekap harian

Loop tuning (tanpa nunggu persetujuan Eka, khusus paper):
1. Reviewer membaca `stats` + `trades.jsonl` berkala (mis. harian).
2. Bila satu alasan keluar dominan merugi (mis. time-stop terus negatif →
   momentum mati terlalu cepat), sesuaikan parameter terkait di
   `params.json` (filter entry, TP/SL, trailing, time-stop, tier slippage).
3. Daemon membaca ulang `params.json` setiap siklus discovery — **tidak
   perlu restart**, perubahan langsung berlaku di trade berikutnya.
4. Catat setiap perubahan parameter + alasannya di log harian supaya hasil
   sebelum/sesudah bisa dibandingkan.

## Notifikasi Telegram

Lewat mekanisme yang sama dengan fcfs-poller (`aa.notify` dari venv
airdrop-agent ke bot laporan Eka): 1 pesan saat startup + 1 pesan per trade
selesai (token, alasan keluar, PnL bersih IDR, modal berjalan).

## Service

```bash
systemctl status meme-paper
journalctl -u meme-paper -f
```

Unit di `/etc/systemd/system/meme-paper.service` — **unit di /etc TERHAPUS
setiap VM replacement**; buat ulang persis seperti ini lalu
`systemctl daemon-reload && systemctl enable --now meme-paper`:

```ini
[Unit]
Description=Meme coin paper-trading scalper (PAPER ONLY - no wallet, no real trades)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/root/.proxy.env
WorkingDirectory=/home/hatch/workspace/meme-paper
ExecStart=/usr/bin/python3 /home/hatch/workspace/meme-paper/trader.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Prasyarat: `/root/.proxy.env` ada (mode 600) — file ini juga terhapus saat VM
replacement; buat ulang dari env runtime (8 baris variabel proxy) sebelum
start service apa pun.

## Keterbatasan (jujur)

- Harga DexScreener bukan feed real-time bursa — latensi detik s/d menit,
  dan priceChange/txns adalah agregat DexScreener.
- Fill paper adalah estimasi dari harga polled ± slippage model; eksekusi
  asli bisa lebih buruk (antrean, MEV, likuiditas berubah).
- Filter ketat = sedikit trade; filter longgar = lebih banyak rug. Angka
  seminggu pertama adalah bahan tuning, bukan bukti strategi final.
