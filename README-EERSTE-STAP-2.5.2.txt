MOODPRESS 2.5.2 — HERSTEL “WIE WERKT ER?”

1. Upload MoodPress-Android-compleet-2.5.2.zip naar de hoofdmap van de GitHub-repository.
2. Upload MoodPress-Android-Tablet-2.5.2.zip naar dezelfde hoofdmap.
3. Upload de vier workflowbestanden in .github/workflows/ naar dezelfde map in GitHub. Laat de oudere workflows staan.
4. Start eerst de telefoon-APK-testbuild via android-apk-2.5.2.yml en daarna de tablet-APK-testbuild.
5. Installeer test-APK's op echte apparaten en controleer Home, Meer, Wie werkt er?, foto's en weekrooster.
6. Start de releaseworkflows pas nadat beide testbuilds groen zijn en de apparaattests slagen.

De huidige stabiele 2.5.0-bestanden blijven apart behouden. De build is met deze broncodewijziging nog niet bewezen totdat GitHub Actions slaagt.
