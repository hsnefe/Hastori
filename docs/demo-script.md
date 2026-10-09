# Demo walkthrough (about two minutes)

For the recording of the project video and for anyone showing it live. Everything runs on the
local stack: `make up`, `make seed`, `make backfill`, `make simulate`, then open
<http://127.0.0.1:8080>.

Before recording: `make seed-reset` (rules and passwords as in `seed/demo.yaml`), a window about
1280 x 800, dark theme, Grafana signed in on another tab (`http://127.0.0.1:3001`, user `admin`,
password `GRAFANA_ADMIN_PASSWORD` from `.env`).

1. **Sign in (10 s).** The sign-in page; press "Tesis yöneticisi olarak gir". Say that the demo
   buttons need no password and never open the system admin.
2. **Live dashboard (25 s).** Four device cards update every 2 s, the live chart follows the main
   panel, the daily energy shows seven bars of history (synthetic, from `make backfill`) and
   today's partial day with its coverage.
3. **A fault becomes an alarm (35 s).** In a terminal: `make fault DEVICE=izmir-komp-1 KIND=overheat`.
   The temperature on Kompresör-1 climbs; after 30 s a critical "Kompresör-1 yüksek sıcaklık"
   alarm appears in "Açık alarmlar" without a refresh. Press "Onayla": the badge reads
   "Onaylandı · izmir.admin" (a name, never the e-mail address).
4. **Rules (20 s).** "Kurallar" -> "Yeni kural": a threshold, a reactive ratio and a "veri
   gelmezse" (silence) rule can be created here; the form refuses a closing threshold on the wrong
   side.
5. **Observability (30 s).** Grafana "Hastori pipeline": telemetry flow (about 3.5 messages a
   second), ingestion lag, open alarms, queue depths, API traffic. Show that the alarm just raised
   is in "Open alarms".

Afterwards: `make seed-reset` brings the rules back, `make down` stops the stack.
