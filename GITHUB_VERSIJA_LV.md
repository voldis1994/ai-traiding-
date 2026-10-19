# Autonomous v6 — GitHub versija

Šī repozitorija avots: `NEURAL_TRADING_AI_Autonomous_Closed_Candle_v6.zip`.

- Sistēma analizē pabeigtas M1 sveces kopā ar M5/M15/H1 kontekstu.
- Iekļauta Forex Factory ziņu analīze, intermarket dati un 18 pazīmju neironu ievade.
- LIVE signāla izvēlē nav fiksētas 60% vai 8,1 pp prasības.
- Pirmā manuālā darījuma pārņemšana un turpmāka automātiska darbība paredzēta projektā.
- Darba mape `data/` apzināti **nav** augšupielādēta — tā satur brokeru informāciju, apmācības stāvokli un pozīciju vēsturi.

## Windows

Instalē Python un atver `run_windows.bat`. Vispirms pārbaudi DEMO kontā; LIVE orderu darbība un SL/TP jāverificē pašā Capital.com.

**Nepublicē** API atslēgas, `.env`, sesiju tokenus, `robot.sqlite3`, `brain*.npz` vai citus `data/` failus.
