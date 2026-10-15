# MoodPress 2.5.2 — herstel van “Wie werkt er?”

## Wat er misging

De functie voor begeleiders en het weekrooster zat nog in de broncode, maar kon voor bestaande gebruikers uit de lijst Meer functies verdwijnen. De lijst met zichtbare functies werd gefilterd op opgeslagen instellingen. Op Home was de oude directe ingang in de nieuwe indeling eveneens niet meer standaard zichtbaar.

## Herstel in 2.5.2

- De functie **Wie werkt er?** blijft vindbaar in Meer, ook als oude opgeslagen zichtbaarheid-instellingen deze functie niet noemen.
- Home toont weer een kaart **Wie werkt er vandaag?** met de namen en beschikbare foto's van de personen die voor vandaag in het lokaal opgeslagen weekrooster zijn aangevinkt.
- De kaart heeft een directe knop om begeleiders en het weekrooster te beheren.
- De functie is onder **Communicatie en steun** ingedeeld.
- De instelling voor zichtbare functies toont deze functie als ingeschakeld en niet uitschakelbaar, zodat hij niet per ongeluk verdwijnt.
- Naam, foto en weektoewijzingen blijven in de bestaande versleutelde lokale kluis; er wordt geen server of automatische koppeling met een zorgorganisatie toegevoegd.

## Zo werkt het weekrooster

Open **Wie werkt er?**, voeg een begeleider toe, vul de naam in, kies optioneel een foto en vink de dagen aan waarop die persoon werkt. Gebruik Vorige/Volgende om per kalenderweek het rooster te wijzigen. De bezetting in de Home-kaart volgt de week en de dag die op het toestel zijn ingesteld.

Foto's van echte personen mogen alleen met toestemming van de persoon of bevoegde vertegenwoordiger worden toegevoegd.

## Validatiestatus

De codewijziging en ZIP-integriteit zijn lokaal gecontroleerd. De echte .NET Core-tests, Android-build en praktijktest moeten daarna via GitHub Actions en op een toestel slagen; dit document noemt die controles niet vooraf geslaagd.
