# Henüz kodu olmayan bileşenlerin tasarım riskleri

Gün 1 altyapısı (simülatör, MQTT, ingestion, TimescaleDB, outbox, RabbitMQ) için bulunan sorunlar
koda düzeltildi (bkz. README "Design notes"). Bu belge, **henüz yazılmamış** parçalar için
tasarım aşamasında bilinmesi gereken riskleri ve alınacak kararları toplar. Her madde bir
"yapılacak"tır: ilgili günün başında gözden geçirilmeli, çözülünce buradan silinmeli.

Kaynak notu: emsal projelerden alınan bilgiler (issue, post-mortem, blog) bir araştırma
turunda toplandı ve tek tek elle doğrulanmadı; bağlantılar başlangıç noktasıdır. Mosquitto'da
v5 oturum süresi ile `persistent_client_expiration` etkileşimi gibi bazı noktalar belgede net
değil, denemeyle doğrulanmalı.

## 1. Alarm servisi (Gün 2) — en yüksek risk

| # | Risk | Karar / önlem |
|---|------|---------------|
| A1 | **Cihaz susarsa alarm ne olur?** `pending` süresiz kalır, açık alarm hiç kapanmaz ya da hiç açılmaz. Şemada `no_data`/`offline` kavramı yok. | Kuralda `no_data_s` (veri gelmeme süresi) ve cihaz "çevrimdışı" durumu tasarla; alarm kuralı değerlendirmesinde son örnek yaşını kontrol et. Yeni migration gerekir (0004 yalnızca CHECK ve indeks ekledi). |
| A2 | **Süre (`duration_s`) hangi saate göre?** Cihaz saati kayabilir (ingestion −65 dk / +30 sn kabul ediyor); işleme saati ise yeniden oynatmada (restart, kuyruk birikmesi) yanlış sonuç verir. | Süreyi **mesaj zaman damgasıyla** hesapla, ama ardışık iki örnek arasında üst sınır koy (boşluk = süre sıfırlanır). Restart'ta Redis durumuna güvenme: son N dakikayı `measurements`'tan yeniden oynat. |
| A3 | **Flapping / alarm fırtınası.** Eşik civarında titreşim, aynı olaydan `warning` + `critical` iki alarm. | Hem açma gecikmesi (`duration_s`) hem kapatma gecikmesi; `clear_threshold` zorunlu olsun (CHECK ile `>` için ≤ eşik, `<` için ≥ eşik zaten var); bildirim başına hız sınırı ve aynı cihaz/metrik için tek alarm. ISA-18.2'de "chattering" (60 sn'de ≥3 geçiş) ve 10 dakikada 10 alarm fırtına eşiği yaygın referanstır. |
| A4 | **İdempotans.** RabbitMQ en-az-bir-kez teslim eder; outbox, yeniden oynatmada aynı olayı tekrar üretebilir. | Tüketici `message_id`'yi **Postgres'te** (kalıcı) tekilleştirsin, Redis'e güvenmesin; alarm kayıtları `uq_alarms_open_rule` ile zaten tek açık alarma kilitli. |
| A5 | **Kuyruk sessizce siler.** `alarm.telemetry` kuyruğu `drop-head` + 1 saat TTL ile taşan/eskiyen mesajı bildirmeden atar. | Kuyruk derinliği ve düşen mesaj için uyarı (Gün 4 Prometheus); tüketici açılışta DB'den son N dakikayı yeniden oynasın; hata durumunda `reject(requeue=False)` + dead-letter exchange (poison mesaj döngüsü olmasın). Kuyruk argümanı değiştirmek mevcut kuyrukta `PRECONDITION_FAILED` verir: kuyruğu silip yeniden oluştur, notunu migration'a düş. |
| A6 | **Reaktif oran (%18 / %20).** Günlük kümülatif oran gece yarısında payda ~0 olduğu için sıçrar; "gün" UTC mi yerel mi belirsiz; şemada türetilmiş metrik yok (kural tek cihaz + tek anlık metrik). | Asgari aktif enerji eşiği (günün ilk X kWh'ı dolmadan uyarma); gün sınırı `Europe/Istanbul`; türetilmiş metrik için ayrı kural tipi (migration). Kapasitif (negatif) reaktif artık kabul ediliyor, oran hesabı işareti hesaba katmalı. |
| A7 | **Demo senaryosu.** Varsayılan `make fault` artık 60 sn; kural 30 sn. | `test_default_overheat_trips_the_demo_rule` ikisini birbirine bağlar. Kural ya da simülatör değişirse test kırılır. |

Test vakaları baştan yazılmalı: anlık sıçrama, flapping, cihaz susması, `pending` ortasında
restart, saat kayması, aynı mesajın iki kez gelmesi.

## 2. API, kimlik doğrulama ve çok kiracılı yetki (Gün 2)

| # | Risk | Karar / önlem |
|---|------|---------------|
| B1 | **Tenant sızıntısı (IDOR/BOLA).** `GET /alarms/{id}`, `POST /alarms/{id}/ack`, `GET /devices/{id}/measurements` tesisi dolaylı türetir (`alarm → device → site → user_sites`); bir uçta filtreyi unutmak yeter. | Filtreyi veri erişim katmanında zorla (ya da Postgres RLS); id'li her uç için "başka tesisin nesnesi → 404" testi; `viewer` ack yapamaz testi; yetki değişikliği JWT claim'inde 15 dk eski kalmasın: tesis listesini her istekte (veya kısa TTL ile) DB'den çöz. |
| B2 | **Refresh token rotation yarışı.** Çok sekmede iki paralel refresh: ikincisi "yeniden kullanım" sayılıp tüm token ailesi iptal edilir, herkes oturumdan düşer. | Redis'te atomik tüketim (Lua ya da `GETDEL`), 10-30 sn grace window (aynı yanıtı tekrar ver), aile bazlı iptal, frontend'de tek-uçuşlu (single-flight) refresh. |
| B3 | **Redis kalıcılığı.** Redis'te refresh token tutulacak; kalıcılık kapalıysa her restart herkesi çıkarır. | `appendonly yes` ve compose'ta volume; oturum kaybı kabul edilebilirse README'ye yaz. |
| B4 | **Parola/seed.** Demo kullanıcı parolaları belgede açık. | Dışarı açılacak demo için `make env-public`; ilk girişte parola değiştirme (isteğe bağlı). E-posta artık büyük/küçük harfe duyarsız tekil (migration 0004). |
| B5 | **Denetim kaydı.** `audit_log` yazımları merkezi değilse eksik kalır. | Kural/kullanıcı değişikliklerini tek servis fonksiyonundan geçir. |

## 3. WebSocket ve canlı yayın (Gün 2-3)

| # | Risk | Karar / önlem |
|---|------|---------------|
| C1 | **`/ws?token=<jwt>`** URL'de loglara (Caddy, Cloudflare), tarayıcı geçmişine ve Referer'a düşer. | `POST /ws-ticket` (Bearer ile, ~30 sn, tek kullanımlık, Redis `GETDEL`), WebSocket `?ticket=`; proxy'de WS sorgu dizesini loglama. Tasarım dokümanı bunu `?token=` olarak yazıyor: güncellenmeli. |
| C2 | **Cloudflare boşta WebSocket'i ~100 sn'de keser.** | Sunucudan 25-30 sn'de bir ping; istemci reconnect + backoff. |
| C3 | **Redis pub/sub teslimat garantisi vermez.** Reconnect arasında olaylar kaybolur. | Reconnect sonrası REST ile son durumu (snapshot) çek; olaylara sıra no. |
| C4 | **Yavaş istemci** yayın döngüsünü tıkar. | Bağlantı başına sınırlı gönderim kuyruğu; dolunca bağlantıyı kes. |
| C5 | **Token süresi dolunca açık WS** yetkisiz kalır; yetki değişince bağlantı düşmez. | WS'i token süresiyle kapat ya da mesajla yenile; abonelikte `user_sites` kontrolü, kanal adı tesis bazlı. |

## 4. TimescaleDB (Gün 2-3)

| # | Risk | Karar / önlem |
|---|------|---------------|
| D1 | **Retention ile cagg çakışması.** `refresh_continuous_aggregate('measurements_1m', NULL, NULL)` ya da 7 günden uzun `start_offset`, cagg'i ham veriyle birlikte siler. | README ve 0002'de uyarı var. Saatlik/günlük cagg'ler için ayrı refresh politikası; günlük cagg'i saatlik cagg'den türet (hierarchical). |
| D2 | **Yerel saatle günlük toplam.** `time_bucket('1 day', time, 'Europe/Istanbul')` cagg'de plan sorunları çıkarabilir (tüm chunk'ları tarar). | Günlük kWh için gerçek veriyle `EXPLAIN`; gerekirse saatlik cagg'den topla. |
| D3 | **Sıkıştırma açılırsa** `uq_measurements` + `ON CONFLICT DO NOTHING` sıkıştırılmış chunk'larda yavaşlar. | `compress_segmentby='device_id,metric'`; eski chunk'ta tekilleştirme testi. Şu an kapalı. |
| D4 | **Real-time cagg** (`materialized_only=false`) her sorguda son dakikaları ham tablodan hesaplar. | Dashboard sorgu sayısı artınca ölçülmeli. |

## 5. Dashboard, gateway, gözlemlenebilirlik (Gün 3-4)

| # | Risk | Karar / önlem |
|---|------|---------------|
| E1 | **Grafana/Prometheus tüm tesis verisini gösterir.** Tunnel ile açılır ya da varsayılan parola kalırsa tenant izolasyonu anlamsız. | Tunnel dışında tut, anonim erişimi kapat, parolayı `.env`'e koy. |
| E2 | **Rate limiting** gateway'de planlı; Cloudflare Tunnel arkasında istemci IP'si `CF-Connecting-IP`'de, yoksa herkes tek IP görünür. | Caddy'de gerçek IP başlığını yapılandır, login için ayrı sıkı limit. |
| E3 | **Prometheus scrape yok.** `/metrics` açık ama scrape ve alarm kuralı tanımlı değil. | `ingest_outbox_depth`, `ingest_mqtt_connects_total` sıçraması, `ingest_rejected_total`, kuyruk derinliği için uyarı. |

## 6. Canlı demo barındırma ve CI (Gün 4)

| # | Risk | Karar / önlem |
|---|------|---------------|
| F1 | **Ev sunucusu / Docker Desktop** yeniden başlatmada konteynerleri her zaman kaldırmaz; uyku, güncelleme, elektrik. | Docker Desktop'ı oturum açılışında başlat, uykuyu kapat, tek komutluk başlatma betiği; video ve ekran görüntüsü her zaman yedek. |
| F2 | **`mosquitto-auth` volume'u `external`.** Temiz bir `docker compose up` bu yüzden düşer; önce `make mqtt-auth` gerekir. | README'de açık; `make up` zaten hepsini yapar. İstenirse compose'tan çıkarılıp bir init servisine taşınabilir. |
| F3 | **Cloudflare Tunnel yalnızca HTTP(S)/WS taşır**, MQTT 8883'ü taşımaz. | Simülatör compose içinde olduğu için sorun değil; dışarıdan cihaz bağlanmayacak. |
| F4 | **Testcontainers + Timescale:** Postgres "ready" log satırı iki kez çıkar; bekleme stratejisi ikincisini beklemezse test ilk (geçici) sunucuya bağlanır. | `wait_for_logs(..., occurrence=2)` ya da gerçek sorgu/`pg_isready -h 127.0.0.1`; ≥60 sn zaman aşımı. Mosquitto için sertifika ve auth volume'u CI'da da hazırlanmalı. |
| F5 | **Entegrasyon testi yok.** Migration, Timescale (hypertable/cagg/retention), `writer_loop`, `relay_once`, `Publisher` ve gerçek ack yolu yalnızca canlı stack'te `make smoke` / `make resilience` ile sınanıyor. | Gün 4'te Testcontainers ile DB+broker entegrasyon testleri ve CI. |

## 7. Bilinçle kabul edilenler

- Ingestion'da cihaz başına hız sınırı yok (varış zamanına göre sınır, geçerli bir yeniden
  oynatmayı da kısıtlardı). Sınır: 10 000'lik kuyruk, 4096 baytlık mesaj, tek batch yazıcı.
- Timescale imajı 2.17.2'de sabit; yükseltme `ALTER EXTENSION ... UPDATE` gerektirir.
- `LICENSE` dosyası yok (lisans seçimi sahibine ait).
