using System.Globalization;
using MijnGevoel.Core.Data;

namespace __MOODPRESS_NAMESPACE__;

public sealed partial class FeaturePage
{
    Guid? _editingAdultGoalId;
    static readonly (FeatureKind Kind, string Label, string Group)[] FeatureVisibilityOptions =
        FeatureCatalog.All.Select(x => (Kind: x.Kind, Label: x.Label, Group: x.Group)).ToArray();
    // Kept as a shared partial so Windows and iOS receive the Android 2.5.2
    // wellbeing feature without replacing their platform-specific page code.
    void BuildWellbeing()
    {
        Header("Mijn welzijn", "Houd alleen bij wat jij zelf wilt: slaap, beweging, energie of een korte notitie. Dit is jouw eigen terugblik, geen medische meting.");

        var includeSleep = new Switch { IsToggled = false, OnColor = AppUi.Accent };
        var sleep = new Slider { Minimum = 1, Maximum = 5, Value = 3, IsEnabled = false };
        var sleepLabel = AppUi.Label("Slaapkwaliteit overslaan");
        includeSleep.Toggled += (_, e) => { sleep.IsEnabled = e.Value; sleepLabel.Text = e.Value ? $"Slaapkwaliteit: {(int)Math.Round(sleep.Value)} van 5" : "Slaapkwaliteit overslaan"; };
        sleep.ValueChanged += (_, e) => sleepLabel.Text = $"Slaapkwaliteit: {(int)Math.Round(e.NewValue)} van 5";

        var includeMovement = new Switch { IsToggled = false, OnColor = AppUi.Accent };
        var movement = AppUi.Entry("Beweging in minuten (0 t/m 1440)", keyboard: Keyboard.Numeric);
        movement.IsEnabled = false;
        includeMovement.Toggled += (_, e) => { movement.IsEnabled = e.Value; if (!e.Value) movement.Text = ""; };

        var includeEnergy = new Switch { IsToggled = false, OnColor = AppUi.Accent };
        var energy = new Slider { Minimum = 1, Maximum = 5, Value = 3, IsEnabled = false };
        var energyLabel = AppUi.Label("Energie overslaan");
        includeEnergy.Toggled += (_, e) => { energy.IsEnabled = e.Value; energyLabel.Text = e.Value ? $"Energie: {(int)Math.Round(energy.Value)} van 5" : "Energie overslaan"; };
        energy.ValueChanged += (_, e) => energyLabel.Text = $"Energie: {(int)Math.Round(e.NewValue)} van 5";

        var note = AppUi.Editor("Korte welzijnsnotitie (optioneel)", height: 90);
        note.MaxLength = 1000;
        var status = AppUi.Label("");
        Guid? editingId = null;
        Action? saveAction = null;
        var saveButton = AppUi.Button("💾 Welzijn opslaan", () => saveAction?.Invoke());
        var history = new VerticalStackLayout { Spacing = 8 };

        void ResetForm()
        {
            editingId = null;
            includeSleep.IsToggled = false; sleep.Value = 3;
            includeMovement.IsToggled = false; movement.Text = "";
            includeEnergy.IsToggled = false; energy.Value = 3;
            note.Text = ""; saveButton.Text = "💾 Welzijn opslaan";
            status.Text = "Nieuwe registratie.";
        }

        void LoadEntry(WellbeingEntry entry)
        {
            editingId = entry.Id;
            includeSleep.IsToggled = entry.SleepQuality > 0;
            sleep.Value = Math.Clamp(entry.SleepQuality > 0 ? entry.SleepQuality : 3, 1, 5);
            includeMovement.IsToggled = entry.MovementMinutes >= 0;
            movement.Text = entry.MovementMinutes >= 0 ? entry.MovementMinutes.ToString(CultureInfo.InvariantCulture) : "";
            includeEnergy.IsToggled = entry.EnergyLevel > 0;
            energy.Value = Math.Clamp(entry.EnergyLevel > 0 ? entry.EnergyLevel : 3, 1, 5);
            note.Text = entry.Note;
            saveButton.Text = "💾 Wijzigingen opslaan";
            status.Text = $"Je past de registratie van {entry.Created:dd-MM-yyyy HH:mm} aan.";
        }

        void ReloadList()
        {
            history.Children.Clear();
            var items = Store.All<WellbeingEntry>("wellbeing").OrderByDescending(x => x.Created).Take(30).ToList();
            if (items.Count == 0)
            {
                history.Children.Add(AppUi.Label("Er zijn nog geen welzijnsregistraties. Je hoeft niets bij te houden als je dat niet wilt.", muted: true));
                return;
            }
            foreach (var item in items)
            {
                var local = item;
                var summary = new List<string>();
                if (local.SleepQuality > 0) summary.Add($"slaapkwaliteit {local.SleepQuality}/5");
                if (local.MovementMinutes >= 0) summary.Add($"beweging {local.MovementMinutes} min");
                if (local.EnergyLevel > 0) summary.Add($"energie {local.EnergyLevel}/5");
                if (!string.IsNullOrWhiteSpace(local.Note)) summary.Add(local.Note);
                var card = new VerticalStackLayout
                {
                    Spacing = 4,
                    Children =
                    {
                        AppUi.Label(local.Created.ToString("dd-MM-yyyy HH:mm", CultureInfo.GetCultureInfo("nl-NL")), title: true),
                        AppUi.Label(string.Join(" · ", summary))
                    }
                };
                card.Children.Add(AppUi.Button("✏️ Aanpassen", () => LoadEntry(local), quiet: true));
                card.Children.Add(AppUi.AsyncButton("🗑 Verwijderen", async () =>
                {
                    if (!await DisplayAlertAsync("Registratie verwijderen?", "Deze welzijnsnotitie wordt definitief verwijderd.", "Verwijderen", "Annuleren")) return;
                    Store.Delete("wellbeing", local.Id);
                    if (editingId == local.Id) ResetForm();
                    AppSession.Touch();
                    ReloadList();
                }, quiet: true));
                history.Children.Add(AppUi.Card(card));
            }
        }

        void SaveWellbeing()
        {
            var movementValue = -1;
            if (includeMovement.IsToggled &&
                (!int.TryParse(movement.Text, NumberStyles.Integer, CultureInfo.InvariantCulture, out movementValue) || movementValue is < 0 or > 1440))
            {
                status.Text = "Vul beweging in minuten in, van 0 tot en met 1440.";
                return;
            }
            try
            {
                var old = editingId is Guid id ? Store.Load<WellbeingEntry>("wellbeing", id) : null;
                if (editingId is not null && old is null)
                {
                    status.Text = "Deze registratie bestaat niet meer. Kies een andere of begin opnieuw.";
                    ResetForm();
                    return;
                }
                var updating = editingId is not null;
                var entry = WellbeingEntryService.Create(editingId ?? Guid.NewGuid(), old?.Created ?? DateTime.Now,
                    includeSleep.IsToggled, (int)Math.Round(sleep.Value), includeMovement.IsToggled, movementValue,
                    includeEnergy.IsToggled, (int)Math.Round(energy.Value), note.Text);
                Store.Save("wellbeing", entry.Id, entry);
                AppSession.Touch();
                ResetForm();
                status.Text = updating ? "Wijzigingen opgeslagen." : "Welzijnsregistratie opgeslagen.";
                ReloadList();
            }
            catch (ArgumentException ex) { status.Text = ex.Message; }
        }

        saveAction = SaveWellbeing;
        AddLabel("Slaapkwaliteit (1 = slecht uitgerust, 5 = goed uitgerust)", title: true);
        Add(new HorizontalStackLayout { Spacing = 8, Children = { includeSleep, AppUi.Label("Slaap bijhouden") } });
        Add(sleep); Add(sleepLabel);
        Add(new HorizontalStackLayout { Spacing = 8, Children = { includeMovement, AppUi.Label("Beweging bijhouden") } });
        AddField("Beweging (minuten)", movement);
        Add(new HorizontalStackLayout { Spacing = 8, Children = { includeEnergy, AppUi.Label("Energie bijhouden") } });
        Add(energy); Add(energyLabel);
        AddField("Notitie", note); Add(status); Add(saveButton);
        AddButton("Leeg formulier", ResetForm, quiet: true);
        AddLabel("Mijn laatste registraties", title: true); Add(history); ReloadList();
    }

    void SaveRoutineRunProgress(Routine routine, int step, bool completed = false)
    {
        if (routine is null || routine.Id == Guid.Empty || routine.Steps is null) return;
        var progress = completed
            ? RoutineRunProgressService.Complete(routine.Id, step, routine.Steps.Length, DateTime.Now)
            : RoutineRunProgressService.AtStep(routine.Id, step, routine.Steps.Length, DateTime.Now);
        Store.Save("routinerunprogress", routine.Id, progress);
    }

    void BuildTimerParity()
    {
        _timer?.Stop();
        _timer = null;
        Header("Timer", "Je timer bewaart de eindtijd, zodat je hem kunt hervatten na het wisselen van scherm of het opnieuw openen van MoodPress.");

        TimerSessionState state = Store.Load<TimerSessionState>("timer", Guid.Empty)
            ?? TimerSessionService.CreateIdle(300);
        var loadedState = state;
        var initial = TimerSessionService.Reconcile(state, DateTime.Now);
        state = initial.State;
        if (loadedState != state) Store.Save("timer", Guid.Empty, state);

        var options = new[] { "1", "2", "3", "5", "10", "15", "20", "30" };
        var selected = options.Contains((state.TotalSeconds / 60).ToString(CultureInfo.InvariantCulture))
            ? (state.TotalSeconds / 60).ToString(CultureInfo.InvariantCulture) : "5";
        var minutes = AppUi.Picker("Hoeveel minuten?", options, selected);
        var display = AppUi.Title($"{state.RemainingSeconds / 60:00}:{state.RemainingSeconds % 60:00}");
        display.HorizontalTextAlignment = TextAlignment.Center;
        display.FontSize = AppUi.TitleFont * 1.6;
        var status = AppUi.Label("Kies de tijd en druk op start.");

        void Render()
        {
            var remaining = Math.Clamp(state.RemainingSeconds, 0, Math.Max(1, state.TotalSeconds));
            display.Text = $"{remaining / 60:00}:{remaining % 60:00}";
        }

        void ShowCompleted(bool speak)
        {
            _timer?.Stop();
            Store.Save("timer", Guid.Empty, state);
            status.Text = "⏰ De tijd is om!";
            if (speak) AppUi.Speak("De tijd is om.");
        }

        void EnsureTicker()
        {
            if (_timer is null)
            {
                _timer = Dispatcher.CreateTimer();
                _timer.Interval = TimeSpan.FromSeconds(1);
                _timer.Tick += (_, _) =>
                {
                    var result = TimerSessionService.Reconcile(state, DateTime.Now);
                    state = result.State;
                    Render();
                    if (result.CompletedNow) ShowCompleted(speak: true);
                    else if (!state.IsRunning) _timer?.Stop();
                };
            }
            if (!_timer.IsRunning) _timer.Start();
        }

        AddField("Tijd", minutes);
        Add(display);
        Add(status);
        AddLabel("De eindtijd blijft lokaal bewaard. Op Windows en iOS wordt in deze versie geen timerwaarschuwing beloofd wanneer de app volledig gesloten is.", muted: true);

        AddButton("▶ Start / pauze", () =>
        {
            var now = DateTime.Now;
            var reconciled = TimerSessionService.Reconcile(state, now);
            state = reconciled.State;
            if (reconciled.CompletedNow)
            {
                ShowCompleted(speak: true);
                Render();
                return;
            }

            if (state.IsRunning)
            {
                state = TimerSessionService.Pause(state, now);
                _timer?.Stop();
                Store.Save("timer", Guid.Empty, state);
                Render();
                status.Text = "Gepauzeerd.";
                return;
            }

            var selectedSeconds = int.Parse(minutes.SelectedItem?.ToString() ?? "5", CultureInfo.InvariantCulture) * 60;
            state = TimerSessionService.Start(state, selectedSeconds, now);
            Store.Save("timer", Guid.Empty, state);
            Render();
            status.Text = "De timer loopt. De resterende tijd wordt bijgewerkt zolang MoodPress open is.";
            EnsureTicker();
        });
        AddButton("↻ Opnieuw", () =>
        {
            var selectedSeconds = int.Parse(minutes.SelectedItem?.ToString() ?? "5", CultureInfo.InvariantCulture) * 60;
            _timer?.Stop();
            state = TimerSessionService.Reset(selectedSeconds);
            Store.Save("timer", Guid.Empty, state);
            Render();
            status.Text = "Klaar om te starten.";
        }, quiet: true);

        Render();
        if (initial.CompletedNow)
        {
            ShowCompleted(speak: true);
        }
        else if (state.IsRunning)
        {
            status.Text = "De timer loopt. Je kunt naar een ander scherm gaan; de eindtijd blijft bewaard.";
            EnsureTicker();
        }
        else if (state.CompletedAt is not null)
        {
            status.Text = "⏰ De tijd is om!";
        }
        else if (state.RemainingSeconds < state.TotalSeconds)
        {
            status.Text = "Gepauzeerd.";
        }
    }
    void ApplyPlatformScreenPrivacy(bool enabled)
    {
#if ANDROID
        AndroidScreenPrivacy.Apply(enabled);
#endif
    }
}
