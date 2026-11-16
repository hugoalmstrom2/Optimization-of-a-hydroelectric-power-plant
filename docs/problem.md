# Problem som dök upp under arbetet

De flesta är typiska för vattenkraftsplanering. Tillbaka till [README](../README.md).


- Spill i förtid under vårfloden. När prognosen säger att magasinet blir fullt spelar det
  ingen roll för en deterministisk modell när spillet sker, så den rullande planen spillde i
  onödan dygn efter dygn. Spillstraffet är nu något högre ju tidigare spillet sker.
- Tomt magasin före vårfloden. SDP:n kände först inte till vattendomen nedströms.
  Optimeringen tömde magasinet och bröt minimitappningen i 54 timmar när den naturliga
  tillrinningen var lägre än kravet.
- Minimitappning som inte går att uppfylla. Ett hårt villkor ger ingen lösning alls efter en
  prognosmiss. Villkoret är därför mjukt med högt straff, dammar med minimitappning planeras
  med säkerhetsmarginal, och varje bruten timme redovisas.
- Vattenvärde från efterhandspriser. Ett vattenvärde från senaste månadens medelpris släpar
  efter när priserna stiger, och optimeringen tömde magasinet på "billigt" vatten. SDP:n
  löser det med säsong, fyllnadsgrad och osäkerhet.
- För litet magasin och fel tillrinningsregim. Med en regleringsgrad på 7 % blev magasinet
  fullt både vid vårfloden och under hösten. En station i Värmland hade vårflod i mars–april,
  vilket inte passar ett fjällmagasin.
- Vinst som egentligen var en säsongssatsning. Första helårskörningen gav +27 %, men hälften
  kom från att MILP lämnade magasinet nästan tomt. Strategin som följer styrkurvan och
  konfidensintervallet skiljer nu korttidsvinsten från säsongsomfördelningen.
- För svaga tumregler. Tumregeln fick först aldrig köra Trängslet mer än Åsen tar emot och
  tog samma vattenmängd varje dygn. Nu finns fem regler, däribland veckotopp och
  vattenvärdesbud, och vinsten redovisas mot den som gick bäst.
- Vatten som släpptes förbi en stillastående turbin. När ett magasin tog slut drog
  simuleringen ned flödet under minsta last, och då gick vattnet förbi turbinen i stället för
  att sparas. Nu står turbinen och vattnet sparas.
- Bud och leverans som inte stämde överens. Budscenarierna planerades först över tre dygn och
  leveransen över sju. En strategi tjänade då 1,5 M€ på obalans som inte hade med planeringen
  att göra. Nu använder båda samma modell och horisont.
- Budkurva som tog slut. Det verkliga priset hamnade under alla fem scenarierna i 14 % av
  kvartarna, och då gällde det lägsta scenariots volym även vid 7 €/MWh. Nu täcker scenarierna
  hela felfördelningen och budet faller till noll vid vattenvärdet.
- Budkurva som rättades i efterhand. Scenarierna optimerades först var för sig, och deras
  körplaner bröt budkurvan i 6–36 kvartar per dygn: ett scenario med lägre pris ville
  producera mer. Kurvan rättades genom att höja de lägre buden, vilket gav för stora
  åtaganden. Nu är budkurvan ett villkor i tvåstegsmodellen.
- Stödtjänster som låste produktionen. Reserverna bestämdes först i mittenscenariot och låstes
  sedan i alla scenarier. Nedregleringsreserver tvingade då fram produktion när priset blev
  lågt, i snitt 54 MW vid 11 €/MWh, och på fyra veckor kostade det 1,2 M€ för 0,35 M€ i
  reservintäkt. Att bara sälja det som alla scenarier ville sälja tog bort problemet, men då
  såldes för lite mFRR upp: 2,7 M€ på ett år mot 13,3 M€ med perfekt information.
  Tvåstegsmodellen väger intäkt mot flexibilitet och gav 4,2 M€ mer netto.
- Leveranskontroll per produkt. Leveransen kontrollerades först produkt för produkt med FCR
  först, medan optimeringen kunde använda samma marginal till mFRR. Då såg 1 400 MWh i veckan
  ut att inte gå att leverera fast planen höll. Nu prövas det sålda mot kaskadens samlade
  marginaler.
- Fel område i frågan till ENTSO-E. Produktion per aggregat publiceras per kontrollområde,
  för Sverige hela landet, och inte per elområde. Frågan för SE3 gav ett tomt svar.
- Ett mjukt villkor som var billigt att bryta. På sommardygnen räknades straffet för att ändra
  Åsens tappning bara för en kvart. MILP höll dygnsgränsen första kvarten och ändrade sedan
  tappningen resten av dygnet. Simuleringen jämnar ut dygnet, så Åsen tömdes och
  minimitappningen bröts i 9–14 timmar den 15–16 augusti. Nu kostar en ändring för hela
  dygnet, och ett test visar att det inte längre lönar sig.
- Numeriskt fel i lösaren. HiGHS avbröt en optimering med "Solve error" (12 januari,
  vattenvärde +20 %). Felet beror på vägen genom sökningen, så ett nytt försök med längre
  tidsgräns gav samma fel. Nu görs nya försök med andra slumpfrön.
- Tumregler utan marginal. Reglerna släppte från Trängslet exakt det som minimitappningen vid
  Åsen krävde enligt tillrinningsprognosen. När Åsens damm var tom och tillrinningen blev
  några procent lägre bröts minimitappningen med 0,1–0,7 m³/s, upp till 112 timmar om året.
  Nu räknar reglerna med 20 % lägre lokal tillrinning än prognosen, som var som mest 17 % för
  hög under backtesten.
- Tumregler som släppte vatten förbi turbinen. Flödet för minimitappningen, cirka 18 m³/s, är
  mindre än minsta last i Trängslet, 33 m³/s. Reglerna släppte det förbi turbinerna, i
  veckotopp 39 % av tiden och i vattenvärdesbudet 65 %, och förlorade 70–120 GWh om året. Nu
  kör de ett aggregat på minsta last en del av dygnet, som Fortum gör. Dygnets sista timmar
  körs alltid, eftersom vattnet därifrån når Åsen först nästa dygn. Tumreglerna blev 4–5 M€
  bättre, och vinsten mot dem sjönk från 17,7 till 13,8 M€.
