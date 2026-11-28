# NeuralTradingAI — OANDA LIVE / SL+TP

**Platformas statuss:** Capital.com ir aizstāts ar OANDA. Admin pievieno `API token` + `Account ID` (`broker_type=oanda`). Async adapteris: `app/broker.py` (`OandaBroker`) + `app/stream.py` (`OandaQuoteStream`). Zemāk esošās CLI komandas (`preflight.py`, `latency.py`) ir no OANDA LIVE latency pakotnes un izmanto sync klientu `app/oanda.py`.

Šī dokumentācija apraksta OANDA v20 LIVE (fxTrade) plūsmu. Shadow un Frozen ir iekšēji modeļi; lokālais replay ir izstrādes instruments, nevis alternatīvs brokera režīms. Capital.com adapteri vairs netiek atbalstīti. Nav automātiskas reālu orderu aktivizēšanas bez apzinātas konfigurācijas.

## Pozīciju noteikumi

- Tikai fiksēts SL/TP: brokerī GTC aizsardzība kopā ar FOK market orderi. Trailing Stop, pretējā signāla izeja un taimera izeja NAV atļautas. Lokālā simulatorā nav manuālas slēgšanas funkcijas. Brokeris vai lietotājs ārpus robota var slēgt darījumu; SL nav garantēts un gap/slippage var palielināt zaudējumus.
- Shadow BUY/SELL/HOLD mācību piemērs tiek pabeigts tikai tad, kad abi virtuālie virzieni sasniedz savu SL/TP. Nepabeigts piemērs netiek mākslīgi aizvērts vai apgūts. `horizon` ir vienīgi pētījuma ieejas embargo pirms novērojumu perioda beigām, nevis ordera ilgums. Līdz 16 neatkarīgiem kontrafaktuāliem piemēriem vienlaikus (konfigurējams līdz 128). Sasniedzot ietilpību, vecākais neatrisinātais piemērs tiek atzīmēts kā cenzēts bez izdomāta P&L. Tas neatceļ reālu vai papīra pozīciju. Mācību piemēri var būt savstarpēji korelēti.
- SL/TP distances ir pārskatītās konfigurācijas parametri. Tās netiek automātiski optimizētas katram darījumam un netiek pārvietotas pēc atvēršanas. OANDA faktiskā fill cena var atšķirties no simulētās; brokerī SL tiek aprēķināts no sliktākās atļautās ieejas cenas (`priceBound`) un noapaļots uz drošāko pusi. Faktiskā SL distance var būt mazāka, bet gap/slippage var pārsniegt zaudējumu risku. Shadow fiksē distances no simulētās fill cenas; precīza brokeru izpildes paritāte nav pierādīta.

## Instalācija

Python 3.11+ (testēts ar lokāli pieejamo Python), izolēta vide:

```sh
python -m venv .venv
# Aktivizēt .venv atbilstoši savai OS
python -m pip install -r requirements.txt
python -m pytest -q
```

API atslēgu un konta ID ievadīt tikai savā datorā vides mainīgajos `OANDA_API_TOKEN` un `OANDA_ACCOUNT_ID`. Nekad neiekļaut tos ZIP, GitHub vai čatā. Nepieciešams v20 LIVE konts; konta saderība nav apstiprināta. Piemērā `XAU_USD` ir tikai instrumenta piemērs, ne rekomendācija.

```sh
python preflight.py --instrument XAU_USD
python live.py --config configs/live.shadow.example.json --data-dir data --seconds 3600
```

Otrā komanda izmanto īstu LIVE datu plūsmu, bet NESŪTA orderus. Vismaz 61 derīga, secīga pabeigta M1 svece nepieciešama pēc iesildīšanās/pārrāvuma. Datu buferis neatkarīgi saņem kotējumus; apstrādes laiks ieskaitīts pieejamības latentumā, faktiskā saņemšana saglabāta `wire_received_at`. Ja buferis pārpildās, sistēma aptur sesiju. `max_latency=2` un `max_gap=10` ir testa konfigurācijas sliekšņi, jāizmēra faktiskajā plūsmā. OANDA sampled stream nav visi tirgus tick.

## Datu ātrums — bez orderiem

Vispirms sinhronizē datora pulksteni (UTC/NTP). Atsevišķs OANDA LIVE monitors:

```sh
python latency.py --instrument XAU_USD --seconds 259200 --out data/latency.json
```

Atver `data/latency.html` pārlūkā. Tas pārlasa failu ik pēc 5 sekundēm. Terminalī tiek rādīti JSON mērījumi, `data/latency.json` satur jaunāko stāvokli, `data/latency.jsonl` — periodiskos mērījumu kopsavilkumus. Slīdošais logs: pēdējie 10 000 mērījumu; tas nav visu 72 stundu kopējais percentile. Negatīvs datu vecums signalizē pulksteņa/timestamp nesaskaņu. Nav datu ≠ 0 ms. Pārtrūkstot sesijai, atkārtoti palaid monitoru; automātiska reconnect nav ieviesta.

Atsevišķā terminālī var mērīt arī gatavo REST M1 sveču pirmo pieejamību:

```sh
python latency.py --instrument XAU_USD --seconds 259200 --rest-probe --out data/rest_m1.jsonl
```

REST aptauja aptuveni reizi sekundē plus HTTP apstrāde; rezultāts ir pirmās novērošanas kavējums, ne precīzs brokera publikācijas laiks. Sākotnējā vēsturiskā atbilde netiek skaitīta. Tirgum esot slēgtam, mērījumu var nebūt. Izvēlies 72 aktīva tirgus stundas.

Arī `live.py` automātiski veido `data/<konts>/<instruments>/latency.html`, `latency.json`, `latency.jsonl`. Rāda kotējuma vecumu, rindu, apstrādi, M1 robežu un kotējumu intervālus atsevišķi. Izmanto `--latency-report` ar unikālu `.json` failu, ja vajadzīgs cits ceļš. Nelieto vienu izvades failu divos procesos.

## Datu ieraksts un neatkarīga izpēte

```sh
python run.py capture --instrument XAU_USD --seconds 28800 --out recordings/xau.jsonl
python research.py --config configs/live.shadow.example.json --input recordings/xau.jsonl --select-from <UTC_A> --test-from <UTC_B> --out results/research
```

Aizvieto robežas ar reāliem ISO UTC laikiem (piemēram, 2026-01-05T03:00:00+00:00), atstājot pietiekami daudz datu visiem trim periodiem. 30 pabeigti darījumi katram modelim ir minimālā aprakstošās validācijas prasība, ne statistisks pierādījums. Neatrisinātas pozīcijas, nezināma izpilde, gap/rejections un negatīvs P&L neļauj apstiprināt modeli. Ievades faila izcelsmes apzīmējums `recorded_input` nav kriptogrāfisks brokera apliecinājums. Pārbaudīt, ka datu izcelsme ir autentiska, izmaksas un risks atbilst kontam. Ja pētījums iztur validāciju, tikai pēc manuālas pārbaudes apstiprināt konkrētos kontrolpunktus:

```sh
python review_model.py approve --db data/registry.db --instrument XAU_USD --candidate <candidate_checkpoint.json> --frozen <frozen_checkpoint.json>
```

Kandidāta dinamiskā evolūcija ir eksperimentāla un aktivizējama Shadow konfigurācijā (`evolve_candidate`, `evolution_interval`, `max_neurons`, `grow_threshold`, `prune_age`). Freeze/holdout laikā arhitektūra un svari nemainās. Sākotnējā `core.py` matemātika nav mainīta. Katrs kandidāts jāpārbauda ar reāliem datiem; neironu dzimšana nav uzlabojuma garantija.

## LIVE orderu funkcija — tikai pēc apstiprinātas pārbaudes

Aizpildi `configs/live.risk.example.json` kopiju `configs/live.risk.json`. Visi riska parametri ir obligāti, nav noklusējuma apjoma vai zaudējumu sliekšņa. Pirms modeļa izpētes konfigurē skaitlisku `max_spread` Shadow konfigurācijā, identisku riska konfigurācijai; `null` piemērs nozīmē tikai nenoteiktu novērošanas filtru un neļauj arm. Apstiprinātā modeļa konfigurācijā arī jābūt identiskam limitam. `units` un `max_units` ir instrumenta vienības, NAV/marža/dienas slieksnis konta valūtā, spread/drift cenas vienībās. Dienas NAV slieksnis ir pret pirmo dienas novērojumu un nav absolūta zaudējumu garantija. Netiek aprēķināts valūtas konvertēts risks uz darījumu.

Tikai pēc autentiska konta un modeļa validācijas, un ar apzinātu reālu orderu atļauju:

```sh
python live.py --config configs/live.shadow.example.json --data-dir data --registry data/registry.db --risk configs/live.risk.json --confirm-account <LIVE_KONTA_ID> --arm
```

**Šī komanda var atvērt reālus darījumus.** Izmantot vienu instrumentu, vienu procesu un tam paredzētu kontu; neizmantot ar vienlaicīgu citu robotu vai manuālo tirdzniecību. Sveša pozīcija, mainīti brackets vai neapstiprināts orderis izraisa apturēšanu. Manuāla pirmā darījuma pārņemšana nav ieviesta. Automātiska LIVE Promotion nav atļauta. Svara versiju drīkst mainīt pēc neatkarīgas izpētes un manuālas pārskatīšanas.

## Uzraudzība un atjaunošana

```sh
python status.py --account-data-dir data/<LIVE_KONTA_ID>
```

Read-only lokāls statusa rīks; tas nav vecā GUI aizstājējs. SQLite ieraksta kotējumus, mācību piemērus un orderu intentus. Brokera apstiprinātas slēgšanas transakcijas sasaista ar robota orderi `outcome_links`; atsevišķos cash laukus nesummē patvaļīgi. Tiešā svaru apmācība no faktiskā P&L vēl nav savienota. Nerealizēts P&L netiek izmantots. Restartā žurnāls deterministiski atkārto visu vēsturi (laiks aug kopā ar DB apjomu). Mainot kodu vai konfigurāciju, vajag jaunu mācību žurnālu un no jauna validēt modeļus; NEIZDZĒST orderu DB. Ja vecā versija jau izmantota reālai tirdzniecībai, neuzsāc jaunu orderu žurnālu, lai apietu persisted halt vai nezināmu intent; vispirms saskaņo kontu. Neskaidras brokeru atbildes gadījumā intent paliek PENDING, automātiska atkārtošana aizliegta, nepieciešama konta pārbaude. Esošie brokera SL/TP paliek aktīvi pēc izslēgšanas. Trūkstošas aizsardzības gadījumā halt un manuāla pārbaude, ne automātiska pozīcijas slēgšana.

## Atlikušās robežas

Nav veikts īsts OANDA konta tests vai reāls fill; nav apstiprināta rentabilitāte. Pilns legacy Fusion (intermarket, news, likviditāte), pilns Structure Shadow skolotājs, adaptīva SL/TP distances izvēle, tieša faktiskā P&L apmācība, manuālā pirmā darījuma pārņemšana un administrācijas GUI nav migrēti. Oriģinālā OHLC struktūras uztvere ir saglabāta, bet tā nav pilna legacy sistēma. Precīzie testi un ierobežojumi `docs/FINAL_STATUS_LV.md`. Arhīvs nav jāizmanto kā nepārbaudīts reālās tirdzniecības aizstājējs.
