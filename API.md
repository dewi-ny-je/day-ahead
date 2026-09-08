# Day Ahead Optimizer – JSON API (v2)

Alle endpoints hangen onder `/v2/api/` van de dao-webserver
(standaard poort `5000`). De api kent geen eigen authenticatie: draai hem
alleen op een vertrouwd (lokaal) netwerk of achter een reverse proxy die de
authenticatie afhandelt.

Tijdstempels worden - behalve in `/data/`, dat om compatibiliteitsredenen
ongewijzigd is gebleven - teruggegeven als ISO-8601 met tijdzone-offset.
Met de queryparameter `timezone` (bijv. `timezone=Europe/Brussels`) kan een
andere tijdzone dan `Europe/Amsterdam` worden gekozen; de tijdstippen worden
dan omgerekend, niet alleen anders gelabeld.

Fouten komen terug als json met een `error`-sleutel:

| status | betekenis                                                        |
|--------|-------------------------------------------------------------------|
| `400`  | onbruikbare parameter (onbekende tijdzone, ongeldige datum, onbekende taak, body die geen object is) |
| `404`  | gevraagde installatie of dataset bestaat niet                     |
| `409`  | er loopt al een taak                                              |
| `500`  | fout bij het ophalen uit de database of het rekenwerk             |

## Verhouding tot de oudere api onder `/api/`

Naast deze v2-api bestaat de oudere api onder `/api/`, beschreven in de wiki:
[6. Gebruik van de API](https://github.com/corneel27/day-ahead/wiki/6.-Gebruik-van-de-API).
Die blijft werken; er verandert niets aan. Twee van de endpoints hieronder
komen ervoor in de plaats:

| oud (legacy)                        | nieuw                                       | waarom                                                                                                                     |
|-------------------------------------|---------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| `GET /api/run/<bewerking>`          | `POST /v2/api/task-exec/` + `/task-state/`  | het oude endpoint blokkeert tot de taak klaar is en levert html; gunicorn draait met `timeout = 120`, dus een optimalisering die langer duurt krijgt sowieso geen antwoord |
| `GET /api/report/da/<periode>`      | `GET /v2/api/prices/`                       | vrije start- en einddatum in plaats van vaste periodenamen, plus de dagstatistieken (min, max, gemiddelde, goedkoopste uren) |

Ook `GET /v2/api/run/<task>` blokkeert en is daarmee vervangen door
`/task-exec/`; zie de opmerking onderaan.

Voor de overige velden van `/api/report/<veld>/<periode>` is er geen
vervanging nodig: `/v2/api/data/` levert dezelfde gegevens, maar dan alle
gevraagde variabelen in één aanroep en over een vrij te kiezen periode. Wie de
oude vorm gebruikt hoeft niets te wijzigen.

De namen van de taken verschillen tussen oud en nieuw:

| `/api/run/<bewerking>` | `/v2/api/task-exec/` |
|------------------------|----------------------|
| `calc_zonder_debug`    | `optimize`           |
| `calc_met_debug`       | `optimize_debug`     |
| `get_prices`           | `prices`             |
| `get_meteo`            | `meteo`              |
| `get_tibber`           | `tibber`             |
| `calc_baseloads`       | `calc_baseloads`     |
| `train_ml_predictions` | `train_ml`           |
| (geen)                 | `consolidate`, `clean` |

## Data

### `GET /v2/api/data/`

De gemeten (`v`) en voorspelde (`f`) waarden per variabele.

| parameter   | verplicht | omschrijving                                             |
|-------------|-----------|----------------------------------------------------------|
| `start`     | ja        | ISO-datum(tijd), bijv. `2026-09-07T00:00:00`              |
| `end`       | ja        | ISO-datum(tijd), exclusief                                |
| `aggregate` | ja        | `15min`, `hour`, `day`, `week` of `month`                 |
| `fields`    | nee       | komma-gescheiden codes, bijv. `da,cons,prod`; leeg = alle |

```json
[
  {"ts": "2026-09-07 14:00", "da": {"v": 0.1234, "f": null},
                             "cons": {"v": 0.51, "f": 0.48}}
]
```

### `GET /v2/api/vars/`

De variabelen waarvoor daadwerkelijk data aanwezig is: `[{"code", "name"}]`.

### `GET /v2/api/prices/`

De day ahead tarieven per uur, inclusief morgen zodra die gepubliceerd zijn.
Parameters `start` en `end` zijn optionele datums (standaard vandaag t/m
overmorgen).

Dit endpoint komt in de plaats van `GET /api/report/da/<periode>`. Het verschil
is dat de periode vrij te kiezen is in plaats van een vaste naam als
`vandaag_en_morgen`, en dat de dagstatistieken (min, max, gemiddelde en de
goedkoopste uren) meekomen in plaats van dat de client ze zelf uitrekent. De
oude aanroep blijft gewoon werken.

```json
{
  "dim": "euro/kWh",
  "series": [{"time": "2026-09-07T14:00:00+02:00", "price": 0.1234}],
  "today": {"date": "2026-09-07", "hours": 24, "min": 0.02, "max": 0.31,
            "average": 0.1234,
            "cheapest": {"time": "...", "price": 0.02},
            "most_expensive": {"time": "...", "price": 0.31},
            "cheapest_hours": ["...", "...", "..."]},
  "tomorrow": null
}
```

## Machine learning

### `GET /v2/api/solar-devices/`

De pv-installaties waarvoor `ml_prediction` aanstaat, inclusief of er al een
getraind model aanwezig is.

### `GET /v2/api/solar-ml/`

Per uur de gemeten productie naast de dao-voorspelling en de ml-voorspelling
van één installatie, plus de r²-scores en dagtotalen.

| parameter | verplicht | omschrijving                                       |
|-----------|-----------|-----------------------------------------------------|
| `device`  | nee       | naam van de installatie; leeg = de eerste met ml     |
| `date`    | nee       | dag `YYYY-MM-DD`; leeg = vandaag                     |

### `GET /v2/api/ml-models/`

Per model wanneer het getraind is en - als de training met deze versie is
gedraaid - de trainingsstatistieken (`test_r2`, `test_mae`, `test_rmse`,
`feature_importance`, `best_params`, ...). Deze worden bij het trainen
weggeschreven als `<model>_stats.json` naast het `.pkl`-bestand.

### `GET /v2/api/baseloads/`

De berekende basislast per weekdag (`0` = maandag) met 24 uurwaarden, plus de
reeks van vandaag met tijdstempels. Levert `404` zolang de taak
`calc_baseloads` nog niet gedraaid heeft.

## Taken

### `GET /v2/api/status/`

Versie, huidige tijd en tijdzone, de status van de lopende taak en een
samenvatting van de configuratie.

### `POST /v2/api/task-exec/`

Start een taak asynchroon. Body als json (`{"task": "optimize"}`) of
form-encoded. Antwoordt met `202` bij starten, `409` als er al een taak loopt
en `400` bij een onbekende taak.

Dit is de opvolger van `GET /api/run/<bewerking>`. Waar het oude endpoint
wacht tot de taak klaar is en de log als html-pagina teruggeeft, keert dit
endpoint direct terug en volg je de taak via `/task-state/`. Dat is niet
alleen prettiger: `gunicorn_config.py` zet `timeout = 120`, dus een taak die
langer dan twee minuten duurt - zoals een optimalisering - laat de synchrone
variant zonder antwoord achter. Het deelt zijn
statusbestand met de web-ui, zodat de ui en een api-client dezelfde lopende
taak zien en er niet twee tegelijk gestart kunnen worden.

Geldige taken: `optimize` (alias `optimize_regular`), `optimize_debug`,
`prices` (`update_prices`), `meteo` (`update_meteo`), `tibber`
(`update_tibber`), `calc_baseloads`, `train_ml`, `consolidate`, `clean`.

### `GET /v2/api/task-state/`

Status van de taak (`idle`, `running`, `done`, `error`, `cancelled`), hoelang
hij loopt en de laatste 8000 tekens van het logbestand.

### `POST /v2/api/task-cancel/`

Breekt de lopende taak af.

### `GET /v2/api/run/<task>` (legacy)

Draait een taak *synchroon* en geeft de log als platte tekst terug. Bedoeld
voor handmatig gebruik; gebruik voor clients `/task-exec/`, omdat een
optimalisering langer kan duren dan de meeste http-timeouts. Dit endpoint
kent geen bescherming tegen twee taken tegelijk en deelt zijn status niet met
de web-ui.

## Legacy: de api onder `/api/`

Deze endpoints blijven ongewijzigd werken en zijn beschreven in de wiki:
[6. Gebruik van de API](https://github.com/corneel27/day-ahead/wiki/6.-Gebruik-van-de-API).
Ze staan hier alleen om de verhouding tot de v2-api duidelijk te maken.

| endpoint                                | status  | opmerking                                                          |
|-----------------------------------------|---------|--------------------------------------------------------------------|
| `GET /api/run/<bewerking>`              | legacy  | synchroon, levert html; vervangen door `/v2/api/task-exec/`         |
| `GET /api/report/<veld>/<periode>`      | in gebruik | voor `da` vervangen door `/v2/api/prices/`; overige velden ook in `/v2/api/data/` |

`GET /api/prognose/<veld>` staat in de broncode maar is uitgecommentarieerd en
bestaat dus niet als route.
