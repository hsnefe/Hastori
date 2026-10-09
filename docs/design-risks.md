# Açık riskler: henüz yazılmamış parçalar ve eksik kalanlar

Gün 1 (altyapı) ve Gün 2 (alarm servisi, API) için bulunan sorunlar koda düzeltildi (bkz. README
"Design notes"). Bu belge, **henüz yazılmamış** parçalar (gateway, CI,
yayın) için tasarım aşamasında bilinmesi gereken riskleri ve Gün 2'den **açık kalanları**
toplar. Her madde bir "yapılacak"tır: ilgili günün başında gözden geçirilmeli, çözülünce buradan
silinmeli. Gün 2'de kapanan maddeler (çevrimdışı cihaz için alarm yok: `no_data` kural türü eklendi, G1; alarm süresinin saati, flapping, idempotans, yeniden
başlatma, yetki sızıntısı, refresh rotation yarışı, Redis kalıcılığı, denetim kaydı, reaktif
oranın günlük kümülatif sıçraması) silindi. 2026-10-08'de canlı yığında (`make smoke`, `make e2e`,
`make resilience`, alarm servisi için elle RabbitMQ/DB/kill denemeleri) doğrulandı; günlük kWh
sorgusu 7 günlük veride 27 ms çıktı (G5 kapandı). Gün 4'te Testcontainers entegrasyon testleri (F4, F5) ve CI yazıldı.

Gün 3'te WebSocket maddeleri kapandı ve bu belgeden silindi: C1 (tek kullanımlık `ws-ticket`, sorgu dizesi Caddy günlüğünde yok), C2 (25 sn `hb`, istemci 60 sn sessizlikte yeniden bağlanır), C3 (her abonelikte ve Redis dönüşünde `resync`), C4 (bağlantı başına 256 mesajlık kuyruk, dolunca 1013), C5 (bağlantı en çok 15 dk, yetki her bağlantıda veritabanından kurulur), E4 (geliştirmede Next `rewrites` aynı origin, pakette Caddy tek giriş). Bkz. README "Dashboard (day 3)".

Kaynak notu: emsal projelerden alınan bilgiler (issue, post-mortem, blog) bir araştırma
turunda toplandı ve tek tek elle doğrulanmadı; bağlantılar başlangıç noktasıdır. Mosquitto'da
v5 oturum süresi ile `persistent_client_expiration` etkileşimi gibi bazı noktalar belgede net
değil, denemeyle doğrulanmalı.

## 1. Gün 2'den açık kalanlar

| # | Risk | Karar / önlem |
|---|------|---------------|
| G4 | **Giriş sınırı çok adresli saldırganı durdurmaz.** Gerçek istemci adresi Caddy'den (`CF-Connecting-IP` / `X-Forwarded-For`, yalnızca `TRUSTED_PROXIES`'ten) alınıyor; API e-posta+adres (5) ve yalnız adres (30) sayar, gateway adres başına dakikada 10 giriş/yenileme ve 300 diğer `/api` isteğine izin verir. Kalan: çok sayıda adresten dağıtık deneme. | Hesap başına ikinci, daha yüksek eşik düşünülebilir (kilitlenme riskiyle). Tünelin gerçek adresi iletip iletmediği canlı adreste denenmeli (Funnel için `X-Forwarded-For`, Cloudflare Tunnel için `CF-Connecting-IP`). |
| G6 | **Reaktif oran dakikalık ortalamadan.** Günlük reaktif enerji `greatest(avg_value, 0)` ile dakikalık ortalamadan hesaplanır; kısmen kapasitif bir dakika inductive enerjiyi az gösterir. Uyarı kuralı (alarm servisi) ham örnekle çalışır ve bundan etkilenmez. | Gösterim için kabul edilebilir; hassasiyet gerekirse ham veriden ya da ayrı bir `reactive_pos` cagg'inden hesapla. |
| G7 | **Alarm servisi tek işlemde.** Tek aktif tüketici ve bellekte durum: demo yükünde (~3,5 mesaj/sn) sorun yok; yatay ölçek için cihaz başına bölme gerekir. Yedek kopya `x-single-active-consumer` ile bekler; devralınca (2 dakikadan uzun mesajsız kaldıktan sonraki ilk mesajda) motoru DB'den yeniden kurar. Tek kopya çalıştırılıyor; ikinci kopya canlı yığında denenmedi. | README'de "Known limits". Ölçek gerekirse cihaz kimliğine göre tutarlı karma ile ayrı kuyruklar. |
| G8 | **Alarm olayları (Redis) outbox'sız.** Alarm DB'ye yazıldıktan sonra Redis'e `PUBLISH` edilir; Redis kapalıysa olay kaybolur (sayaç artar, alarm yerinde). Canlı ekran bir sonraki REST okumasında düzelir. | Ekran, bağlantı açılınca ve API Redis'e yeniden bağlanınca `resync` alır ve REST'ten durumu çeker; ayrıca alarm listesi 60 sn'de bir yenilenir (Gün 3). Garanti istenirse alarm olaylarını da outbox'tan geçir. |
| G9 | **Alarm bir kuraldan okunur.** Alarmın adı, önemi ve ölçümü gösterilirken kuraldan okunur; kural düzenlenince geçmiş alarmlar da yeniden adlanır (yalnızca eşikler alarmda saklanır). | `alarms`'a ad/önem/ölçüm anlık görüntüsü sütunları (yeni migration) ve alarm servisinin açarken yazması. |
| G10 | **Reaktif alarm düşük yükte kapanmaz.** Pencerede 2 kWh'ten az enerji varsa oran yoktur; açık reaktif alarm pencere dolana kadar açık kalır ("veri yok iyileşme değildir" kararı). Gece düşük yükte yanlış alarm gibi görünebilir. | Kapanma bekleme süresine düşük enerji için ayrı bir kural ya da gösterimde "oran hesaplanamıyor" etiketi. |
| G11 | **Aynı milisaniyede iki farklı metrik kümesi.** Cihaz aynı `ts` ile önce `temperature_c`, sonra `current_a` gönderirse ölçüm tablosunda ikisi de var, ama `message_id` yalnızca cihaz+ts'ten türediği için ikinci olay outbox'ta düşer ve motor zaten ts'e göre tekilleştirir: alarm servisi ikinciyi görmez. Gerçek cihazlar tüm metrikleri tek mesajda yollar. | Cihaz sözleşmesinde "bir ts = bir mesaj" olarak yaz; gerekirse motor aynı ts'in metriklerini birleştirsin. |
| G12 | **Alarm tüketicisi DB kesintisinde ack'siz bekler.** `_retry` sınırsız bekler; RabbitMQ'nun tüketici ack zaman aşımı (varsayılan 30 dk; 4.3.6 için doğrulanmadı) aşılırsa kanal kapanır, tüketici yeniden bağlanır, mesajlar yeniden teslim edilir (veri kaybı yok, gürültü ve gecikme var). | `consumer_timeout` ayarını izle ya da uzun DB kesintisinde tüketimi bilerek durdur; "son işlenen mesaj yaşı" metriği (G2). |
| G13 | **Yeniden tesliminde alarm olayı tekrar yayınlanmaz.** Süreç commit ile Redis `PUBLISH` arasında ölürse `alarm.opened` canlı ekranlara hiç gitmez; yeniden tesliminde alarm zaten açık olduğu için olay üretilmez (G8'in özel hâli). | Olayları outbox'tan geçir (README'de yazılı); en azından "zaten açık" yolunda olayı yeniden yayınla. |
| G15 | **`clear_threshold` eşiğe eşit olabilir** (sıfır bant); yalnızca 10 sn'lik `CLEAR_HOLD_S` titremeyi sınırlar. | Bandı `>` 0 zorunlu kıl (API ve CHECK) ya da UI'da uyar. |
| G16 | **`no_data` tüm filo susunca alarm vermez.** Sessizlik yalnızca hattın canlı olduğu görülürse cihaza yazılır (başka bir cihazdan veri geliyorsa); her cihaz birden susarsa (örn. iki tesisin birden elektriği gider) bu, ingestion/broker kesintisinden ayırt edilemez ve alarm açılmaz. Hat 20 sn'den uzun susup geri gelince tüm sessizlik sayaçları baştan başlar: gerçekten ölü bir cihazın alarmı o andan `duration_s` sonra açılır. | Hat sağlığı için ayrı uyarı (G2: kuyruk derinliği, `ingest_*` sayaçları). Tek tesisli kurulumda cihazlardan biri hep canlı olmalıdır; gerekirse `no_data`'yı tesis ana sayacına bağla. |
| G17 | **Demo girişi parolasızdır.** `DEMO_LOGIN=true` iken herkes izleyici ya da tesis yöneticisi olarak girer; tesis yöneticisi kuralları değiştirir ve alarm onaylar. Sistem yöneticisi bu yolla açılmaz (rol denetlenir). | Herkese açık adreste gece `make seed-reset` ile kuralları geri al; sınırlama gateway'de giriş ile aynı bölgede. Kapalı kalmak istenen kurulumda `DEMO_LOGIN=false`. |

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
| E5 | **Swagger UI** aynı origin'de ve CDN script'iyle çalışır; `/api/*` üzerindeki Next CSP'si API yanıtlarına uygulanmaz. `API_DOCS=false` (make env-public) ikisini de kapatır. | Docs gerekirse kendi barındırılan varlıklar ve `/api/v1/docs` için ayrı CSP. |
| E7 | **Web:** CSP `style-src 'unsafe-inline'`; tema ilk boyamada parlayabilir; oturum düşünce `?next=` ile geri dönüş yok; grafik `role="img"` içinde tooltip erişilemez; bileşen testleri yardımcıları sınar, render etmez; `start` betiği `standalone` ile uyumsuz. | Render testleri; stil için nonce'a geçiş ancak grafik kütüphanesi izin verirse. |

## 4. Canlı demo barındırma ve CI (Gün 4)

| # | Risk | Karar / önlem |
|---|------|---------------|
| F1 | **Ev sunucusu / Docker Desktop** yeniden başlatmada konteynerleri her zaman kaldırmaz; uyku, güncelleme, elektrik. Docker Desktop, sanallaştırma kapalıyken hiç açılmaz (`HCS_E_HYPERV_NOT_INSTALLED`). | Docker Desktop'ı oturum açılışında başlat, uykuyu kapat, tek komutluk başlatma betiği; video ve ekran görüntüsü her zaman yedek. |
| F2 | **`mosquitto-auth` volume'u `external`.** Temiz bir `docker compose up` bu yüzden düşer; önce `make mqtt-auth` gerekir. Aynı şekilde `infra/redis/redis.pw` yoksa Redis'in compose secret'ı yüklenemez. | README'de açık; `make up` zaten hepsini (`make env` dahil) yapar. İstenirse compose'tan çıkarılıp bir init servisine taşınabilir. |
| F3 | **Cloudflare Tunnel yalnızca HTTP(S)/WS taşır**, MQTT 8883'ü taşımaz. | Simülatör compose içinde olduğu için sorun değil; dışarıdan cihaz bağlanmayacak. |

## 5. Bilinçle kabul edilenler

- Ingestion'da cihaz başına hız sınırı yok (varış zamanına göre sınır, geçerli bir yeniden
  oynatmayı da kısıtlardı). Sınır: 10 000'lik kuyruk, 4096 baytlık mesaj, tek batch yazıcı.
- Timescale imajı 2.17.2'de sabit; yükseltme `ALTER EXTENSION ... UPDATE` gerektirir.
- `LICENSE` dosyası yok (lisans seçimi sahibine ait).
- Onaylayanın görünen adı e-posta adresinin `@` öncesidir (kullanıcıların ayrı bir ad alanı yok); hesap değişse de alarmdaki ad kalır.
- Demo alarm eşikleri, sessizlik sınırı (10 sn), kapanma bekleme süresi (10 sn) ve reaktif oran
  sınırları (0,18 / 0,165, 2 kWh tabanı) demo varsayımıdır ve kodda adlandırılmış sabitlerdir;
  gerçek limitler dağıtım şirketine ve cihaza göre değişir.
- Alarm servisi açılışta yalnızca son 15 dakikayı (ya da en uzun reaktif pencereyi) yeniden
  oynatır: 15 dakikadan uzun süredir açık bir alarm geriye dönük yeniden hesaplanmaz, veritabanındaki
  hâliyle benimsenir; kapanması yalnızca sonraki verilere bağlıdır.
