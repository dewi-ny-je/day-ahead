# DAY AHEAD OPTIMIZER

![Supports aarch64 Architecture][aarch64-shield] ![Supports amd64 Architecture][amd64-shield]

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg

## About this fork

This fork of [corneel27/day-ahead](https://github.com/corneel27/day-ahead) adds fixes and
features that were not accepted upstream, either because their usefulness was not
recognised or because they were generated with LLMs. This fork is entirely LLM-driven:
its changes are written, reviewed and merged with the help of large language models.
The branch `dev` follows upstream `main` and carries everything listed below.

### Features not in upstream

| Feature | Purpose |
| --- | --- |
| Calculation chart on demand | Saves the chart data after each run and draws the PNG only when it is opened in the web UI; `graphics` -> `generate png` restores the old behaviour. |
| Production bonus | Configurable bonus on feed-in (for example the Zonneplan Zonnebonus), from upstream PR 878, which is not merged there yet. |
| Mixed weather models | Without a configured `meteoserver-model`, uses harmonie, and fetches gfs for the hours after harmonie only when price prediction extends the planning horizon beyond harmonie's 40 hours, so the extended hours also get weather data. A configured model is always used. |
| Configuration reload | Every process picks up a changed `options.json` or `secrets.json` without a restart, so the dashboard and the API use the same settings as the calculation. |
| Legacy API at the optimiser interval | `/api/report/...` returns quarter-hour values when the optimiser runs at `15min`, and hourly values in the part with predicted prices where the optimiser calculates per hour. Energy fields are average power in kW per interval, so quarters and hours are comparable; each record carries `interval_minutes`. Only the hourly part of the prognoses is merged to hours: the quarter-hour solar forecast beyond it keeps its resolution. |
| Report data pushed to Home Assistant | `report` -> `ha_sensors` maps report fields (da, consumption, production, bat_in, bat_out, base, pv_ac, soc, ...) to HA entities. Each entity's state is the value of the current interval and its `data` attribute holds today and tomorrow as `/api/report/<field>/vandaag_en_morgen` does, so REST sensors polling the API are no longer needed. Energy series are in kW, every entity gets a `last_update` attribute, an entity used twice is rejected, and `include_price_prediction` extends the series to the predicted prices. |
| Calculation chart as stepped areas | The energy-balance and battery charts of the calculation PNG fill each series as a stepped area, so intervals join up without gaps; gridlines every 6 hours are drawn over the areas, and the time axis runs exactly from the first to the last interval. |
| Slimmer Docker image | Drops the CBC debug build, duplicate shared libraries, the NVIDIA NCCL dependency (xgboost-cpu), unused MySQL drivers, and the compilers, npm and headers in the runtime image; `build_miplib.sh` installs its toolchain itself when `use_self_compiled_miplib` is on. |
| Same irradiance for ML and DAO solar predictions | Both predictors are fed the same measured/forecast irradiance, so their results can be compared. |
| Release workflows | The version is typed once in `dao/config.yaml`, the image is built for a single architecture when that is all you need, and a manually given release tag that matches no `config.yaml` is written into it instead of stopping the build. |
| Development setup | `run_dev.sh` detects and repairs a broken virtual environment; the Vite dev server works from another machine, behind a proxy and with several network addresses; troubleshooting notes in `DEVELOP.md`. |

### Fixes not in upstream

| Fix | Purpose |
| --- | --- |
| Battery value at the end of the horizon | Averages the DC-to-AC efficiency over the power axis, not over table rows. The impact on prediction is basically none, but at least it has a physical basis, while averaging on bare table rows has none. |
| Network loss | Scheduled tasks are killed after 30 minutes, HTTP requests time out (10 s for Home Assistant, 60 s for the others), and MySQL and PostgreSQL connections get driver timeouts and keepalives, so a lost connection no longer blocks the scheduler until a restart. |
| Report API errors | `/api/report/...` answers with a JSON reason: 503 when Home Assistant or the database cannot be reached, 500 otherwise, instead of Flask's HTML error page. |
| Daylight saving time in reports | The report API no longer fails on the day DST ends, and the repeated 02:00 hour stays a separate row instead of being added to the first one; hourly rows are grouped by their UTC start. |
| Consolidate task | Runs to the end and saves the hourly cost and profit, continues after the last consolidated hour, stores local hours instead of hours shifted by the UTC offset, and keeps both repeated hours at the end of DST. |
| Predicted quarter-hour prices | Sources that predict per quarter (epexpredictor, energypriceforecast_eu, jeroen_nl) no longer return each predicted slot up to four times; only hourly predictions are interpolated. |
| Baseload of the first interval | A run that starts partway through an interval scales the first interval's baseload in the unoptimised reference, the "Overig verbr." bar, "Cost before optimize" and the logged prognoses table, as the optimisation already did. |
| `prices_spot: false` | The calculation PNG no longer draws the spot price line when it is disabled. |
| Task exit codes | A failed optimisation or price prediction exits with code 1, `day_ahead.py` stops at the first failed task, and `/api/run/` answers 500 when the task fails. |
| Smaller fixes | Empty-state page of the v2 chart view, Sass deprecation warnings, `.DS_Store` ignored. |

---

## Inleiding
Het programma Day Ahead Optimizer (DAO) voert de volgende acties, 
berekeningen en bewerkingen uit: 

* ophalen dynamische energie tarieven bij Entsoe en/of NordPool
* ophalen van meteogegevens bij Meteoserver
* berekenen van de optimale inzet van een aanwezige batterij, wp-boiler en elektrische auto.<br>

Het programma draait alleen als addon op HA-installaties met een arm64 processor (bijv Raspberry Pi4) of 
een intel 64 bit processor (amd64). Maar het kan ook separate Docker-container worden geinstalleerd

---
## Documentatie 
In de loop van 2025 is begonnen met het verhuizen en actualiseren van de documentie vanaf DOCS.md
naar de wiki-pagina's op github: https://github.com/corneel27/day-ahead/wiki
---

## Installatie
Deze is uitgebreid beschreven in de wiki: https://github.com/corneel27/day-ahead/wiki/2.-Installatie-en-basis-configuratie



---
## Optimization
De optimalisering van het verbruik gebeurt met behulp van een generiek wiskundig algoritme
met de naam "Mixed-Integer lineair Programming". Meer daarover kun je lezen op de 
website die ook het algoritme en allerlei bijbehorende hulpmiddelen aanbiedt:
https://python-mip.com/

Deze implementatie berekent een optimale inzet van je batterij, boiler en e.v., waarbij naar keuze wordt 
gestreefd naar minimalisering van je kosten, naar minimalisering van je inkoop (nul op de meter) of 
een combinatie van beide. Daarvoor worden de volgende zaken berekend:
* uit de prognose van het weer (globale straling) per uur wordt een voorspelling berekend van de productie van je 
zonnepanelen
* met de tarieven van je dynamische leverancier (incl. opslag, belastingen en btw) worden per uur de kosten 
en opbrengsten van het verbruik c.q. teruglevering berekend
* m.b.v. de karakteristieken van je accu worden per uur het laad- c.q. ontlaadvermogen berekend
* wanneer moet je elektrische auto worden geladen

Dit resulteert (in de mip-module) in enkele honderden vergelijkingen en idem dito variabelen(onbekenden). 
Aan de hand van de gekozen strategie kan met behulp van het algoritme de meest optimale setting van al deze 
variabelen worden berekend. Dit zijn:
* per uur verbruik en kosten op de inkoopmeter
* per uur teruglevering en opbrengst op de inkoopmeter
* per uur laad- c.q. ontlaadvermogen van de batterij en de SoC aan het einde van het uur
* tijdstip waarop de boiler moet worden opgewarmd
* uurvakken waarin de elektrische auto moet worden geladen

Het geheel kan grafisch worden weergegeven:

![optimalisering](dao/images/optimum2300.png "optimalisering")

Of in tabelvorm:
````
         uur  accu_in  accu_out     soc  con_l  c_t_t  c_t_n  bas_l   boil     wp     ev     pv  kos_l  kos_t  k_t_n   b_tem
0      23.00     0.00      0.00   20.00   0.55   0.00   0.00   0.55   0.00  -0.00   0.00   0.00   0.15  -0.00  -0.00   37.50
1       0.00     0.00      0.00   20.00   0.68   0.00   0.00   0.68   0.00  -0.00   0.00   0.00   0.17  -0.00  -0.00   37.10
2       1.00     0.00      0.00   20.00   1.93   0.00   0.00   1.03   0.90  -0.00   0.00   0.00   0.49  -0.00  -0.00   48.36
3       2.00     0.00      0.00   20.00   1.18   0.00   0.00   1.18   0.00  -0.00   0.00   0.00   0.29  -0.00  -0.00   47.96
4       3.00     0.22      0.00   20.71   1.76   0.00   0.00   0.65   0.00   0.23   0.66   0.00   0.41  -0.00  -0.00   47.56
5       4.00     4.50      0.00   35.02   7.64   0.00   0.00   0.54   0.00   0.30   2.30   0.00   1.63  -0.00  -0.00   47.16
6       5.00     0.00      0.00   35.02   0.63   0.00   0.00   0.63   0.00  -0.00   0.00   0.00   0.15  -0.00  -0.00   46.76
7       6.00     0.00      0.00   35.02   0.70   0.00   0.00   0.70   0.00   0.00   0.00   0.00   0.19  -0.00  -0.00   46.36
8       7.00     0.00      0.00   35.02   0.64   0.00   0.00   0.65   0.00  -0.00   0.00   0.01   0.18  -0.00  -0.00   45.96
9       8.00     0.00      0.00   35.02   0.11   0.00   0.00   0.34   0.00  -0.00   0.00   0.23   0.03  -0.00  -0.00   45.56
10      9.00     0.00      0.00   35.02   0.00   1.28   0.00   0.31   0.00  -0.00   0.00   1.59   0.00  -0.34  -0.00   45.16
11     10.00     4.50      0.00   49.32   2.46   0.00   0.00   0.16   0.00   0.23   0.00   2.42   0.55  -0.00  -0.00   44.76
12     11.00     4.50      0.00   63.62   2.37   0.00   0.00   0.38   0.00   0.30   0.00   2.81   0.51  -0.00  -0.00   44.36
13     12.00     4.50      0.00   77.93   2.91   0.00   0.00   0.69   0.00   0.30   0.00   2.58   0.63  -0.00  -0.00   43.96
14     13.00     4.50      0.00   92.23   3.80   0.00   0.00   0.59   0.00   0.30   0.00   1.59   0.83  -0.00  -0.00   43.56
15     14.00     0.00      0.00   92.23   0.21   0.00   0.00   1.13   0.00   0.23   0.00   1.14   0.05  -0.00  -0.00   43.16
16     15.00     0.00      0.00   92.23   0.00   0.97   0.00   0.93   0.00  -0.00   0.00   1.90   0.00  -0.23  -0.00   42.76
17     16.00     0.00      0.00   92.23   0.00   0.46   0.00   0.53   0.00  -0.00   0.00   0.99   0.00  -0.13  -0.00   42.36
18     17.00     0.00      4.50   74.17   0.00   4.21   0.00   1.02   0.00  -0.00   0.00   0.73   0.00  -1.34  -0.00   41.96
19     18.00     0.00      4.50   56.12   0.00   5.26   0.00   0.51   0.00  -0.00   0.00   1.27   0.00  -1.79  -0.00   41.56
20     19.00     0.00      4.50   38.06   0.00   3.92   0.00   0.62   0.00  -0.00   0.00   0.04   0.00  -1.37  -0.00   41.16
21     20.00     0.00      4.50   20.00   0.00   3.88   0.00   0.62   0.00  -0.00   0.00   0.00   0.00  -1.25  -0.00   40.76
22     21.00     0.00      0.00   20.00   0.62   0.00   0.00   0.62   0.00  -0.00   0.00   0.00   0.18  -0.00  -0.00   40.36
23     22.00     0.00      0.00   20.00   0.55   0.00   0.00   0.55   0.00  -0.00   0.00   0.00   0.15  -0.00  -0.00   39.96
24     23.00     0.00      0.00   20.00   0.55   0.00   0.00   0.55   0.00  -0.00   0.00   0.00   0.15  -0.00  -0.00   39.56
````
Heb je wensen, opmerkingen, suggesties, commentaar of kritiek: alles is welkom op de github pagina 
van deze addon. Bij het onderdeel issues:<br>
https://github.com/corneel27/day-ahead/issues <br>
of bij de discussions:<br>
https://github.com/corneel27/day-ahead/discussions <br>
Voordat je een issue invult of een discusie begint: doorzoek de geschiedenis, misschien is het probleem al 
eerder aangekaart.<br>
Of nog beter: Plaats een pull request met een oplossing of een nieuwe feature!
