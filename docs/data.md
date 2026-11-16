# Data

Datakällor för priser, stödtjänster, tillrinning och verklig drift. Tillbaka till [README](../README.md).


- Spotpriser: Nord Pool day-ahead SE3, från november 2022 via elprisetjustnu.se (kvartspriser
  sedan oktober 2025) och tidigare timpriser tillbaka till november 2011 från Energinets
  Energi Data Service. Tiden anges i CET året runt.
- Obalanspriser: eSett Open Data (EXP14), per kvart.
- Stödtjänster: Svenska kraftnäts Mimer, med kapacitetspriser och upphandlade volymer per
  timme för FCR-N, FCR-D upp/ned och mFRR upp/ned i SE3.
- Tillrinning: SMHI:s station 2258 Idre 3 (2 384 km²) i Österdalälven, uppströms Trängslet.
  Den är oreglerad, har data sedan 1977 och vårflod i maj. Den specifika avrinningen skalas
  med arean och fördröjs med en linjär magasinsmodell.
- Verklig tappning: SMHI:s station 2436 vid Spjutmo kraftverk mäter det reglerade flödet per
  dygn sedan 1996. Det används för att kalibrera tillrinningen och för att jämföra modellen
  med hur Fortum faktiskt har tappat.
- Verklig produktion: ENTSO-E:s transparensplattform, produktion per aggregat (dokumenttyp
  A73) för Trängslet G1–G3 sedan februari 2018. Den publiceras för aggregat från 100 MW och
  per kontrollområde, så Åsen och Spjutmo finns inte med.

Källor:
- [elprisetjustnu.se](https://www.elprisetjustnu.se/elpris-api)
- [Energi Data Service: Elspotprices (API)](https://api.energidataservice.dk/dataset/Elspotprices?limit=5)
- [eSett Open Data](https://opendata.esett.com/)
- [Svenska kraftnät: Mimer](https://mimer.svk.se/)
- [SMHI: Idre 3, vattenföring (API)](https://opendata-download-hydroobs.smhi.se/api/version/latest/parameter/1/station/2258.json)
- [SMHI: Spjutmo kraftverk, vattenföring (API)](https://opendata-download-hydroobs.smhi.se/api/version/latest/parameter/1/station/2436.json)
- [ENTSO-E Transparency Platform: Actual Generation per Generation Unit](https://transparency.entsoe.eu/)
- [Svenska kraftnät: villkor för FCR](https://svk.se/siteassets/aktorsportalen/bsp-och-brp/tidigare-balansansvarsavtal/avtal-4620-8-bilaga-3-villkor-fcr.pdf) (upphandling före och efter day-ahead)
- [Svenska kraftnät: mFRR-kapacitetsmarknaden](https://www.svk.se/en/about-us/news/news/first-daily-auction-for-the-national-mfrr-capacity-market-on-17-october) (stängning 07.30 dagen före)
