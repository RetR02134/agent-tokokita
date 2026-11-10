# Deploy TokoKita ke server sendiri

## 0. Sebelum mulai
- Siapkan nilai untuk `.env` (lihat `.env.example`). **`MCP_SERVER_URL` dan `SECRET_KEY` wajib diisi**; tidak ada default.
- Putar (rotate) API key Azure dan subscription key APIM jika pernah terkirim lewat chat, email, atau ZIP.
- Pakai HTTPS (cookie sesi `Secure`). Contoh di bawah memakai nginx + Let's Encrypt.
- Jalankan **satu worker** (`WEB_CONCURRENCY=1`). State sesi dan rate limit ada di memori proses; kapasitas dinaikkan lewat `THREADS`.

## 1. Tes lokal
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env     # isi nilainya; untuk http lokal: APP_ENV=dev, COOKIE_SECURE=0, TRUSTED_PROXIES=0
pytest -q                                  # tes otomatis (tanpa koneksi ke Azure)
python app.py                              # http://127.0.0.1:8000
```

## 2A. Docker
```bash
docker build -t tokokita .
docker run -d --name tokokita --restart unless-stopped \
  --env-file /etc/tokokita/env -p 127.0.0.1:8000:8000 tokokita
```
`/etc/tokokita/env` berisi isi `.env` (`chmod 600`, pemilik root). Port hanya dibuka ke localhost; nginx di depannya.

## 2B. systemd (tanpa Docker)
```bash
sudo useradd -r -m -d /opt/tokokita tokokita
sudo cp -r app.py instructions.md startup.sh static requirements.txt /opt/tokokita/
sudo -u tokokita python3 -m venv /opt/tokokita/.venv
sudo -u tokokita /opt/tokokita/.venv/bin/pip install -r /opt/tokokita/requirements.txt
sudo install -m 600 -o root -g root /path/ke/.env /etc/tokokita.env
```
`/etc/systemd/system/tokokita.service`:
```ini
[Unit]
Description=TokoKita chat
After=network.target

[Service]
User=tokokita
WorkingDirectory=/opt/tokokita
EnvironmentFile=/etc/tokokita.env
Environment=PATH=/opt/tokokita/.venv/bin:/usr/bin HOST=127.0.0.1
ExecStart=/opt/tokokita/startup.sh
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tokokita
```

## 3. nginx (streaming SSE wajib `proxy_buffering off`)
```nginx
server {
    listen 443 ssl http2;
    server_name chat.contoh.com;
    # ssl_certificate ... (certbot --nginx)

    client_max_body_size 32k;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;   # timpa, jangan tambah: mencegah spoof IP
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
server { listen 80; server_name chat.contoh.com; return 301 https://$host$request_uri; }
```
Bila ada CDN/proxy lain di depan nginx (mis. Cloudflare), atur `TRUSTED_PROXIES` sesuai jumlah hop dan pastikan nginx memakai real-IP dari CDN.

## 4. Cek setelah deploy
```bash
curl -s https://chat.contoh.com/healthz                       # {"build":...,"status":"ok"}
curl -s -H "X-Health-Token: $HEALTH_TOKEN" "https://chat.contoh.com/healthz?deep=1"   # uji POS + model
```
Lalu coba skenario di chat: cari produk, bandingkan + grafik, buat pesanan (alamat tidak lengkap harus ditolak), cek status pesanan, dan ganti identitas di tengah percakapan.

## Catatan operasional
- **Retensi data:** percakapan memakai `previous_response_id`, jadi Azure OpenAI menyimpan respons (termasuk nama/alamat/HP yang diketik pelanggan) sesuai kebijakan retensi Azure Anda. Informasikan di kebijakan privasi.
- **Log** tidak mencatat isi balasan POS maupun pesan pelanggan; hanya nama action, kode status, dan jenis error.
- **Batas biaya:** `RATE_LIMIT_PER_MIN` (per IP), `DAILY_MESSAGE_LIMIT` (global/hari), `MAX_TURNS_PER_CONVERSATION`.
- Restart proses menghapus state sesi; pelanggan cukup klik "Percakapan baru".
