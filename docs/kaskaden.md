# Kaskaden

Anläggningsdata för Trängslet, Åsen och Spjutmo, med källor och kalibrering. Tillbaka till [README](../README.md).


| | Trängslet | Åsen | Spjutmo | Källa |
|---|---:|---:|---:|---|
| Effekt | 300 MW | 24 MW | 35 MW | Fortum |
| Fallhöjd | 142 m | 24 m | 21,4 m | Fortum |
| Normal årsproduktion | 651 GWh | 117 GWh | 138 GWh | Fortum |
| Magasin | 880 Mm³ | cirka 9 Mm³ | ingen egen reglering | Länsstyrelsen Dalarna |
| Regleringsamplitud | 35 m | – | – | Länsstyrelsen Dalarna |
| Aggregat | 3 | 2 Kaplan | 1 Kaplan | Trängslet: ENTSO-E; Spjutmo: Litostroj; Åsen: antagande |
| Maxflöde | 250 m³/s | 112 m³/s | 185 m³/s | Trängslet: Länsstyrelsen; övriga härledda ur effekt och fallhöjd |
| Minimitappning | – | 21 m³/s | – | Länsstyrelsen Dalarna |
| Ändringstakt | fri | högst 30 m³/s per timme | följer Åsen | Länsstyrelsen Dalarna |
| 1 juni–31 augusti | fri | ingen korttidsreglering, högst 40 m³/s per dygn | följer Åsen | Länsstyrelsen Dalarna |
| Gångtid till nästa station | 2 h | 5 h | – | Åsen–Spjutmo: Länsstyrelsen (4–5 h); Trängslet–Åsen: antagande |
| Lokalt avrinningsområde | 4 230 km² | 280 km² | 1 410 km² | kalibrerat mot SMHI:s flöde vid Spjutmo |

Åsens magasin räcker till minimitappningen i fem dygn utan tappning från Trängslet, alltså
cirka 9 Mm³. Väsa, Blyberg och Spjutmo har inga egna regleringsmagasin utan följer Åsens
tappning med 4–5 timmars fördröjning. I modellen har Spjutmo en damm på 0,5 Mm³, som jämnar ut
inom någon timme. Väsa och Blyberg ägs gemensamt i separata bolag och ingår inte.

Åsen tar emot mindre än hälften av vad Trängslet kan släppa och får inte korttidsregleras på
sommaren. Körs Trängslet fullt längre än Åsens magasin räcker spills vatten vid Åsen, så
samordningen mellan stationerna betyder mycket.

Avrinningsområdena är skalade så att den naturliga tillrinningen i medel stämmer med det
uppmätta flödet vid Spjutmo kraftverk under hydrologiska åren 1996–2025 (91,6 m³/s). Över många
år är tappningen lika med tillrinningen, eftersom magasinen går runt. Förhållandet är stabilt:
1,088 för 1996–2010 och 1,081 för 2010–2025 jämfört med den tidigare skalningen efter
normalårsproduktionen.

Trängslets verkningsgrad är kalibrerad mot ENTSO-E. Den högsta uppmätta produktionen
2018–2026 är 290 MW, vilket vid 250 m³/s och 142 m ger 0,83 vid full last. Fortums verkliga
tappning vintern 2025/26 gav 6,5 % mindre el än modellen med 0,93 vid bästa last, alltså 0,87.
Kalibreringen gäller samma vinter som backtesten, men det är en fysisk parameter som påverkar
alla strategier lika.

Källor:
- [Fortum: Trängslets kraftverk](https://www.fortum.com/se/om-oss/elproduktion/vattenkraft/vattenkraftverk/dalalven/trangslets-kraftverk)
- [Fortum: Åsens kraftverk](https://www.fortum.com/se/om-oss/elproduktion/vattenkraft/vattenkraftverk/dalalven/asens-kraftverk)
- [Fortum: Spjutmo kraftverk](https://www.fortum.com/se/om-oss/elproduktion/vattenkraft/vattenkraftverk/dalalven/spjutmo-kraftverk)
- [Länsstyrelsen Dalarna: Dalälvens vattenkraftssystem (2017)](https://nedredalalven.se/filer/about/Rapport_2017-03_Dal%C3%A4lvens_vattenkraftssystem_energiproduktion.pdf), s. 35–41 och 98–101 (vattenhushållningsbestämmelser för Trängslet och Åsen)
- [Litostroj: Spjutmos Kaplanaggregat](https://www.litostrojpower.com/news/2019/07/82-Litostroj-Power-signed-a-contract-with-Fortum-AB-for-turbine-refurbishment-of-HPP-Spjutmo-in-Sweden)
- [Wikipedia: Trängslet Dam](https://en.wikipedia.org/wiki/Tr%C3%A4ngslet_Dam)
- [Elområden per kommun](https://boio.se/el/elomrade)

