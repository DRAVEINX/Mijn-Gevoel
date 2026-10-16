# MoodPress 4.1 — cloudsync en ondersteunersportal

## Verplichte scope
1. Automatische, versiegestuurde cloudsynchronisatie tussen ingelogde MoodPress-apparaten. De server bewaart alleen het bestaande back-uparchief en controleert hashes/versies; hij mag het archief niet uitpakken of ontsleutelen.
2. Een online portal met aparte accounts en rollen voor begeleiders en ouders/voogden. De eigenaar beheert uitnodigingen, categorie-toestemmingen en intrekt toegang; ondersteuners kunnen alleen toegestane gedeelde items lezen en versleutelde voorstellen indienen. De eigenaar kan voorstellen accepteren of afwijzen.
3. Dezelfde gebruikersmogelijkheden op Android-telefoon, Android-tablet, Windows en iOS. De actuele groene 4.0-release is Android-only. Deze branch is de werkbasis voor cloud/portal-integratie en platformpariteit; de definitieve vier-platform releases mogen pas worden vrijgegeven als iedere platform-build en apparaat-test geslaagd is.

## Privacy- en beveiligingskeuzes
- Cloudsync gebruikt compare-and-swap: een oude versie kan nooit stil de nieuwe server-snapshot overschrijven.
- Het cloudarchief is een opaque snapshot met lokaal versleutelde account-/kluisbestanden. De cloudserver voert geen decryptie uit.
- De portal gebruikt AES-GCM in de browser voor gedeelde tekst; de server krijgt alleen IV en ciphertext. De deel-sleutel wordt nooit door de server bewaard. Deel sleutels buiten het portaal via een ander vertrouwd kanaal.
- Een ingetrokken lid verliest servertoegang, maar een eerder gedownload bestand of eerder bekende deelsleutel kan niet op afstand uit diens bezit worden gewist.
- Een gedeeld item is alleen zichtbaar voor leden met de bijbehorende categorie in hun rechtenlijst. Ondersteunersvoorstellen blijven standaard 'pending' totdat de eigenaar ze beoordeelt.
- Het huidige prototype verzendt geen uitnodigingsmails en verifieert e-mailadressen nog niet. Het mag daarom niet als openbare zorgdienst worden uitgerold voordat e-mailverificatie, wachtwoordherstel, monitoring, databaseback-ups, retentie/verwijdering en een privacy-/DPIA-review zijn afgerond.

## Integratie- en release-gates
- Android: client moet een cloudprofiel kunnen instellen, token in SecureStorage bewaren, encrypted snapshot/hash/version ophalen of uploaden en conflict veilig afhandelen.
- Windows en iOS: zelfde contract en dezelfde conflictregels; tokens moeten in platform veilige opslag staan.
- Portal: accountregistratie, inloggen, rol-/categorie-autorisatie, versleutelde items, suggesties, intrekking en logout testen.
- Server: CI smoke tests voor auth, checksum, stale-write conflict, toegangsgrenzen, share-encryptie, suggestie-workflow en intrekking.
- Geen APK/AAB/MSI/ondertekende iOS-release als 'definitief' markeren voordat build én relevante apparaat-/upgradetests groen zijn.
