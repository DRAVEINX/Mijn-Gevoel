using Microsoft.Maui.ApplicationModel;
using Microsoft.Maui.Controls;
using Microsoft.Maui.Storage;

namespace __MOODPRESS_NAMESPACE__;

public sealed class CloudSyncPage : ContentPage
{
    readonly Entry _endpoint = new() { Placeholder = "HTTPS-adres van de MoodPress-cloudserver", Keyboard = Keyboard.Url, ClearButtonVisibility = ClearButtonVisibility.WhileEditing };
    readonly Entry _email = new() { Placeholder = "Portal-e-mailadres", Keyboard = Keyboard.Email, ClearButtonVisibility = ClearButtonVisibility.WhileEditing };
    readonly Entry _displayName = new() { Placeholder = "Naam (alleen bij account maken)", ClearButtonVisibility = ClearButtonVisibility.WhileEditing };
    readonly Entry _password = new() { Placeholder = "Portalwachtwoord (minimaal 12 tekens)", IsPassword = true, ClearButtonVisibility = ClearButtonVisibility.WhileEditing };
    readonly Label _status = new() { Text = "Nog niet gekoppeld.", LineBreakMode = LineBreakMode.WordWrap };
    readonly Switch _autoSync = new() { IsToggled = CloudSyncClient.AutoSyncEnabled };

    public CloudSyncPage()
    {
        Title = "Cloudsync tussen apparaten";
        BackgroundColor = Application.Current?.RequestedTheme == AppTheme.Dark ? Color.FromArgb("#101827") : Color.FromArgb("#F5F7FB");
        _status.AutomationId = "cloud-sync-status";
        _autoSync.AutomationId = "cloud-sync-auto-toggle";

        var title = new Label { Text = "MoodPress Cloudsync", FontSize = 25, FontAttributes = FontAttributes.Bold, LineBreakMode = LineBreakMode.WordWrap };
        var intro = new Label
        {
            Text = "Je telefoon en tablet gebruiken dezelfde beveiligde werkruimte. MoodPress verstuurt een versleuteld back-uparchief, controleert de versie en meldt conflicten in plaats van gegevens stil te overschrijven.",
            LineBreakMode = LineBreakMode.WordWrap
        };
        var autoRow = new HorizontalStackLayout { Spacing = 12, Children = { _autoSync, new Label { Text = "Automatisch synchroniseren wanneer MoodPress actief is", VerticalTextAlignment = TextAlignment.Center, LineBreakMode = LineBreakMode.WordWrap, HorizontalOptions = LayoutOptions.Fill } } };
        _autoSync.Toggled += (_, e) => CloudSyncClient.AutoSyncEnabled = e.Value;

        var connect = new Button { Text = "Inloggen op cloudaccount", AutomationId = "cloud-sync-login" };
        connect.Clicked += async (_, _) => await ConnectAsync(register: false);
        var register = new Button { Text = "Nieuw cloudaccount maken", AutomationId = "cloud-sync-register" };
        register.Clicked += async (_, _) => await ConnectAsync(register: true);
        var sync = new Button { Text = "Nu synchroniseren", AutomationId = "cloud-sync-now" };
        sync.Clicked += async (_, _) => await SyncAsync();
        var disconnect = new Button { Text = "Cloudaccount ontkoppelen", AutomationId = "cloud-sync-disconnect" };
        disconnect.Clicked += async (_, _) => await DisconnectAsync();
        var portal = new Button { Text = "Online portal openen", AutomationId = "cloud-sync-open-portal" };
        portal.Clicked += async (_, _) =>
        {
            try
            {
                var address = (_endpoint.Text ?? await CloudSyncClient.LoadEndpointAsync()).Trim().TrimEnd('/');
                if (string.IsNullOrWhiteSpace(address)) throw new InvalidOperationException("Vul eerst het HTTPS-adres van de MoodPress-cloudserver in.");
                await Browser.Default.OpenAsync(address);
            }
            catch (Exception ex) { SetStatus(ex.Message, true); }
        };

        var content = new VerticalStackLayout
        {
            Padding = new Thickness(20),
            Spacing = 13,
            Children =
            {
                title, intro,
                new Label { Text = "Cloudserver", FontAttributes = FontAttributes.Bold }, _endpoint,
                new Label { Text = "Portal-e-mailadres", FontAttributes = FontAttributes.Bold }, _email,
                new Label { Text = "Naam voor nieuw account", FontAttributes = FontAttributes.Bold }, _displayName,
                new Label { Text = "Portalwachtwoord", FontAttributes = FontAttributes.Bold }, _password,
                autoRow, _status, connect, register, sync, portal, disconnect,
                new Label { Text = "Privacy: de synchronisatieserver controleert de kluis-ID, hash en versie. Persoonlijke records blijven lokaal versleuteld. Bij conflicten wordt niets automatisch overschreven.", FontSize = 13, LineBreakMode = LineBreakMode.WordWrap }
            }
        };
        Content = new ScrollView { Content = content };
        _ = LoadSavedAsync();
    }

    async Task ConnectAsync(bool register)
    {
        try
        {
            SetStatus("Bezig met veilig verbinden…");
            await CloudSyncClient.AuthenticateAsync(_endpoint.Text ?? "", _email.Text ?? "", _password.Text ?? "", register, _displayName.Text ?? "");
            _password.Text = "";
            SetStatus(register ? "Cloudaccount gemaakt en gekoppeld." : "Ingelogd op je MoodPress-cloudaccount.");
        }
        catch (Exception ex) { SetStatus(ex.Message, true); }
    }

    async Task SyncAsync()
    {
        try
        {
            SetStatus("Cloudsnapshot controleren en versleutelde wijzigingen vergelijken…");
            var result = await CloudSyncClient.SyncNowAsync();
            SetStatus($"{result.Message}\nVersie: {result.Version}\nGewijzigde bestanden toegepast: {result.AppliedRecords}");
        }
        catch (CloudSyncConflictException ex) { SetStatus(ex.Message, true); }
        catch (Exception ex) { SetStatus(ex.Message, true); }
    }

    async Task DisconnectAsync()
    {
        try
        {
            await CloudSyncClient.DisconnectAsync();
            _password.Text = "";
            SetStatus("Cloudaccount ontkoppeld op dit apparaat. Je lokale MoodPress-gegevens blijven behouden.");
        }
        catch (Exception ex) { SetStatus(ex.Message, true); }
    }

    async Task LoadSavedAsync()
    {
        try
        {
            var endpoint = await CloudSyncClient.LoadEndpointAsync();
            var email = await CloudSyncClient.LoadEmailHintAsync();
            MainThread.BeginInvokeOnMainThread(() =>
            {
                _endpoint.Text = endpoint;
                _email.Text = email;
                _autoSync.IsToggled = CloudSyncClient.AutoSyncEnabled;
            });
            if (await CloudSyncClient.HasSessionAsync())
                MainThread.BeginInvokeOnMainThread(() => SetStatus("Cloudaccount is gekoppeld. Tik op 'Nu synchroniseren' om direct te controleren."));
        }
        catch (Exception ex) { AppLog.Write(ex); }
    }

    void SetStatus(string value, bool error = false)
    {
        MainThread.BeginInvokeOnMainThread(() =>
        {
            _status.Text = value;
            _status.TextColor = error ? Colors.IndianRed : (Application.Current?.RequestedTheme == AppTheme.Dark ? Colors.White : Colors.Black);
        });
    }
}
