# Açık riskler: henüz yazılmamış parçalar ve eksik kalanlar

Gün 1 (altyapı) ve Gün 2 (alarm servisi, API) için bulunan sorunlar koda düzeltildi (bkz. README
"Design notes"). Bu belge, **henüz yazılmamış** parçalar (gateway, CI,
yayın) için tasarım aşamasında bilinmesi gereken riskleri ve Gün 2'den **açık kalanları**
toplar. Her madde bir "yapılacak"tır: ilgili günün başında gözden geçirilmeli, çözülünce buradan
silinmeli. Gün 2'de kapanan maddeler (alarm süresinin saati, flapping, idempotans, yeniden
başlatma, yetki sızıntısı, refresh rotation yarışı, Redis kalıcılığı, denetim kaydı, reaktif
oranın günlük kümülatif sıçraması) silindi. 2026-10-08'de canlı yığında (`make smoke`, `make e2e`,
`make resilience`, alarm servisi için elle RabbitMQ/DB/kill denemeleri) doğrulandı; günlük kWh
sorgusu 7 günlük veride 27 ms çıktı (G5 kapandı). Kalan fark: Testcontainers + CI (F4, F5, Gün 4).

Gün 3'te WebSocket maddeleri kapandı ve bu belgeden silindi: C1 (tek kullanımlık `ws-ticket`, sorgu dizesi Caddy günlüğünde yok), C2 (25 sn `hb`, istemci 60 sn sessizlikte yeniden bağlanır), C3 (her abonelikte ve Redis dönüşünde `resync`), C4 (bağlantı başına 256 mesajlık kuyruk, dolunca 1013), C5 (bağlantı en çok 15 dk, yetki her bağlantıda veritabanından kurulur), E4 (geliştirmede Next `rewrites` aynı origin, pakette Caddy tek giriş). Bkz. README "Dashboard (day 3)".

Kaynak notu: emsal projelerden alınan bilgiler (issue, post-mortem, blog) bir araştırma
turunda toplandı ve tek tek elle doğrulanmadı; bağlantılar başlangıç noktasıdır. Mosquitto'da
v5 oturum süresi ile `persistent_client_expiration` etkileşimi gibi bazı noktalar belgede net
değil, denemeyle doğrulanmalı.

## 1. Gün 2'den açık kalanlar

| # | Risk | Karar / önlem |
|---|------|---------------|
| G1 | **Çevrimdışı cihaz için alarm yok.** Cihaz susarsa açık alarm açık kalır, bekleyen süre boşluk kuralıyla sıfırlanır; ama "cihaz 60 sn'dir veri göndermiyor" diye bir alarm üretilmez (`no_data_s` yazılmadı). Dashboard kartı çevrimdışı rozeti gösterir (son ölçüm 30 sn'den eski; Gün 3'te yapıldı). | Alarm istenirse `kind: no_data` kural türü (yeni migration) ve alarm servisinde cihaz başına son `ts` zamanlayıcısı (~2 sa; MVP bitince bonus). |
| G2 | **Kuyruk ve DLQ için uyarı yok.** `alarm.telemetry` derinliği ve `alarm.telemetry.dlq` büyümesi yalnızca RabbitMQ yönetim ekranında ve `alarm_rejected_total` sayacında görünür; eskiyen mesaj DLQ'ya gider ama kimseyi uyarmaz. | Gün 4 Prometheus: `alarm.telemetry` derinliği, DLQ derinliği, `alarm_eval_lag_seconds`, `ingest_outbox_depth` için uyarı kuralları (E3 ile birlikte). |
| G3 | **Parola değişikliği refresh oturumlarını bitirmez.** Erişim belirteci her istekte veritabanından doğrulanır (rol, tesis, kullanıcı varlığı); refresh belirteci yalnızca Redis'e karşı. Parolası değişen ya da pasife alınan kullanıcının açık refresh oturumu 7 güne kadar yenilenebilir. | Kullanıcı başına belirteç sürümü (`users.token_version`, refresh kaydında saklanır) ya da Redis'te `user -> aileler` indeksi; parola değişince ve kullanıcı değişiminde aileleri iptal et. |
| G4 | **Giriş sınırı adres döndüren saldırganı durdurmaz.** Sayaç e-posta + istemci adresi başına; bir hesaba farklı adreslerden denemeyi yalnızca gateway'in IP sınırı yavaşlatır. Caddy/Cloudflare arkasında `request.client` proxy'nin adresidir. | Gün 4: gateway'de gerçek IP başlığını (`CF-Connecting-IP`, `X-Forwarded-For`) yapılandır ve API'de yalnızca güvenilen proxy'den kabul et; login için gateway'de ayrı sıkı limit (E2). Hesap başına ikinci, daha yüksek eşik düşünülebilir (kilitlenme riskiyle). |
| G6 | **Reaktif oran dakikalık ortalamadan.** Günlük reaktif enerji `greatest(avg_value, 0)` ile dakikalık ortalamadan hesaplanır; kısmen kapasitif bir dakika inductive enerjiyi az gösterir. Uyarı kuralı (alarm servisi) ham örnekle çalışır ve bundan etkilenmez. | Gösterim için kabul edilebilir; hassasiyet gerekirse ham veriden ya da ayrı bir `reactive_pos` cagg'inden hesapla. |
| G7 | **Alarm servisi tek işlemde.** Tek aktif tüketici ve bellekte durum: demo yükünde (~3,5 mesaj/sn) sorun yok; yatay ölçek için cihaz başına bölme gerekir. Yedek kopya `x-single-active-consumer` ile bekler, devralınca açılışta DB'den yeniden oynatır. | README'de "Known limits". Ölçek gerekirse cihaz kimliğine göre tutarlı karma ile ayrı kuyruklar. |
| G8 | **Alarm olayları (Redis) outbox'sız.** Alarm DB'ye yazıldıktan sonra Redis'e `PUBLISH` edilir; Redis kapalıysa olay kaybolur (sayaç artar, alarm yerinde). Canlı ekran bir sonraki REST okumasında düzelir. | Ekran, bağlantı açılınca ve API Redis'e yeniden bağlanınca `resync` alır ve REST'ten durumu çeker; ayrıca alarm listesi 60 sn'de bir yenilenir (Gün 3). Garanti istenirse alarm olaylarını da outbox'tan geçir. |

## 2. TimescaleDB (Gün 3)

| # | Risk | Karar / önlem |
|---|------|---------------|
| D1 | **Retention ile cagg çakışması.** `refresh_continuous_aggregate('measurements_1m', NULL, NULL)` ya da 7 günden uzun `start_offset`, cagg'i ham veriyle birlikte siler. | README ve 0002'de uyarı var. Saatlik/günlük cagg'ler için ayrı refresh politikası; günlük cagg'i saatlik cagg'den türet (hierarchical). |
| D2 | **Yerel saatle günlük toplam** büyük aralıkta plan sorunları çıkarabilir (tüm chunk'ları tarar). | `EXPLAIN` ölçüldü (7 gün, 2 pano: 27 ms, chunk elemesi çalışıyor); yalnızca çok daha fazla tesiste yeniden bak. |
| D3 | **Sıkıştırma açılırsa** `uq_measurements` + `ON CONFLICT DO NOTHING` sıkıştırılmış chunk'larda yavaşlar. | `compress_segmentby='device_id,metric'`; eski chunk'ta tekilleştirme testi. Şu an kapalı. |
| D4 | **Real-time cagg** (`materialized_only=false`) her sorguda son dakikaları ham tablodan hesaplar. | Dashboard sorgu sayısı artınca ölçülmeli. |

## 3. Dashboard, gateway, gözlemlenebilirlik (Gün 3-4)

| # | Risk | Karar / önlem |
|---|------|---------------|
| E1 | **Grafana/Prometheus tüm tesis verisini gösterir.** Tunnel ile açılır ya da varsayılan parola kalırsa tenant izolasyonu anlamsız. | Tunnel dışında tut, anonim erişimi kapat, parolayı `.env`'e koy. |
| E2 | **Rate limiting** gateway'de planlı; Cloudflare Tunnel arkasında istemci IP'si `CF-Connecting-IP`'de, yoksa herkes tek IP görünür. | Caddy'de gerçek IP başlığını yapılandır, login için ayrı sıkı limit (G4). |
| E3 | **Prometheus scrape yok.** `/metrics` ingestion ve alarm servisinde açık ama scrape ve alarm kuralı tanımlı değil; API'de `/metrics` yok. | `ingest_outbox_depth`, `ingest_mqtt_connects_total` sıçraması, `ingest_rejected_total`, `alarms_open`, kuyruk ve DLQ derinliği için uyarı (G2); API için istek sayısı/gecikme metrikleri. |

## 4. Canlı demo barındırma ve CI (Gün 4)

| # | Risk | Karar / önlem |
|---|------|---------------|
| F1 | **Ev sunucusu / Docker Desktop** yeniden başlatmada konteynerleri her zaman kaldırmaz; uyku, güncelleme, elektrik. Docker Desktop, sanallaştırma kapalıyken hiç açılmaz (`HCS_E_HYPERV_NOT_INSTALLED`). | Docker Desktop'ı oturum açılışında başlat, uykuyu kapat, tek komutluk başlatma betiği; video ve ekran görüntüsü her zaman yedek. |
| F2 | **`mosquitto-auth` volume'u `external`.** Temiz bir `docker compose up` bu yüzden düşer; önce `make mqtt-auth` gerekir. Aynı şekilde `infra/redis/redis.pw` yoksa Redis'in compose secret'ı yüklenemez. | README'de açık; `make up` zaten hepsini (`make env` dahil) yapar. İstenirse compose'tan çıkarılıp bir init servisine taşınabilir. |
| F3 | **Cloudflare Tunnel yalnızca HTTP(S)/WS taşır**, MQTT 8883'ü taşımaz. | Simülatör compose içinde olduğu için sorun değil; dışarıdan cihaz bağlanmayacak. |
| F4 | **Testcontainers + Timescale:** Postgres "ready" log satırı iki kez çıkar; bekleme stratejisi ikincisini beklemezse test ilk (geçici) sunucuya bağlanır. | `wait_for_logs(..., occurrence=2)` ya da gerçek sorgu/`pg_isready -h 127.0.0.1`; ≥60 sn zaman aşımı. Mosquitto için sertifika ve auth volume'u CI'da da hazırlanmalı. |
| F5 | **Gerçek TimescaleDB/RabbitMQ entegrasyon testi yok.** Migration 0002 (hypertable/cagg/retention), `writer_loop`, `relay_once`, `Publisher` ve alarm tüketicisinin gerçek RabbitMQ yolu yalnızca canlı yığında `make smoke` / `make resilience` / `make e2e` ile sınanıyor. | Gün 4'te Testcontainers ile TimescaleDB + RabbitMQ entegrasyon testleri ve CI; `pgserver` tabanlı hızlı testler yanında kalır. |

## 5. Bilinçle kabul edilenler

- Ingestion'da cihaz başına hız sınırı yok (varış zamanına göre sınır, geçerli bir yeniden
  oynatmayı da kısıtlardı). Sınır: 10 000'lik kuyruk, 4096 baytlık mesaj, tek batch yazıcı.
- Timescale imajı 2.17.2'de sabit; yükseltme `ALTER EXTENSION ... UPDATE` gerektirir.
- `LICENSE` dosyası yok (lisans seçimi sahibine ait).
- Demo alarm eşikleri, sessizlik sınırı (10 sn), kapanma bekleme süresi (10 sn) ve reaktif oran
  sınırları (0,18 / 0,165, 2 kWh tabanı) demo varsayımıdır ve kodda adlandırılmış sabitlerdir;
  gerçek limitler dağıtım şirketine ve cihaza göre değişir.
- Alarm servisi açılışta yalnızca son 15 dakikayı (ya da en uzun reaktif pencereyi) yeniden
  oynatır: 15 dakikadan uzun süredir açık bir alarm geriye dönük yeniden hesaplanmaz, veritabanındaki
  hâliyle benimsenir; kapanması yalnızca sonraki verilere bağlıdır.
