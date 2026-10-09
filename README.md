<div align="center">

# ⚡ Hastori

**Endüstriyel telemetri platformu** · canlı ölçüm, akıllı alarm, çok kiracılı API

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-000000?logo=nextdotjs&logoColor=white)
![TimescaleDB](https://img.shields.io/badge/TimescaleDB-FDB515?logo=timescale&logoColor=black)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-FF6600?logo=rabbitmq&logoColor=white)
![MQTT](https://img.shields.io/badge/MQTT_5_+_TLS-660066?logo=mqtt&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus_+_Grafana-E6522C?logo=prometheus&logoColor=white)
![Docker](https://img.shields.io/badge/Docker_Compose-2496ED?logo=docker&logoColor=white)

<img src="docs/img/dashboard-dark.jpg" alt="Hastori panosu" width="320"> &nbsp;
<img src="docs/img/dashboard-energy-dark.jpg" alt="Günlük enerji ve açık alarm" width="320">

</div>

7 cihaz her 2 saniyede ölçüm yayınlar; Hastori bunları saklar, kurallara göre alarm üretir ve
tarayıcıya **sayfayı yenilemeden** canlı gösterir. Arayüz Türkçe, kod ve belgeler İngilizce.

## ✨ Neler var

| | |
|---|---|
| 📡 **Güvenli veri hattı** | MQTT 5 + TLS, cihaz başına kimlik ve ACL; mesaj ancak veritabanına yazıldıktan sonra onaylanır, outbox ile kayıp yok |
| 🚨 **Akıllı alarm** | eşik + süre + histerezis, reaktif enerji oranı, susan cihaz (`no_data`); yeniden başlatmada aynı sonuç |
| 🏢 **Çok kiracılı API** | JWT, 3 rol, tesis bazlı yalıtım (başka tesis = 404), yetki matrisi testli |
| 📈 **Canlı panel** | WebSocket ile akan grafik, günlük kWh, alarm onaylama, kural oluşturma, açık/koyu tema |
| 🔭 **Gözlemlenebilirlik** | Prometheus uyarı kuralları, hazır Grafana paneli, istek kimliği ile iz sürme |
| 🛡️ **Sertleştirilmiş geçit** | Caddy: hız sınırı, gövde sınırı, HSTS, `Host` denetimi; imajlar digest ile sabit |

## 🧭 Mimari

```mermaid
flowchart LR
    SIM["🛰️ Simülatör<br/>7 cihaz · 2 sn"]:::edge
    MQ["Mosquitto<br/>MQTT 5 + TLS"]:::broker
    ING["Ingestion"]:::svc
    DB[("TimescaleDB<br/>ölçüm + outbox")]:::db
    RMQ["RabbitMQ<br/>alarm.telemetry + DLQ"]:::broker
    ALM["Alarm servisi"]:::svc
    API["API · FastAPI"]:::svc
    RED[("Redis<br/>oturum · olaylar")]:::db
    CAD["Caddy :8080"]:::gate
    WEB["Next.js"]:::svc
    BR["🖥️ Tarayıcı"]:::edge

    SIM -- "QoS 1" --> MQ --> ING
    ING -- "tek işlem" --> DB
    DB -- "outbox relay" --> RMQ --> ALM
    ALM -- "alarmlar" --> DB
    ALM -- "alarm olayı" --> RED
    ING -- "ölçüm olayı" --> RED
    DB --> API
    RED --> API
    BR <-- "HTTP + WebSocket" --> CAD
    CAD -- "/api/*" --> API
    CAD -- "sayfalar" --> WEB

    classDef edge fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e
    classDef broker fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef svc fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef db fill:#ede9fe,stroke:#7c3aed,color:#3b0764
    classDef gate fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
```

<details>
<summary><b>🔄 Bir arızanın alarma dönüşmesi</b></summary>

```mermaid
sequenceDiagram
    autonumber
    participant D as Cihaz
    participant I as Ingestion
    participant T as TimescaleDB
    participant R as RabbitMQ
    participant A as Alarm servisi
    participant W as Tarayıcı
    D->>I: sıcaklık 85 °C (MQTT)
    I->>T: ölçüm + outbox (tek işlem)
    I-->>D: onay (commit sonrası)
    T->>R: outbox relay
    R->>A: ölçüm
    Note over A: 30 sn eşik üstünde → alarm açılır
    A->>T: alarm kaydı
    A-->>W: alarm.opened (Redis → WebSocket)
    W->>T: Onayla (API) → onaylayanın adı kaydedilir
```

</details>

<details>
<summary><b>🚦 Alarmın durum makinesi</b></summary>

```mermaid
stateDiagram-v2
    direction LR
    [*] --> normal
    normal --> pending: eşik aşıldı
    pending --> normal: süre dolmadan düştü
    pending --> active: süre boyunca aşıldı
    active --> clearing: kapanma eşiğinin altı
    clearing --> active: tekrar yükseldi
    clearing --> normal: 10 sn altında kaldı
```

Onaylamak makinenin durumu değildir: onaylanmış alarm hâlâ aktiftir. Ayrıntı: [tasarım notları](docs/design-notes.md#alarm-service-day-2).

</details>

## 🚀 Hızlı başlangıç

Gerekenler: Docker + Compose, [uv](https://docs.astral.sh/uv/), Git. (`make` yoksa Windows için karşılıkları [docs/operations.md](docs/operations.md) içinde.)

```bash
git clone <repo-url> hastori && cd hastori
make up         # .env, TLS, MQTT kullanıcıları, tüm servisler → http://127.0.0.1:8080
make seed       # demo kurum: 2 tesis, 7 cihaz, 5 kullanıcı, 5 kural
make backfill   # 7 günlük sentetik geçmiş (enerji kartı boş kalmasın)
make simulate   # saha simülatörünü başlat
make fault DEVICE=izmir-komp-1 KIND=overheat   # 30 sn sonra kritik alarm açılır
```

Sonra <http://127.0.0.1:8080> adresini aç. Giriş sayfasındaki **"İzleyici olarak gir"** ve
**"Tesis yöneticisi olarak gir"** düğmeleri parola istemez.

<details>
<summary><b>👤 Demo hesapları</b></summary>

| Rol | E-posta | Parola (`.env`) |
|---|---|---|
| Sistem yöneticisi | `admin@demo.hastori.local` | `SEED_SYSTEM_ADMIN_PASSWORD` |
| Tesis yöneticisi | `izmir.admin@…`, `antalya.admin@…` | `SEED_SITE_ADMIN_PASSWORD` |
| İzleyici | `izmir.izleyici@…`, `antalya.izleyici@…` | `SEED_VIEWER_PASSWORD` |

Varsayılan parolalar herkese açıktır. Yığını `127.0.0.1` dışına açmadan önce `make env-public` çalıştır.

</details>

### Hangi adreste ne var

| | Adres |
|---|---|
| 🖥️ Panel (sayfa + REST + WebSocket) | <http://127.0.0.1:8080> |
| 📘 Swagger | <http://127.0.0.1:8000/api/v1/docs> |
| 📊 Grafana — "Hastori pipeline" | <http://127.0.0.1:3001> (`admin`, parola `.env`'de) |
| 🔥 Prometheus | <http://127.0.0.1:9090> |

Tüm port listesi ve komutlar: [docs/operations.md](docs/operations.md).

## 🖼️ Ekran görüntüleri

| Alarmlar | Alarm ayrıntısı | Yeni kural |
|---|---|---|
| ![Alarmlar](docs/img/alarms-admin.jpg) | ![Ayrıntı](docs/img/alarm-detail.jpg) | ![Kural formu](docs/img/rules-new.jpg) |

| Açık tema | Grafana paneli |
|---|---|
| ![Açık tema](docs/img/dashboard-light.jpg) | ![Grafana](docs/img/grafana-pipeline.jpg) |

> Görüntüler dar pencerede alındı (tek sütun düzeni). Enerji çubukları `make backfill` ile üretilen
> **sentetik** geçmiştir; gerçek ölçüm yalnızca yığının çalıştığı günlerdir.

## 🔐 Güvenlik özeti

- Cihaz parolası saklanmaz: `HMAC-SHA256(gizli, cihaz_id)`; her cihaz yalnızca kendi konusuna yazar.
- Erişim belirteci 15 dk ve bellekte; yenileme belirteci httpOnly çerez, her kullanımda döner, çalınma tespit edilir.
- Parola değişimi ya da pasife alma tüm oturumları hemen bitirir (`token_version`).
- WebSocket tek kullanımlık, 30 sn'lik biletle açılır; belirteç URL'ye girmez.
- Konteynerler yetkisiz kullanıcıyla, salt okunur dosya sistemiyle ve tüm capability'ler kapalı çalışır.

## 📚 Daha fazlası

| Belge | İçerik |
|---|---|
| [docs/design-notes.md](docs/design-notes.md) | Ingestion, alarm servisi, API, panel, gözlemlenebilirlik: nasıl ve neden |
| [docs/operations.md](docs/operations.md) | `make` hedefleri, adresler, API tablosu, yükseltme notları |
| [docs/testing.md](docs/testing.md) | Test türleri, Testcontainers, CI |
| [docs/known-limits.md](docs/known-limits.md) | Bilinen sınırlar, üretimde farklı yapacaklarım |
| [docs/design-risks.md](docs/design-risks.md) | Açık riskler |
| [docs/adr/](docs/adr/) | Kararlar: [TimescaleDB](docs/adr/0001-timescaledb-over-influxdb.md) · [RabbitMQ](docs/adr/0002-rabbitmq-over-kafka.md) · [WebSocket](docs/adr/0003-websocket-over-sse.md) |
| [docs/demo-script.md](docs/demo-script.md) | İki dakikalık gösteri akışı |

`seed/demo.yaml` tek doğruluk kaynağıdır: veritabanı tohumu, Mosquitto parola/ACL dosyaları ve
simülatörün cihaz listesi ondan üretilir.
