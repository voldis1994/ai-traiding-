# Neural Trading AI — GitHub versija (OANDA)

- Brokeris: **OANDA v20 LIVE** (API token + Account ID). Capital.com nav atbalstīts.
- Portāli: `/admin` (operators), `/app` (klients), `/portal`.
- Darba mape `data/` **nav** GitHubā — tā satur brokeru atslēgas, apmācību un pozīciju vēsturi.

## Windows

1. Instalē Python
2. `run_windows.bat` → `http://127.0.0.1:8765`
3. `/admin` → izveido klientu → pieslēdz OANDA
4. Klients: `/app` ar admin iedoto login

**Nepublicē** API tokenus, `.env`, `robot.sqlite3`, `brain*.npz` vai citus `data/` failus.
