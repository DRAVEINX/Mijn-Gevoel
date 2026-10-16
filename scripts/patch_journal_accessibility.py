#!/usr/bin/env python3
"""Port the accessible encrypted journal draft and speech-to-text controls to desktop/iOS builds."""
from __future__ import annotations

import re
from pathlib import Path


def patch_journal_draft_and_dictation(app_dir: Path, target: str) -> None:
    app_project = app_dir / ("MoodPress.WindowsApp.csproj" if target == "windows" else "MoodPress.iOS.csproj")
    if not app_project.is_file():
        raise FileNotFoundError(app_project)

    project = app_project.read_text(encoding="utf-8")
    toolkit = re.compile(r'<PackageReference\b(?=[^>]*\bInclude=["\']CommunityToolkit\.Maui["\'])[^>]*/>', re.I)
    refs = list(toolkit.finditer(project))
    if len(refs) > 1:
        raise RuntimeError(f"Multiple CommunityToolkit.Maui references in {app_project}")
    if refs:
        tag = refs[0].group(0)
        if not re.search(r'\bVersion=["\']15\.0\.1["\']', tag, re.I):
            replacement = re.sub(r'\bVersion=["\'][^"\']+["\']', 'Version="15.0.1"', tag, count=1, flags=re.I)
            if replacement == tag:
                replacement = tag[:-2] + ' Version="15.0.1" />'
            project = project[:refs[0].start()] + replacement + project[refs[0].end():]
    else:
        controls = re.search(r'(?m)^(?P<indent>[ \t]*)<PackageReference\s+Include="Microsoft\.Maui\.Controls"[^>]*/>', project)
        if not controls:
            raise RuntimeError(f"Microsoft.Maui.Controls reference missing from {app_project}")
        indent = controls.group("indent")
        package = controls.group(0) + "\n" + indent + '<PackageReference Include="CommunityToolkit.Maui" Version="15.0.1" />'
        project = project[:controls.start()] + package + project[controls.end():]
    if not re.search(r'<PackageReference\b(?=[^>]*\bInclude=["\']CommunityToolkit\.Maui["\'])(?=[^>]*\bVersion=["\']15\.0\.1["\'])[^>]*/>', project, re.I):
        raise RuntimeError(f"CommunityToolkit.Maui 15.0.1 could not be confirmed in {app_project}")
    app_project.write_text(project, encoding="utf-8")

    program = app_dir / "MauiProgram.cs"
    program_text = program.read_text(encoding="utf-8")
    if "UseMauiCommunityToolkit()" not in program_text:
        audio_using = "using Plugin.Maui.Audio;"
        if "using CommunityToolkit.Maui;" not in program_text:
            if program_text.count(audio_using) != 1:
                raise RuntimeError(f"Could not locate audio using directive in {program}")
            program_text = program_text.replace(audio_using, audio_using + "\nusing CommunityToolkit.Maui;", 1)
        if program_text.count("builder.UseMauiApp<App>();") != 1:
            raise RuntimeError(f"Could not locate MAUI builder initialization in {program}")
        program_text = program_text.replace(
            "builder.UseMauiApp<App>();",
            "builder.UseMauiApp<App>().UseMauiCommunityToolkit();",
            1,
        )
    program.write_text(program_text, encoding="utf-8")
    if "UseMauiCommunityToolkit()" not in program_text:
        raise RuntimeError(f"Community Toolkit initialization was not applied in {program}")

    if target == "ios":
        plist = app_dir / "Platforms" / "iOS" / "Info.plist"
        plist_text = plist.read_text(encoding="utf-8")
        if "<key>NSSpeechRecognitionUsageDescription</key>" not in plist_text:
            marker = "    <key>NSMicrophoneUsageDescription</key>"
            if plist_text.count(marker) != 1:
                raise RuntimeError(f"Could not locate microphone privacy text in {plist}")
            plist_text = plist_text.replace(
                marker,
                '    <key>NSSpeechRecognitionUsageDescription</key>\n'
                '    <string>MoodPress zet gesproken woorden alleen om naar tekst wanneer je zelf op inspreken tikt.</string>\n'
                + marker,
                1,
            )
        plist.write_text(plist_text, encoding="utf-8")
        if "<key>NSSpeechRecognitionUsageDescription</key>" not in plist_text:
            raise RuntimeError("iOS speech-recognition privacy description is missing.")
    else:
        manifest = app_dir / "Platforms" / "Windows" / "Package.appxmanifest"
        manifest_text = manifest.read_text(encoding="utf-8")
        if 'DeviceCapability Name="microphone"' not in manifest_text:
            marker = "</Capabilities>"
            if manifest_text.count(marker) != 1:
                raise RuntimeError(f"Could not locate Windows capabilities in {manifest}")
            manifest_text = manifest_text.replace(
                marker,
                '    <DeviceCapability Name="microphone" />\n  ' + marker,
                1,
            )
        manifest.write_text(manifest_text, encoding="utf-8")
        if 'DeviceCapability Name="microphone"' not in manifest_text:
            raise RuntimeError("Windows microphone capability is missing.")

    page = app_dir / "FeaturePage.cs"
    text = page.read_text(encoding="utf-8")
    if "using Microsoft.Maui.Devices;" not in text:
        marker_devices = "using Microsoft.Maui.ApplicationModel;"
        if text.count(marker_devices) != 1:
            raise RuntimeError(f"Could not locate MAUI application-model namespace in {page}")
        text = text.replace(marker_devices, marker_devices + "\nusing Microsoft.Maui.Devices;", 1)
    if "using CommunityToolkit.Maui.Media;" not in text:
        marker = "using Plugin.Maui.Audio;"
        if text.count(marker) != 1:
            raise RuntimeError(f"Could not locate audio namespace in {page}")
        text = text.replace(
            marker,
            marker + "\nusing CommunityToolkit.Maui.Media;\nusing Microsoft.Maui.Networking;",
            1,
        )
    elif "using Microsoft.Maui.Networking;" not in text:
        text = text.replace(
            "using CommunityToolkit.Maui.Media;",
            "using CommunityToolkit.Maui.Media;\nusing Microsoft.Maui.Networking;",
            1,
        )

    if "public sealed record JournalDraftEntry(" not in text:
        namespace = re.search(r"(?m)^namespace\s+[A-Za-z0-9_.]+;\s*$", text)
        if not namespace:
            raise RuntimeError(f"Could not locate file-scoped namespace in {page}")
        record = (
            "\n\n/// <summary>Versleuteld automatisch concept voor een dagboek- of notitiepagina.</summary>\n"
            "public sealed record JournalDraftEntry(DateTime Updated, int Mood, string Text, Guid? EntryId = null);"
        )
        text = text[:namespace.end()] + record + text[namespace.end():]

    if "ISpeechToText? _speechToTextService;" not in text:
        marker = "    static readonly (string Emoji, string Name, string Hex)[] Moods ="
        if text.count(marker) != 1:
            raise RuntimeError(f"Could not locate mood-model insertion point in {page}")
        fields = (
            "    IDispatcherTimer? _journalDraftTimer;\n"
            "    Action? _flushJournalDraft;\n"
            "    ISpeechToText? _speechToTextService;\n"
            "    Editor? _speechToTextEditor;\n"
            "    Label? _speechToTextStatus;\n"
            "    Button? _speechToTextButton;\n"
            "    string _speechTextPrefix = \"\";\n"
            "    bool _speechToTextListening;\n\n"
        )
        text = text.replace(marker, fields + marker, 1)

    if "if (_journalDraftTimer is not null && !_journalDraftTimer.IsRunning)" not in text:
        lifecycle = re.compile(
            r"(protected override void OnAppearing\(\)\s*\{\s*base\.OnAppearing\(\);\s*AppSession\.Touch\(\);)",
            re.S,
        )
        text, count = lifecycle.subn(
            r"\1\n        if (_journalDraftTimer is not null && !_journalDraftTimer.IsRunning)\n            _journalDraftTimer.Start();",
            text,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"Could not patch draft timer lifecycle in {page}")

    if "_flushJournalDraft?.Invoke()" not in text:
        disappearing = re.search(
            r"(protected override void OnDisappearing\(\)\s*\{.*?)(?P<marker>^[ \t]*_voiceRecordTimer\?\.Stop\(\);)",
            text,
            re.S | re.M,
        )
        if not disappearing:
            raise RuntimeError(f"Could not locate voice timer cleanup inside OnDisappearing in {page}")
        marker = disappearing.group("marker")
        extra = (
            marker
            + "\n        _journalDraftTimer?.Stop();"
            + "\n        try { _flushJournalDraft?.Invoke(); } catch (Exception ex) { AppLog.Write(ex); }"
            + "\n        if (_speechToTextListening) _ = StopSpeechToTextAsync();"
        )
        text = text[:disappearing.start("marker")] + extra + text[disappearing.end("marker"):]

    journal_anchor = '        var status = AppUi.Label("");\n        Guid? current = null;'
    if 'var draftKind = "journal-draft-" + kind;' not in text:
        if text.count(journal_anchor) != 1:
            raise RuntimeError(f"Could not locate journal draft insertion point in {page}")
        draft_ui = r'''        var status = AppUi.Label("");
        Guid? current = null;
        var draftStatus = AppUi.Label("Concept wordt lokaal bewaard zodra je iets schrijft.", muted: true);
        var draftKind = "journal-draft-" + kind;
        var savedDraft = Store.Load<JournalDraftEntry>(draftKind, Guid.Empty);
        if (savedDraft is not null)
        {
            text.Text = savedDraft.Text;
            if (withMood) mood.SelectedIndex = Math.Clamp(savedDraft.Mood, 0, 5);
            if (savedDraft.EntryId is Guid savedId && Store.Load<JournalEntry>(kind, savedId) is not null)
                current = savedId;
            draftStatus.Text = $"Onvoltooid concept teruggezet ({savedDraft.Updated:dd-MM-yyyy HH:mm}). Controleer het en tik op Bewaren als je het wilt opslaan.";
        }
        bool draftDirty = false;
        bool suppressDraftChanges = true;
        void FlushDraft()
        {
            if (!draftDirty || AppSession.Store is null) return;
            draftDirty = false;
            if (string.IsNullOrWhiteSpace(text.Text) && (!withMood || mood.SelectedIndex <= 0))
            {
                Store.Delete(draftKind, Guid.Empty);
                draftStatus.Text = "Leeg concept.";
                return;
            }
            Store.Save(draftKind, Guid.Empty, new JournalDraftEntry(DateTime.Now,
                withMood ? Math.Clamp(mood.SelectedIndex, 0, 5) : 0, text.Text ?? "", current));
            draftStatus.Text = $"Concept automatisch en versleuteld bewaard ({DateTime.Now:HH:mm}).";
        }
        _flushJournalDraft = FlushDraft;
        text.TextChanged += (_, _) => { if (!suppressDraftChanges) draftDirty = true; };
        mood.SelectedIndexChanged += (_, _) => { if (!suppressDraftChanges && withMood) draftDirty = true; };
        suppressDraftChanges = false;
        _journalDraftTimer?.Stop();
        _journalDraftTimer = Dispatcher.CreateTimer();
        _journalDraftTimer.Interval = TimeSpan.FromSeconds(1);
        _journalDraftTimer.Tick += (_, _) =>
        {
            try { FlushDraft(); }
            catch (Exception ex) { AppLog.Write(ex); draftStatus.Text = "Concept bewaren lukte niet. Gebruik Bewaren om je tekst op te slaan."; }
        };
        _journalDraftTimer.Start();
        AddLabel("Automatisch concept bewaren", title: true);
        Add(draftStatus);
        var speechStatus = AppUi.Label("Je kunt woorden inspreken en ze als tekst laten herkennen. Online herkenning wordt pas na jouw toestemming gestart.", muted: true);
        _speechToTextEditor = text;
        _speechToTextStatus = speechStatus;
        var speechButton = AppUi.AsyncButton("🗣️ Inspreken naar tekst", async () => await ToggleSpeechToTextAsync(), quiet: true);
        _speechToTextButton = speechButton;
        SemanticProperties.SetDescription(speechButton, "Spreek woorden uit om ze als tekst in het dagboek te plaatsen.");
        SemanticProperties.SetHint(speechButton, "Controleer de herkende tekst voordat je op Bewaren tikt.");
        Add(speechButton);
        Add(speechStatus);'''
        text = text.replace(journal_anchor, draft_ui, 1)

    save_anchor = 'Store.Save(kind, entry.Id, entry); current = entry.Id; status.Text = "Opgeslagen ✔"; ReloadList(list);'
    if save_anchor not in text and 'Store.Delete(draftKind, Guid.Empty); draftDirty = false; current = entry.Id;' not in text:
        raise RuntimeError(f"Could not locate journal save handler in {page}")
    if save_anchor in text:
        text = text.replace(
            save_anchor,
            'Store.Save(kind, entry.Id, entry); Store.Delete(draftKind, Guid.Empty); draftDirty = false; current = entry.Id; status.Text = "Opgeslagen ✔"; draftStatus.Text = "Je dagboekstuk is opgeslagen. Het concept is opgeruimd."; ReloadList(list);',
            1,
        )

    old_new = 'AddButton("✨ Nieuw", () => { current = null; text.Text = ""; if (withMood) mood.SelectedIndex = 0; status.Text = ""; }, quiet: true);'
    new_new = r'''AddAsyncButton("✨ Nieuw", async () =>
        {
            if (!string.IsNullOrWhiteSpace(text.Text) && !await DisplayAlertAsync("Nieuw stukje?", "De tekst op dit scherm wordt vervangen. Bewaar hem eerst als je hem wilt houden.", "Nieuw", "Annuleren")) return;
            suppressDraftChanges = true;
            current = null; text.Text = ""; if (withMood) mood.SelectedIndex = 0; status.Text = "";
            suppressDraftChanges = false;
            draftDirty = false;
            Store.Delete(draftKind, Guid.Empty);
            draftStatus.Text = "Nieuw leeg concept.";
            await Task.CompletedTask;
        }, quiet: true);'''
    if old_new in text:
        text = text.replace(old_new, new_new, 1)
    elif "Nieuw leeg concept." not in text:
        raise RuntimeError(f"Could not locate journal new-entry action in {page}")

    delete_anchor = 'Store.Delete(kind, id); current = null; text.Text = ""; if (withMood) mood.SelectedIndex = 0; status.Text = "Verwijderd."; ReloadList(list);'
    if delete_anchor in text:
        text = text.replace(
            delete_anchor,
            'Store.Delete(kind, id); Store.Delete(draftKind, Guid.Empty); draftDirty = false; current = null; suppressDraftChanges = true; text.Text = ""; if (withMood) mood.SelectedIndex = 0; suppressDraftChanges = false; status.Text = "Verwijderd."; draftStatus.Text = "Verwijderd. Er staat geen concept meer klaar."; ReloadList(list);',
            1,
        )
    elif "draftStatus.Text = \"Verwijderd. Er staat geen concept meer klaar.\"" not in text:
        raise RuntimeError(f"Could not locate journal delete action in {page}")

    speech_anchor = "    async Task StartVoiceRecordingAsync()"
    if "    async Task ToggleSpeechToTextAsync()" not in text:
        if text.count(speech_anchor) != 1:
            raise RuntimeError(f"Could not locate voice recording method in {page}")
        methods = r'''    async Task ToggleSpeechToTextAsync()
    {
        if (_speechToTextListening)
        {
            await StopSpeechToTextAsync();
            return;
        }
        if (_speechToTextEditor is null || _speechToTextStatus is null) return;

        try
        {
#if ANDROID || IOS
            if (DeviceInfo.Version.Major >= 13)
            {
                _speechToTextStatus.Text = "Offline spraakherkenning wordt gecontroleerd…";
                if (await TryStartSpeechToTextAsync(OfflineSpeechToText.Default)) return;
                var onlineAllowed = await DisplayAlertAsync(
                    "Offline inspreken niet beschikbaar",
                    "Offline herkenning werkt op dit toestel niet of is niet ingesteld. Online herkenning kan gesproken woorden naar een herkenningsdienst sturen. Wil je daarvoor bewust toestemming geven?",
                    "Online proberen", "Annuleren");
                if (!onlineAllowed)
                {
                    _speechToTextStatus.Text = "Niet gestart. Je kunt typen of een audio-opname bewaren.";
                    return;
                }
            }
            else
#endif
            {
                var onlineAllowed = await DisplayAlertAsync(
                    "Inspreken naar tekst",
                    "Online spraakherkenning kan gesproken woorden naar een herkenningsdienst sturen. Gebruik liever geen informatie die je niet wilt delen. Alleen doorgaan als je daarmee akkoord gaat.",
                    "Toestaan en starten", "Annuleren");
                if (!onlineAllowed)
                {
                    _speechToTextStatus.Text = "Niet gestart. Je kunt typen of een audio-opname bewaren.";
                    return;
                }
            }

            if (Connectivity.Current.NetworkAccess != NetworkAccess.Internet)
            {
                _speechToTextStatus.Text = "Online herkenning vraagt een internetverbinding. Je kunt wel typen of audio opnemen.";
                return;
            }
            if (!await TryStartSpeechToTextAsync(SpeechToText.Default))
                _speechToTextStatus.Text = "Inspreken niet beschikbaar. Je kunt typen of een audio-opname bewaren.";
        }
        catch (Exception ex)
        {
            AppLog.Write(ex);
            _speechToTextStatus.Text = "Spraakherkenning kon niet starten. Je kunt typen of een audio-opname bewaren.";
            CleanupSpeechToText(_speechToTextService);
        }
    }

    async Task<bool> TryStartSpeechToTextAsync(ISpeechToText service)
    {
        if (_speechToTextEditor is null || _speechToTextStatus is null) return false;
        var microphone = await Permissions.RequestAsync<Permissions.Microphone>();
        if (microphone != PermissionStatus.Granted)
        {
            _speechToTextStatus.Text = "Microfoontoegang is niet toegestaan. Je kunt typen of een audio-opname bewaren.";
            return false;
        }
        if (!await service.RequestPermissions(CancellationToken.None))
        {
            _speechToTextStatus.Text = "Spraakherkenning heeft geen toestemming. Je kunt typen of een audio-opname bewaren.";
            return false;
        }

        try
        {
            _speechToTextService = service;
            _speechTextPrefix = (_speechToTextEditor.Text ?? "").Trim();
            service.RecognitionResultUpdated += OnSpeechToTextUpdated;
            service.RecognitionResultCompleted += OnSpeechToTextCompleted;
            _speechToTextListening = true;
            if (_speechToTextButton is not null) _speechToTextButton.Text = "⏹ Stop inspreken";
            _speechToTextStatus.Text = "Ik luister. Spreek rustig Nederlands; na stilte stopt de herkenning automatisch. Controleer de tekst voordat je hem bewaart.";
            await service.StartListenAsync(new SpeechToTextOptions
            {
                Culture = CultureInfo.GetCultureInfo("nl-NL"),
                ShouldReportPartialResults = true,
                AutoStopSilenceTimeout = TimeSpan.FromSeconds(5)
            }, CancellationToken.None);
            if (!ReferenceEquals(_speechToTextService, service)) return true;
            AppSession.Touch();
            return true;
        }
        catch (Exception ex)
        {
            AppLog.Write(ex);
            CleanupSpeechToText(service);
            _speechToTextStatus.Text = "Deze herkenningsmethode is niet beschikbaar.";
            return false;
        }
    }

    void OnSpeechToTextUpdated(object? sender, SpeechToTextRecognitionResultUpdatedEventArgs args)
    {
        var partial = args.RecognitionResult?.Trim() ?? "";
        MainThread.BeginInvokeOnMainThread(() =>
        {
            if (_speechToTextEditor is null || partial.Length == 0) return;
            _speechToTextEditor.Text = JoinRecognizedText(_speechTextPrefix, partial);
            AppSession.Touch();
        });
    }

    void OnSpeechToTextCompleted(object? sender, SpeechToTextRecognitionResultCompletedEventArgs args)
    {
        var result = args.RecognitionResult;
        var service = _speechToTextService;
        MainThread.BeginInvokeOnMainThread(() =>
        {
            if (_speechToTextEditor is not null && result.IsSuccessful && !string.IsNullOrWhiteSpace(result.Text))
                _speechToTextEditor.Text = JoinRecognizedText(_speechTextPrefix, result.Text.Trim());
            if (_speechToTextStatus is not null)
                _speechToTextStatus.Text = result.IsSuccessful
                    ? "Inspreken klaar. Controleer de tekst en tik op Bewaren; pas daarna staat hij in je dagboek."
                    : "Herkenning lukte niet. Je kunt de tekst zelf typen of een audio-opname bewaren.";
            try { _flushJournalDraft?.Invoke(); } catch (Exception ex) { AppLog.Write(ex); }
            CleanupSpeechToText(service);
        });
    }

    static string JoinRecognizedText(string prefix, string spoken)
        => string.IsNullOrWhiteSpace(prefix) ? spoken : prefix + Environment.NewLine + spoken;

    async Task StopSpeechToTextAsync()
    {
        var service = _speechToTextService;
        if (service is null) return;
        try { await service.StopListenAsync(CancellationToken.None); }
        catch (Exception ex) { AppLog.Write(ex); }
        finally
        {
            CleanupSpeechToText(service);
            if (_speechToTextStatus is not null && _speechToTextStatus.Text.StartsWith("Ik luister", StringComparison.Ordinal))
                _speechToTextStatus.Text = "Inspreken gestopt. Controleer de tekst en tik op Bewaren.";
        }
    }

    void CleanupSpeechToText(ISpeechToText? service)
    {
        if (service is not null)
        {
            service.RecognitionResultUpdated -= OnSpeechToTextUpdated;
            service.RecognitionResultCompleted -= OnSpeechToTextCompleted;
        }
        if (ReferenceEquals(_speechToTextService, service)) _speechToTextService = null;
        _speechToTextListening = false;
        if (_speechToTextButton is not null)
            MainThread.BeginInvokeOnMainThread(() => _speechToTextButton.Text = "🗣️ Inspreken naar tekst");
    }

'''
        text = text.replace(speech_anchor, methods + speech_anchor, 1)

    required = (
        "public sealed record JournalDraftEntry(",
        "void FlushDraft()",
        "ToggleSpeechToTextAsync()",
        "TryStartSpeechToTextAsync(ISpeechToText service)",
        "StartVoiceRecordingAsync()",
        "Concept automatisch en versleuteld bewaard",
    )
    missing = [item for item in required if item not in text]
    if missing:
        raise RuntimeError(f"Journal accessibility integration incomplete in {page}: {missing}")
    page.write_text(text, encoding="utf-8")
