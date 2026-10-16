#!/usr/bin/env python3
"""Apply the checked-in MoodPress cloud-sync client to the separate iOS or Windows source package.

The release source archives are never modified in-place in the repository. CI extracts a
fresh copy, applies this deterministic port script, then compiles/tests that copy.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
import shutil
import sys
from patch_journal_accessibility import patch_journal_draft_and_dictation

MAX_RECORD_BYTES = 64 * 1024 * 1024

def locate_source(source_root: Path, target: str) -> tuple[Path, Path]:
    candidates = [source_root]
    if target == "ios":
        candidates.insert(0, source_root / "MijnGevoel-Android")
    else:
        candidates.insert(0, source_root / "MoodPress-Windows-source-2.3.0")
    for root in candidates:
        app_dir = root / ("MijnGevoel.Android" if target == "ios" else "MoodPress.WindowsApp")
        core_dir = root / "MijnGevoel.Core"
        if app_dir.is_dir() and core_dir.is_dir():
            return app_dir, core_dir
    raise FileNotFoundError(
        f"Could not locate the {target} app and Core project under {source_root}. "
        "Check the extracted source archive layout."
    )

def sync_shared_core(android_source_root: Path, target_core: Path) -> None:
    """Bring shared Windows/iOS Core code forward to the Android 2.5.2 baseline."""
    candidates = [android_source_root, android_source_root / "MijnGevoel-Android"]
    source_core = next((root / "MijnGevoel.Core" for root in candidates if (root / "MijnGevoel.Core").is_dir()), None)
    if source_core is None:
        raise FileNotFoundError(
            f"Could not find MijnGevoel.Core beneath Android source root {android_source_root}. "
            "Extract MoodPress-Android-compleet-2.5.2.zip and pass its parent directory."
        )
    copied = 0
    for subdirectory in ("Data", "Security"):
        source_directory = source_core / subdirectory
        target_directory = target_core / subdirectory
        if not source_directory.is_dir() or not target_directory.is_dir():
            raise FileNotFoundError(f"Shared Core directory missing: {source_directory} or {target_directory}")
        source_files = {path.name for path in source_directory.glob("*.cs")}
        for source_file in source_directory.glob("*.cs"):
            shutil.copy2(source_file, target_directory / source_file.name)
            copied += 1
        for target_file in target_directory.glob("*.cs"):
            if target_file.name not in source_files:
                target_file.unlink()
    print(f"Synced {copied} shared Core files from Android 2.5.2 into {target_core}")


def sync_shared_tests(android_source_root: Path, target_tests: Path) -> None:
    """Use the same Core/security regression suite as Android 2.5.2 on Windows and iOS."""
    candidates = [android_source_root, android_source_root / "MijnGevoel-Android"]
    source_tests = next((root / "MijnGevoel.Tests" for root in candidates
                         if (root / "MijnGevoel.Tests" / "SecurityTests.cs").is_file()), None)
    if source_tests is None:
        raise FileNotFoundError(f"Could not find Android 2.5.2 regression tests under {android_source_root}")
    if not (target_tests / "MijnGevoel.Tests.csproj").is_file():
        raise FileNotFoundError(f"Target regression test project is missing: {target_tests}")
    copied = 0
    for source_file in source_tests.glob("*.cs"):
        shutil.copy2(source_file, target_tests / source_file.name)
        copied += 1
    print(f"Synced {copied} current Android Core/security test files into {target_tests}")


def add_vault_validator(vault_file: Path) -> None:
    source = vault_file.read_text(encoding="utf-8")
    fields_marker = "readonly byte[] _key; readonly string _dir;"
    # Match the record size limit used by the Android 4.0 Core.
    if "MaxRecordBytes" not in source:
        vault_open = re.search(r"(public sealed class VaultStore[^\{]*\{)", source)
        if not vault_open:
            raise RuntimeError(f"Could not find VaultStore class declaration in {vault_file}")
        source = source[:vault_open.end()] + "\n    public const long MaxRecordBytes = 64L * 1024 * 1024;\n" + source[vault_open.end():]
    if "CloudSnapshotMagic" not in source:
        if source.count(fields_marker) != 1:
            raise RuntimeError(f"Expected one VaultStore fields marker in {vault_file}")
        fields = """
    const int CloudSnapshotHeaderLength = 36;
    const int MaxCloudSnapshotBytes = 100 * 1024 * 1024;
    static readonly byte[] CloudSnapshotMagic = "MPCLOUD1"u8.ToArray();
    static readonly byte[] CloudSnapshotAad = "MoodPress cloud snapshot v1"u8.ToArray();"""
        source = source.replace(fields_marker, fields_marker + "\n" + fields, 1)

    marker = "    public T? Load<T>(string kind, Guid id)"
    if source.count(marker) != 1:
        raise RuntimeError(f"Expected one VaultStore.Load marker in {vault_file}")

    methods = {
        "GetStableCloudVaultId": """
    /// <summary>Returns a stable, non-reversible identifier for this active high-entropy vault key.</summary>
    public string GetStableCloudVaultId()
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        hash.AppendData("MoodPress cloud vault identity v1"u8);
        hash.AppendData(_key);
        var digest = hash.GetHashAndReset();
        try { return Convert.ToHexString(digest); }
        finally { CryptographicOperations.ZeroMemory(digest); }
    }

""",
        "EncryptCloudSnapshot": """
    /// <summary>Encrypts an entire backup archive with the active vault key before it leaves this device.</summary>
    public byte[] EncryptCloudSnapshot(byte[] plaintext)
    {
        ArgumentNullException.ThrowIfNull(plaintext);
        if (plaintext.Length == 0 || plaintext.LongLength > MaxCloudSnapshotBytes - CloudSnapshotHeaderLength)
            throw new ArgumentException("De cloudback-up is leeg of te groot.", nameof(plaintext));
        var output = new byte[CloudSnapshotHeaderLength + plaintext.Length];
        var nonce = RandomNumberGenerator.GetBytes(12);
        try
        {
            CloudSnapshotMagic.CopyTo(output, 0);
            nonce.CopyTo(output, 8);
            using var aes = new AesGcm(_key, 16);
            aes.Encrypt(nonce, plaintext, output.AsSpan(CloudSnapshotHeaderLength), output.AsSpan(20, 16), CloudSnapshotAad);
            return output;
        }
        catch
        {
            CryptographicOperations.ZeroMemory(output);
            throw;
        }
        finally { CryptographicOperations.ZeroMemory(nonce); }
    }

""",
        "DecryptCloudSnapshot": """
    /// <summary>Authenticates and decrypts only a snapshot sealed with the active vault key.</summary>
    public byte[] DecryptCloudSnapshot(byte[] envelope)
    {
        ArgumentNullException.ThrowIfNull(envelope);
        if (envelope.Length <= CloudSnapshotHeaderLength || envelope.Length > MaxCloudSnapshotBytes ||
            !envelope.AsSpan(0, 8).SequenceEqual(CloudSnapshotMagic))
            throw new InvalidDataException("De versleutelde cloudback-up is ongeldig of te groot.");
        var plaintext = new byte[envelope.Length - CloudSnapshotHeaderLength];
        try
        {
            using var aes = new AesGcm(_key, 16);
            aes.Decrypt(envelope.AsSpan(8, 12), envelope.AsSpan(CloudSnapshotHeaderLength),
                envelope.AsSpan(20, 16), plaintext, CloudSnapshotAad);
            return plaintext;
        }
        catch
        {
            CryptographicOperations.ZeroMemory(plaintext);
            throw;
        }
    }

""",
        "IsEncryptedRecordForCurrentKey": """
    /// <summary>Validates an encrypted record against this account's active vault key without returning plaintext.</summary>
    public bool IsEncryptedRecordForCurrentKey(string kind, Guid id, byte[]? blob)
    {
        if (string.IsNullOrWhiteSpace(kind) || kind.Length > 80 ||
            kind.Any(ch => !((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '-')))
            return false;
        if (blob is null || blob.Length <= 0 || blob.LongLength > 67108864L)
            return false;

        byte[]? plain = null;
        try
        {
            plain = Crypto.Open(_key, blob, $"{kind}:{id:N}");
            using var document = JsonDocument.Parse(plain);
            return true;
        }
        catch (Exception ex) when (ex is CryptographicException or JsonException or ArgumentException or InvalidDataException)
        {
            return false;
        }
        finally
        {
            if (plain is not null) CryptographicOperations.ZeroMemory(plain);
        }
    }

"""
    }
    for method_name, method_body in methods.items():
        if f"{method_name}(" not in source:
            source = source.replace(marker, method_body + marker, 1)
    vault_file.write_text(source, encoding="utf-8")

def patch_app_session(app_dir: Path) -> None:
    path = app_dir / "AppSession.cs"
    source = path.read_text(encoding="utf-8")
    if "CloudSyncClient.TryAutoSyncAsync()" in source:
        return
    marker = re.compile(r"(public static void Touch\(\)\s*\{\s*LastActivityUtc = DateTime\.UtcNow;)")
    source, count = marker.subn(
        r"\1\n        if (Store is not null) _ = CloudSyncClient.TryAutoSyncAsync();",
        source,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"Could not add cloud auto-sync hook to {path}")
    path.write_text(source, encoding="utf-8")

def patch_dashboard(app_dir: Path) -> None:
    path = app_dir / "DashboardPage.cs"
    source = path.read_text(encoding="utf-8")
    if "FeatureKind.CloudSync" not in source:
        enum_match = re.search(r"(public enum FeatureKind\s*\{)(.*?)(\})", source, re.S)
        if not enum_match:
            raise RuntimeError(f"Could not find FeatureKind enum in {path}")
        body = enum_match.group(2).rstrip()
        if body and not body.endswith(","):
            body += ","
        body += "\n    CloudSync\n"
        source = source[:enum_match.start(2)] + body + source[enum_match.end(2):]

    help_entry = '("Hulp", "❓", FeatureKind.Help)'
    cloud_entry = '("Cloudsync tussen apparaten", "☁️", FeatureKind.CloudSync)'
    if cloud_entry not in source:
        if source.count(help_entry) != 1:
            raise RuntimeError(f"Could not identify the final dashboard group entry in {path}")
        source = source.replace(help_entry, help_entry + ", " + cloud_entry, 1)

    ordinary_route = "else _ = Navigation.PushAsync(new FeaturePage(kind));"
    cloud_route = "else if (kind == FeatureKind.CloudSync) _ = Navigation.PushAsync(new CloudSyncPage());"
    if cloud_route not in source:
        if source.count(ordinary_route) != 1:
            raise RuntimeError(f"Could not identify the default feature route in {path}")
        source = source.replace(ordinary_route, cloud_route + "\n        " + ordinary_route, 1)
    groups_anchor = "            var items = Groups[groupIndex];"
    groups_filtered = """            var items = Groups[groupIndex]
                .Where(item => item.Kind == FeatureKind.WhoWorks ||
                    FeatureVisibilityService.IsEnabled(AppSession.Settings, item.Kind.ToString()))
                .ToArray();
            if (items.Length == 0) continue;"""
    if "FeatureVisibilityService.IsEnabled(AppSession.Settings, item.Kind.ToString())" not in source:
        if source.count(groups_anchor) != 1:
            raise RuntimeError(f"Could not locate dashboard group iteration in {path}")
        source = source.replace(groups_anchor, groups_filtered, 1)

    path.write_text(source, encoding="utf-8")


def extract_added_patch_file(patch_path: Path, file_name: str) -> str:
    """Extract a newly-added source file from the checked-in Android 4.0 patch.

    The patch deliberately remains the source of truth, so platform builds work
    from fresh archive copies instead of mutating the release ZIPs.
    """
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("diff -ruN ") or not line.endswith("/" + file_name):
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("diff -ruN "):
            end += 1
        section = lines[index:end]
        target_header = next((i for i, item in enumerate(section) if item.startswith("+++ ")), None)
        if target_header is None or section[target_header].endswith("/dev/null"):
            continue
        output: list[str] = []
        in_hunk = False
        for item in section[target_header + 1:]:
            if item.startswith("@@ "):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if item.startswith("+") and not item.startswith("+++"):
                output.append(item[1:])
            elif item.startswith(" "):
                output.append(item[1:])
            elif item.startswith("\\"):
                continue
            elif item.startswith("-"):
                continue
        content = "\n".join(output) + "\n"
        if content.strip():
            return content
    raise FileNotFoundError(f"Could not extract added {file_name} from {patch_path}")


def patch_routine_reminder_model(models_file: Path) -> None:
    source = models_file.read_text(encoding="utf-8")
    marker = "    bool SoundEnabled = true);"
    replacement = """    bool SoundEnabled = true)
{
    /// <summary>Temporary single-use reminder such as a 15-minute snooze.</summary>
    public bool OneTime { get; init; }
    /// <summary>Local date/time for a one-time reminder.</summary>
    public DateTime? OneTimeDate { get; init; }
    /// <summary>Skip this reminder for one local calendar day.</summary>
    public DateTime? SkipDate { get; init; }
    /// <summary>Defer reminders that fall inside the chosen quiet interval.</summary>
    public bool RespectQuietHours { get; init; }
    public string? QuietStart { get; init; }
    public string? QuietEnd { get; init; }
}"""
    if "public bool RespectQuietHours { get; init; }" not in source:
        if source.count(marker) != 1:
            raise RuntimeError(f"Could not find the legacy RoutineReminder record in {models_file}")
        source = source.replace(marker, replacement, 1)

    # Keep legacy JSON records readable while porting the Android 4.0 agenda status.
    old_agenda = "public record AgendaItem(Guid Id, DateTime Date, string Time, string Title, string Icon, bool Done);"
    new_agenda = """public record AgendaItem(Guid Id, DateTime Date, string Time, string Title, string Icon, bool Done, string? Status = null)
{
    public string CurrentStatus => string.IsNullOrWhiteSpace(Status) ? (Done ? "Afgerond" : "Gepland") : Status;
    public string Location { get; init; } = "";
    public string Note { get; init; } = "";
    public string ChangeSummary { get; init; } = "";
}"""
    if old_agenda in source:
        source = source.replace(old_agenda, new_agenda, 1)

    model_marker = 'public record TalkCard(Guid Id, string Emoji, string Text);'
    additions = [
        ("public record CalmOptionEntry(", "public record CalmOptionEntry(Guid Id, string Title, string Note, DateTime Created, DateTime Updated);"),
        ("public record WellbeingEntry(", "public record WellbeingEntry(Guid Id, DateTime Created, int SleepQuality, int MovementMinutes, int EnergyLevel, string Note);"),
        ("public record CommunicationBoardCard(", "public record CommunicationBoardCard(Guid Id, string Category, string Emoji, string Text, byte[]? PhotoData, DateTime Created);"),
        ("public record RoutineStepMedia(", "public record RoutineStepMedia(Guid Id, Guid RoutineId, int StepIndex, byte[]? PhotoData, byte[]? AudioData, string Narration, DateTime UpdatedAt);"),
        ("public record SupportAccessGrant(", "public record SupportAccessGrant(Guid Id, string Recipient, string Role, string[] Categories, DateTime CreatedAt, DateTime? ExpiresAt, bool Active);"),
        ("public record SupportHandoffEntry(", "public record SupportHandoffEntry(Guid Id, DateTime CreatedAt, string SenderRole, string ReceiverRole, string Summary, string[] Topics, bool Acknowledged, DateTime? AcknowledgedAt);"),
        ("public record SyncTransferRecord(", "public record SyncTransferRecord(Guid Id, DateTime CreatedAt, string DeviceLabel, string Action, int ImportedFiles, string Fingerprint, string Result);"),
        ("public record BackupHealthRecord(", "public record BackupHealthRecord(Guid Id, DateTime CreatedAt, string FileName, int ArchiveEntries, string Sha256, bool ArchiveReadable, string Result);"),
        ("public record AccessibilityAuditRecord(", "public record AccessibilityAuditRecord(Guid Id, DateTime CreatedAt, Dictionary<string, bool> Checks, string Notes);"),
    ]
    missing = [definition for declaration, definition in additions if declaration not in source]
    if missing:
        if model_marker not in source:
            raise RuntimeError(f"Could not find the TalkCard model insertion point in {models_file}")
        source = source.replace(model_marker, model_marker + "\n" + "\n".join(missing), 1)
    models_file.write_text(source, encoding="utf-8")

def patch_support_share_handoff(service_file: Path) -> None:
    source = service_file.read_text(encoding="utf-8")
    if "SupportHandoffEntry? Handoff" in source:
        return
    source = source.replace(
        "    public List<YouthSupportEntry> YouthCheckIns { get; init; } = new();",
        "    public List<YouthSupportEntry> YouthCheckIns { get; init; } = new();\n    public SupportHandoffEntry? Handoff { get; init; }",
        1)
    old_signature = "IEnumerable<MoodCheckInEntry>? moodCheckIns = null, IEnumerable<YouthSupportEntry>? youthCheckIns = null)"
    new_signature = "IEnumerable<MoodCheckInEntry>? moodCheckIns = null, IEnumerable<YouthSupportEntry>? youthCheckIns = null, SupportHandoffEntry? handoff = null)"
    if old_signature not in source:
        raise RuntimeError(f"Could not update Create signature in {service_file}")
    source = source.replace(old_signature, new_signature, 1)
    source = source.replace(
        "        if (youth.Any(x => !ValidateYouthCheckIn(x))) throw new ArgumentException(\"Een jongerencheck-in bevat ongeldige gegevens.\");",
        "        if (youth.Any(x => !ValidateYouthCheckIn(x))) throw new ArgumentException(\"Een jongerencheck-in bevat ongeldige gegevens.\");\n        if (handoff is not null && !ValidateHandoff(handoff)) throw new ArgumentException(\"De overdrachtskaart bevat ongeldige gegevens.\");",
        1)
    source = source.replace(
        "if (plan is null && lights.Count == 0 && routineList.Count == 0 && moods.Count == 0 && youth.Count == 0)",
        "if (plan is null && lights.Count == 0 && routineList.Count == 0 && moods.Count == 0 && youth.Count == 0 && handoff is null)",
        1)
    source = source.replace(
        "var package = new SupportSharePackage(2, safeFrom, DateTime.Now, plan, lights, routineList)\n        { MoodCheckIns = moods, YouthCheckIns = youth };",
        "var package = new SupportSharePackage(3, safeFrom, DateTime.Now, plan, lights, routineList)\n        { MoodCheckIns = moods, YouthCheckIns = youth, Handoff = handoff };",
        1)
    source = source.replace("p.Version is not (1 or 2)", "p.Version is not (1 or 2 or 3)", 1)
    source = source.replace(
        "|| p.MoodCheckIns.Any(x => !ValidateMoodCheckIn(x)) || p.YouthCheckIns.Any(x => !ValidateYouthCheckIn(x))) return false;",
        "|| p.MoodCheckIns.Any(x => !ValidateMoodCheckIn(x)) || p.YouthCheckIns.Any(x => !ValidateYouthCheckIn(x))\n            || (p.Handoff is not null && !ValidateHandoff(p.Handoff))) return false;",
        1)
    source = source.replace(
        "|| p.MoodCheckIns.Count > 0 || p.YouthCheckIns.Count > 0;",
        "|| p.MoodCheckIns.Count > 0 || p.YouthCheckIns.Count > 0 || p.Handoff is not null;",
        1)
    validator_marker = "    static bool ValidatePlan(SafetyPlan p)"
    validator = """    static bool ValidateHandoff(SupportHandoffEntry? x) => x is not null
        && x.Id != Guid.Empty
        && x.SenderRole is not null && x.SenderRole.Length <= 120
        && x.ReceiverRole is not null && x.ReceiverRole.Length <= 120
        && x.Summary is not null && x.Summary.Length <= 4_000
        && x.Topics is not null && x.Topics.Length <= 50
        && x.Topics.All(t => t is not null && t.Length <= 120);

"""
    if source.count(validator_marker) != 1:
        raise RuntimeError(f"Could not find ValidatePlan in {service_file}")
    source = source.replace(validator_marker, validator + validator_marker, 1)
    service_file.write_text(source, encoding="utf-8")


def patch_feature_routing(app_dir: Path, namespace: str, repo_root: Path, core_dir: Path) -> None:
    patch_path = repo_root / "MoodPress-4.0.0-github.patch"
    source_names = ("FeaturePage.MoodPress40.cs", "FeaturePage.CloudSync.cs")
    for name in source_names:
        content = extract_added_patch_file(patch_path, name)
        content = content.replace("namespace MijnGevoel.AndroidApp;", f"namespace {namespace};", 1)
        if name == "FeaturePage.MoodPress40.cs":
            content = content.replace(
                "await RoutineReminderNotifications.EnsurePermissionAsync()",
                "await EnsureMoodReminderPermissionAsync()")
            content = content.replace(
                "RoutineReminderNotifications.Sync(Android.App.Application.Context, Store.All<RoutineReminder>(\"routinereminder\"));",
                "SyncMoodReminderNotifications();")
            content = content.replace(
                "RoutineReminderNotifications.ScheduleOneTime(Android.App.Application.Context, once);",
                "bool oneTimeScheduled = ScheduleMoodReminder(once);")
            content = content.replace(
                'status.Text = $"Goed. Je krijgt één herinnering om {when:HH:mm}.";',
                'status.Text = oneTimeScheduled ? $"Goed. Je krijgt één herinnering om {when:HH:mm}." : "De keuze is bewaard, maar meldingen worden op dit apparaat niet ondersteund."; ')

            helper_marker = "\n}\n\ninternal static class MoodReminderIds"
            helpers = r"""
    async Task<bool> EnsureMoodReminderPermissionAsync()
    {
#if ANDROID
        return await RoutineReminderNotifications.EnsurePermissionAsync();
#elif IOS
        var center = UserNotifications.UNUserNotificationCenter.Current;
        var settings = await center.GetNotificationSettingsAsync();
        if (settings.AuthorizationStatus == UserNotifications.UNAuthorizationStatus.NotDetermined)
        {
            // The .NET iOS binding returns (granted, NSError), not a bare bool.
            var authorization = await center.RequestAuthorizationAsync(
                UserNotifications.UNAuthorizationOptions.Alert |
                UserNotifications.UNAuthorizationOptions.Sound |
                UserNotifications.UNAuthorizationOptions.Badge);
            return authorization.Item1;
        }
        return settings.AuthorizationStatus == UserNotifications.UNAuthorizationStatus.Authorized ||
            settings.AuthorizationStatus == UserNotifications.UNAuthorizationStatus.Provisional;
#else
        return false;
#endif
    }

    void SyncMoodReminderNotifications()
    {
#if ANDROID
        RoutineReminderNotifications.Sync(Android.App.Application.Context, Store.All<RoutineReminder>("routinereminder"));
#elif WINDOWS
        RoutineReminderNotifications.Sync(Store.All<RoutineReminder>("routinereminder"));
#elif IOS
        _ = SyncIosMoodRemindersAsync();
#endif
    }

    bool ScheduleMoodReminder(RoutineReminder reminder)
    {
#if ANDROID
        RoutineReminderNotifications.ScheduleOneTime(Android.App.Application.Context, reminder);
        return true;
#elif IOS
        Store.Save("routinereminder", reminder.Id, reminder);
        _ = SyncIosMoodRemindersAsync();
        return true;
#else
        return false;
#endif
    }

#if IOS
    async Task SyncIosMoodRemindersAsync()
    {
        var center = UserNotifications.UNUserNotificationCenter.Current;
        var pending = await center.GetPendingNotificationRequestsAsync();
        var ownIds = pending.Select(x => x.Identifier)
            .Where(x => x.StartsWith("moodpress-reminder-", StringComparison.Ordinal)).ToArray();
        if (ownIds.Length > 0) center.RemovePendingNotificationRequests(ownIds);

        foreach (var item in Store.All<RoutineReminder>("routinereminder"))
        {
            if (!item.Enabled) continue;
            var content = new UserNotifications.UNMutableNotificationContent
            {
                Title = item.Title.Length > 0 ? item.Title : "MoodPress",
                Body = "Je kunt je check-in doen of de herinnering overslaan.",
                Sound = item.SoundEnabled ? UserNotifications.UNNotificationSound.Default : null
            };
            if (item.OneTime && item.OneTimeDate is DateTime oneTime)
            {
                var seconds = Math.Max(1, (oneTime - DateTime.Now).TotalSeconds);
                var trigger = UserNotifications.UNTimeIntervalNotificationTrigger.CreateTrigger(seconds, false);
                var request = UserNotifications.UNNotificationRequest.FromIdentifier("moodpress-reminder-" + item.Id.ToString("N"), content, trigger);
                await center.AddNotificationRequestAsync(request);
                continue;
            }
            if (!TimeSpan.TryParse(item.Time, CultureInfo.InvariantCulture, out var localTime)) continue;
            if (item.RespectQuietHours &&
                TimeSpan.TryParse(item.QuietStart, CultureInfo.InvariantCulture, out var quietStart) &&
                TimeSpan.TryParse(item.QuietEnd, CultureInfo.InvariantCulture, out var quietEnd) &&
                IsWithinQuietHours(localTime, quietStart, quietEnd))
                localTime = quietEnd;

            foreach (DayOfWeek day in Enum.GetValues(typeof(DayOfWeek)))
            {
                if ((item.DaysMask & (1 << (int)day)) == 0) continue;
                var components = new Foundation.NSDateComponents
                {
                    Weekday = day == DayOfWeek.Sunday ? 1 : (int)day + 1,
                    Hour = (nint)localTime.Hours,
                    Minute = (nint)localTime.Minutes
                };
                var trigger = UserNotifications.UNCalendarNotificationTrigger.CreateTrigger(components, true);
                var request = UserNotifications.UNNotificationRequest.FromIdentifier(
                    "moodpress-reminder-" + item.Id.ToString("N") + "-" + day, content, trigger);
                await center.AddNotificationRequestAsync(request);
            }
        }
    }

    static bool IsWithinQuietHours(TimeSpan time, TimeSpan start, TimeSpan end) =>
        start < end ? time >= start && time < end : time >= start || time < end;
#endif
"""
            if "async Task<bool> EnsureMoodReminderPermissionAsync()" not in content:
                if content.count(helper_marker) != 1:
                    raise RuntimeError(f"Could not find MoodReminderIds insertion point in {name}")
                content = content.replace(helper_marker, "\n" + helpers + helper_marker, 1)
            if "using UserNotifications;" not in content:
                content = content.replace("using Plugin.Maui.Audio;", "using Plugin.Maui.Audio;\n#if IOS\nusing UserNotifications;\n#endif", 1)
        destination = app_dir / name
        destination.write_text(content, encoding="utf-8")

    secure_transfer = extract_added_patch_file(patch_path, "SecureDeviceTransfer.cs")
    transfer_file = core_dir / "Data" / "SecureDeviceTransfer.cs"
    if not transfer_file.exists():
        transfer_file.write_text(secure_transfer, encoding="utf-8")

    patch_routine_reminder_model(core_dir / "Data" / "Models.cs")
    patch_support_share_handoff(core_dir / "Data" / "SupportShareService.cs")

    dashboard = app_dir / "DashboardPage.cs"
    dashboard_text = dashboard.read_text(encoding="utf-8")
    features = [
        "CommunicationBoard", "ReadAloud", "RoutineMedia", "MoodReminders",
        "DifficultMomentPlan", "SecureSync", "SupportPermissions", "SupportHandoff",
        "BackupHealth", "AccessibilityAudit", "Wellbeing"
    ]
    if any(f"FeatureKind.{kind}" not in dashboard_text for kind in features):
        enum_match = re.search(r"(public enum FeatureKind\s*\{)(.*?)(\})", dashboard_text, re.S)
        if not enum_match:
            raise RuntimeError(f"Could not locate FeatureKind in {dashboard}")
        body = enum_match.group(2)
        for kind in features:
            if f"FeatureKind.{kind}" not in dashboard_text:
                body = body.rstrip()
                if body and not body.rstrip().endswith(","):
                    body += ","
                body += f"\n    {kind}"
        dashboard_text = dashboard_text[:enum_match.start(2)] + body + "\n" + dashboard_text[enum_match.end(2):]

    help_entry = '("Hulp", "❓", FeatureKind.Help)'
    cloud_entry = '("Cloudsync tussen apparaten", "☁️", FeatureKind.CloudSync)'
    menu_items = [
        '("Communicatiebord", "💬", FeatureKind.CommunicationBoard)',
        '("Voorlezen", "🔊", FeatureKind.ReadAloud)',
        '("Media bij stappenplan", "🎧", FeatureKind.RoutineMedia)',
        '("Gevoelsherinneringen", "💛", FeatureKind.MoodReminders)',
        '("Plan voor moeilijke momenten", "🌿", FeatureKind.DifficultMomentPlan)',
        '("Versleutelde overdracht", "🔐", FeatureKind.SecureSync)',
        '("Toestemming begeleiders", "👥", FeatureKind.SupportPermissions)',
        '("Overdracht naar begeleider", "📨", FeatureKind.SupportHandoff)',
        '("Back-upcontrole", "🛡️", FeatureKind.BackupHealth)',
        '("Toegankelijkheidscontrole", "♿", FeatureKind.AccessibilityAudit)',
        '("Mijn welzijn", "💚", FeatureKind.Wellbeing)'
    ]
    if "FeatureKind.CommunicationBoard)" not in dashboard_text.split("public DashboardPage", 1)[0]:
        pass
    menu_anchor = help_entry + ", " + cloud_entry
    if all(item not in dashboard_text for item in menu_items):
        if dashboard_text.count(menu_anchor) != 1:
            raise RuntimeError(f"Could not locate cloud menu insertion point in {dashboard}")
        dashboard_text = dashboard_text.replace(menu_anchor, menu_anchor + ", " + ", ".join(menu_items), 1)
    dashboard.write_text(dashboard_text, encoding="utf-8")

    page = app_dir / "FeaturePage.cs"
    page_text = page.read_text(encoding="utf-8")
    route_anchor = "            case FeatureKind.Settings: BuildSettings(); break;"
    feature_cases = """            case FeatureKind.CommunicationBoard: BuildCommunicationBoard(); break;
            case FeatureKind.Wellbeing: BuildWellbeing(); break;
            case FeatureKind.ReadAloud: BuildReadAloud(); break;
            case FeatureKind.RoutineMedia: BuildRoutineMedia(); break;
            case FeatureKind.MoodReminders: BuildMoodReminders(); break;
            case FeatureKind.DifficultMomentPlan: BuildDifficultMomentPlan(); break;
            case FeatureKind.SecureSync: BuildSecureSync(); break;
            case FeatureKind.SupportPermissions: BuildSupportPermissions(); break;
            case FeatureKind.SupportHandoff: BuildSupportHandoff(); break;
            case FeatureKind.BackupHealth: BuildBackupHealth(); break;
            case FeatureKind.AccessibilityAudit: BuildAccessibilityAudit(); break;
"""
    if "case FeatureKind.CommunicationBoard: BuildCommunicationBoard();" not in page_text:
        if page_text.count(route_anchor) != 1:
            raise RuntimeError(f"Could not locate FeaturePage Build switch in {page}")
        page_text = page_text.replace(route_anchor, feature_cases + route_anchor, 1)
    title_anchor = 'FeatureKind.Settings => "Instellingen", _ => "Hulp"'
    title_additions = """FeatureKind.CommunicationBoard => "Mijn communicatiebord",
        FeatureKind.ReadAloud => "Voorlezen",
        FeatureKind.RoutineMedia => "Media bij stappenplan",
        FeatureKind.MoodReminders => "Gevoelsherinneringen",
        FeatureKind.DifficultMomentPlan => "Mijn plan voor moeilijke momenten",
        FeatureKind.SecureSync => "Versleutelde overdracht",
        FeatureKind.SupportPermissions => "Toestemming begeleiders",
        FeatureKind.SupportHandoff => "Overdracht naar begeleider",
        FeatureKind.BackupHealth => "Back-upcontrole",
        FeatureKind.AccessibilityAudit => "Toegankelijkheidscontrole",
        FeatureKind.Wellbeing => "Mijn welzijn",
        FeatureKind.Settings => "Instellingen", _ => "Hulp" """
    if "FeatureKind.CommunicationBoard =>" not in page_text:
        if page_text.count(title_anchor) != 1:
            raise RuntimeError(f"Could not locate FeaturePage title switch in {page}")
        page_text = page_text.replace(title_anchor, title_additions, 1)
    page.write_text(page_text, encoding="utf-8")


def extract_csharp_method(source: str, method_name: str) -> tuple[int, int, str]:
    """Find a top-level C# method (void, Task, or expression-bodied) with balanced braces."""
    pattern = re.compile(
        rf"(?m)^    (?:(?:async|static|virtual|override|new|sealed|public|private|internal|protected)\s+)*"
        rf"(?:[\w?.]+(?:<[^>\n]+>)?(?:\[\])?\s+)+{re.escape(method_name)}\s*\([^\n]*\)\s*(?P<body>\{{|=>)"
    )
    match = pattern.search(source)
    if not match:
        raise RuntimeError(f"Could not find method {method_name} in C# source")
    if match.group("body") == "=>":
        semicolon = source.find(";", match.end())
        if semicolon < 0:
            raise RuntimeError(f"Could not find expression-bodied method terminator for {method_name}")
        return match.start(), semicolon + 1, source[match.start():semicolon + 1]
    opening = source.find("{", match.start(), match.end())
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return match.start(), index + 1, source[match.start():index + 1]
    raise RuntimeError(f"Unbalanced braces while reading method {method_name}")


def sync_portable_feature_methods(android_source_root: Path, app_dir: Path, namespace: str) -> None:
    """Port current platform-neutral Android 2.5.2 screens to Windows/iOS with guarded platform-specific settings."""
    candidates = [android_source_root, android_source_root / "MijnGevoel-Android"]
    android_app = next((root / "MijnGevoel.Android" for root in candidates
                        if (root / "MijnGevoel.Android" / "FeaturePage.cs").is_file()), None)
    if android_app is None:
        raise FileNotFoundError(f"Could not find Android FeaturePage.cs under {android_source_root}")

    source = (android_app / "FeaturePage.cs").read_text(encoding="utf-8")
    target_path = app_dir / "FeaturePage.cs"
    target = target_path.read_text(encoding="utf-8")

    # Feature visibility settings and the dashboard share this same catalog.
    catalog_path = android_app / "FeatureCatalog.cs"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Could not find Android FeatureCatalog.cs: {catalog_path}")
    catalog = catalog_path.read_text(encoding="utf-8").replace(
        "namespace MijnGevoel.AndroidApp;", f"namespace {namespace};", 1
    )
    wellbeing_entry = '        new(FeatureKind.Wellbeing, "Mijn welzijn", "🌱", "Doelen en zelfstandigheid", "Houd zelfgekozen welzijnsnotities bij."),'
    extra_entries = """        new(FeatureKind.CommunicationBoard, "Mijn communicatiebord", "💬", "Communicatie en steun", "Gebruik tekst en afbeeldingen om aan te geven wat je nodig hebt."),
        new(FeatureKind.ReadAloud, "Voorlezen", "🔊", "Toegankelijkheid", "Laat tekst voorlezen waar dit wordt ondersteund."),
        new(FeatureKind.RoutineMedia, "Media bij stappenplan", "🎧", "Mijn dag en planning", "Koppel afbeeldingen en audio aan stappen."),
        new(FeatureKind.MoodReminders, "Gevoelsherinneringen", "💛", "Mijn gevoel en rust", "Stel zelf herinneringen in om even in te checken."),
        new(FeatureKind.DifficultMomentPlan, "Plan voor moeilijke momenten", "🌿", "Mijn gevoel en rust", "Bewaar persoonlijke stappen die je kunnen helpen."),
        new(FeatureKind.SecureSync, "Versleutelde overdracht", "🔐", "Communicatie en steun", "Zet geselecteerde informatie bewust en versleuteld over."),
        new(FeatureKind.SupportPermissions, "Toestemming begeleiders", "👥", "Communicatie en steun", "Bekijk en beheer toestemming voor ondersteuning."),
        new(FeatureKind.SupportHandoff, "Overdracht naar begeleider", "📨", "Communicatie en steun", "Maak een overzicht voor een afgesproken overdracht."),
        new(FeatureKind.BackupHealth, "Back-upcontrole", "🛡️", "Profiel, privacy en hulp", "Controleer de leesbaarheid en integriteit van een back-up."),
        new(FeatureKind.AccessibilityAudit, "Toegankelijkheidscontrole", "♿", "Profiel, privacy en hulp", "Loop de belangrijkste toegankelijkheidsinstellingen na."),
        new(FeatureKind.CloudSync, "Cloudsync tussen apparaten", "☁️", "Profiel, privacy en hulp", "Synchroniseer versleutelde gegevens tussen gekoppelde apparaten."),
"""
    if "new(FeatureKind.CommunicationBoard" not in catalog:
        if catalog.count(wellbeing_entry) != 1:
            raise RuntimeError("Could not locate the Wellbeing entry in FeatureCatalog.cs")
        catalog = catalog.replace(wellbeing_entry, wellbeing_entry + "\n" + extra_entries.rstrip(), 1)
    (app_dir / "FeatureCatalog.cs").write_text(catalog, encoding="utf-8")

    method_names = (
        "BuildStoplicht", "BuildStoplichtHistory", "BuildAgenda", "BuildDay",
        "BuildRoutines", "BuildRoutineReminders", "BuildCalm",
        "BuildSensory", "BuildBody", "BuildMoodHistory", "BuildDailyCheckIn",
        "BuildMoodCalendar", "BuildMoodInsights", "BuildFeelingWords",
        "BuildCards", "BuildTalkPrep", "BuildSafetyPlan", "BuildShare",
        "BuildDraw", "BuildGallery", "BuildPrivacy", "BuildWeek", "BuildSettings",
        "BuildProfile", "BuildYouthSupport", "BuildAdultPlanning", "BuildHelp"
    )

    for method_name in method_names:
        _, _, modern_method = extract_csharp_method(source, method_name)

        if method_name == "BuildRoutineReminders":
            modern_method = modern_method.replace(
                "await RoutineReminderNotifications.EnsurePermissionAsync()",
                "await EnsureMoodReminderPermissionAsync()")
            modern_method = modern_method.replace(
                'RoutineReminderNotifications.Sync(Android.App.Application.Context, Store.All<RoutineReminder>("routinereminder"));',
                "SyncMoodReminderNotifications();")
            modern_method = modern_method.replace(
                "Routine opgeslagen. Je telefoon kan je op de gekozen dagen herinneren ✔",
                "Routine opgeslagen. Je apparaat kan je op de gekozen dagen herinneren ✔")
            modern_method = modern_method.replace(
                "Routine opgeslagen, maar meldingen zijn niet toegestaan. Zet meldingen aan in de Android-instellingen van MoodPress.",
                "Routine opgeslagen. Op dit apparaat kunnen meldingen beperkt zijn; controleer de beschikbare meldingsinstellingen.")
            modern_method = modern_method.replace(
                "Android kan een melding als pop-up tonen als meldingen zijn toegestaan en je meldingsinstellingen dit toelaten. Zet meldingen voor MoodPress op 'Waarschuwen' en geef toestemming voor meldingen. Geluid kan worden tegengehouden door stille modus of Niet storen.",
                "Meldingen zijn afhankelijk van de instellingen van je apparaat. Geluid kan worden tegengehouden door stille modus of Niet storen.")
            if "Android.App.Application.Context" in modern_method:
                raise RuntimeError("The platform-neutral reminder screen still contains Android-only code.")

        elif method_name == "BuildSettings":
            home_start = '        AddLabel("Snelkoppelingen op Home", title: true);'
            home_end = '        AddLabel("Zichtbare functies", title: true);'
            a = modern_method.find(home_start)
            b = modern_method.find(home_end, a + len(home_start)) if a >= 0 else -1
            if a < 0 or b < 0:
                raise RuntimeError("Could not locate the Home shortcut section in Android BuildSettings.")
            replacement = '''        AddLabel("Snelkoppelingen op Home", title: true);
        AddLabel("Snelkoppelingen op het Android-startscherm worden daar beheerd. Op Windows en iOS vind je je functies via het overzicht.", muted: true);

'''
            modern_method = modern_method[:a] + replacement + modern_method[b:]
            modern_method = modern_method.replace(
                "AppSession.SaveSettings(); AndroidScreenPrivacy.Apply(capture.IsToggled);",
                "AppSession.SaveSettings(); ApplyPlatformScreenPrivacy(capture.IsToggled);")
            modern_method = modern_method.replace(
                "FocusTimerNotifications.Cancel(Android.App.Application.Context);",
                "#if ANDROID\n                FocusTimerNotifications.Cancel(Android.App.Application.Context);\n                #endif")
            modern_method = modern_method.replace(
                'RoutineReminderNotifications.Sync(Android.App.Application.Context, Array.Empty<RoutineReminder>());',
                "SyncMoodReminderNotifications();")
            modern_method = modern_method.replace(
                "Op Android voorkomt deze optie de meeste screenshots en schermopnames via de beveiligde vensterinstelling. Een tweede camera kan dit niet voorkomen.",
                "Het blokkeren van schermafbeeldingen is afhankelijk van het platform; geen enkele instelling kan een tweede camera voorkomen.")
        elif method_name == "BuildPrivacy":
            modern_method = modern_method.replace(
                "Je gegevens worden versleuteld opgeslagen in de privé-appmap van Android. Als je de app verwijdert, kan Android die map verwijderen. Maak eerst een back-up als je gegevens wilt bewaren.",
                "Je gegevens worden versleuteld opgeslagen in de privégegevensmap van dit apparaat. Als je de app verwijdert, kunnen lokale gegevens worden verwijderd. Maak eerst een back-up als je gegevens wilt bewaren.")

        start, end, _ = extract_csharp_method(target, method_name)
        target = target[:start] + modern_method + target[end:]
        print(f"Ported {method_name} from Android 2.5.2 into {app_dir.name}")

        # These methods are new dependencies of the modern daily check-in screen.
    # Keep them shared across Windows and iOS instead of leaving missing callbacks.
    for helper_name in (
        "SaveQuickFeelingAsync", "QuickFeelingEmoji",
        "SendQuickSignalAsync", "ManageMoodCheckInAsync"
    ):
        try:
            extract_csharp_method(target, helper_name)
            continue
        except RuntimeError:
            pass
        _, _, helper_body = extract_csharp_method(source, helper_name)
        insert_at = target.rfind("\n}")
        if insert_at < 0:
            raise RuntimeError(f"Could not find FeaturePage class terminator in {target_path}")
        target = target[:insert_at] + "\n\n" + helper_body + target[insert_at:]
        print(f"Added shared helper {helper_name} to {app_dir.name}")

    communication_using = "using Microsoft.Maui.ApplicationModel.Communication;"
    if communication_using not in target:
        namespace_match = re.search(r"(?m)^namespace\s+[A-Za-z0-9_.]+;", target)
        if not namespace_match:
            raise RuntimeError(f"Could not locate namespace to add SMS API using in {target_path}")
        target = target[:namespace_match.start()] + communication_using + "\n" + target[namespace_match.start():]

    target_path.write_text(target, encoding="utf-8")

def patch_timer_and_routine_progress(app_dir: Path) -> None:
    """Backfill durable timer state and resumable routine steps into the older platform UI."""
    page = app_dir / "FeaturePage.cs"
    source = page.read_text(encoding="utf-8")

    timer_old = "case FeatureKind.Timer: BuildTimer(); break;"
    timer_new = "case FeatureKind.Timer: BuildTimerParity(); break;"
    if timer_new not in source:
        if source.count(timer_old) != 1:
            raise RuntimeError(f"Could not find exactly one Timer route in {page}")
        source = source.replace(timer_old, timer_new, 1)

    appearing_old = "        AppSession.Touch();\n        if (AppSession.Settings.Animations)"
    appearing_new = "        AppSession.Touch();\n        if (_kind == FeatureKind.Timer) Build();\n        if (AppSession.Settings.Animations)"
    if "_kind == FeatureKind.Timer) Build();" not in source:
        if source.count(appearing_old) != 1:
            raise RuntimeError(f"Could not find exactly one OnAppearing timer refresh point in {page}")
        source = source.replace(appearing_old, appearing_new, 1)

    # Newer Android BuildRoutines already restores progress; retain a fallback for older archives.
    if "RoutineRunProgressService.ResumeStep(progress, routine.Steps.Length)" not in source:
        start_old = "() => { _runningRoutine = routine; _routineStep = 0; ShowRoutineRun(); }"
        start_new = '() => { _runningRoutine = routine; _routineStep = RoutineRunProgressService.ResumeStep(Store.Load<RoutineRunProgress>("routinerunprogress", routine.Id), routine.Steps.Length); ShowRoutineRun(); }'
        if source.count(start_old) != 1:
            raise RuntimeError(f"Could not find exactly one routine start action in {page}")
        source = source.replace(start_old, start_new, 1)

    routine_start, routine_end, routine_method = extract_csharp_method(source, "ShowRoutineRun")
    routine_header = "        if (routine is null) return;\n        _root.Children.Clear();"
    routine_with_progress = """        if (routine is null) return;
        if (routine.Steps is null || routine.Steps.Length == 0)
        {
            _runningRoutine = null;
            Build();
            return;
        }
        _routineStep = Math.Clamp(_routineStep, 0, routine.Steps.Length - 1);
        SaveRoutineRunProgress(routine, _routineStep);
        _root.Children.Clear();"""
    if "SaveRoutineRunProgress(routine, _routineStep);" not in routine_method:
        if routine_method.count(routine_header) != 1:
            raise RuntimeError(f"Could not find exactly one ShowRoutineRun insertion point in {page}")
        routine_method = routine_method.replace(routine_header, routine_with_progress, 1)

    completion_old = 'if (_routineStep >= routine.Steps.Length - 1) { await DisplayAlertAsync("Goed gedaan", "Je hebt alle stappen gedaan.", "Klaar"); _runningRoutine = null; Build(); }'
    completion_new = 'if (_routineStep >= routine.Steps.Length - 1) { SaveRoutineRunProgress(routine, _routineStep, completed: true); await DisplayAlertAsync("Goed gedaan", "Je hebt alle stappen gedaan.", "Klaar"); _runningRoutine = null; Build(); }'
    if "SaveRoutineRunProgress(routine, _routineStep, completed: true);" not in routine_method:
        if routine_method.count(completion_old) != 1:
            raise RuntimeError(f"Could not find exactly one routine completion action in {page}")
        routine_method = routine_method.replace(completion_old, completion_new, 1)

    stop_old = 'AddButton("Stoppen en terug", () => { _runningRoutine = null; Build(); }, quiet: true);'
    stop_new = 'AddButton("Stoppen en terug", () => { SaveRoutineRunProgress(routine, _routineStep); _runningRoutine = null; Build(); }, quiet: true);'
    if stop_new not in routine_method:
        if routine_method.count(stop_old) != 1:
            raise RuntimeError(f"Could not find exactly one routine stop action in {page}")
        routine_method = routine_method.replace(stop_old, stop_new, 1)

    source = source[:routine_start] + routine_method + source[routine_end:]
    page.write_text(source, encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("ios", "windows"), required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--android-source-root", required=True, type=Path)
    args = parser.parse_args()

    app_dir, core_dir = locate_source(args.source_root.resolve(), args.target)
    sync_shared_core(args.android_source_root.resolve(), core_dir)
    sync_shared_tests(args.android_source_root.resolve(), app_dir.parent / "MijnGevoel.Tests")
    namespace = "MijnGevoel.AndroidApp" if args.target == "ios" else "MoodPress.WindowsApp"
    # Fix the old QRCoder System.Drawing.Common 6 dependency conflicting with MAUI .NET 10.
    core_project = core_dir / "MijnGevoel.Core.csproj"
    core_project_text = core_project.read_text(encoding="utf-8")
    qrcoder_pattern = re.compile(r'(<PackageReference\s+Include="QRCoder"\s+Version=")[^"]+("\s*/>)')
    core_project_text, qrcoder_count = qrcoder_pattern.subn(r'\g<1>1.8.1\g<2>', core_project_text, count=1)
    if qrcoder_count != 1:
        raise RuntimeError(f"Could not update QRCoder dependency in {core_project}")
    core_project.write_text(core_project_text, encoding="utf-8")

    # The Windows MAUI app targets .NET 10, while the shared Core project targets
    # .NET 8 and brings System.Drawing.Common 6 transitively through QRCoder.
    # Give the Windows executable an explicit .NET 10 reference so assembly
    # resolution prefers the runtime-compatible version. CI suppresses only
    # MSB3277 for this intentional cross-target version bridge; other warnings
    # remain errors.
    if args.target == "windows":
        app_project = app_dir / "MoodPress.WindowsApp.csproj"
        if not app_project.is_file():
            raise FileNotFoundError(f"Windows app project is missing: {app_project}")
        app_project_text = app_project.read_text(encoding="utf-8")
        package_pattern = re.compile(
            r'<PackageReference\b(?=[^>]*\bInclude=["\']System\.Drawing\.Common["\'])[^>]*/>',
            re.I,
        )
        package_matches = list(package_pattern.finditer(app_project_text))
        if len(package_matches) > 1:
            raise RuntimeError(f"Multiple direct System.Drawing.Common references found in {app_project}")
        if package_matches:
            package_tag = package_matches[0].group(0)
            if re.search(r'\bVersion=["\'][^"\']+["\']', package_tag, re.I):
                package_tag = re.sub(
                    r'\bVersion=["\'][^"\']+["\']',
                    'Version="10.0.0"',
                    package_tag,
                    count=1,
                    flags=re.I,
                )
            else:
                package_tag = package_tag[:-2] + ' Version="10.0.0" />'
            app_project_text = app_project_text[:package_matches[0].start()] + package_tag + app_project_text[package_matches[0].end():]
        else:
            if re.search(r'<PackageReference\b[^>]*\bInclude=["\']System\.Drawing\.Common["\']', app_project_text, re.I):
                raise RuntimeError(f"System.Drawing.Common reference in {app_project} is not a self-closing PackageReference; inspect it before patching.")
            closing_tag = "<" + "/Project>"
            closing_project = re.search(r'\s*' + re.escape(closing_tag) + r'\s*$', app_project_text, re.I)
            if not closing_project:
                raise RuntimeError(f"Could not find closing Project tag in {app_project}")
            addition = '  <ItemGroup>\n    <PackageReference Include="System.Drawing.Common" Version="10.0.0" />\n  </ItemGroup>\n'
            app_project_text = app_project_text[:closing_project.start()] + '\n' + addition + app_project_text[closing_project.start():]
        app_project.write_text(app_project_text, encoding="utf-8")
        confirmation = re.compile(
            r'<PackageReference\b(?=[^>]*\bInclude=["\']System\.Drawing\.Common["\'])(?=[^>]*\bVersion=["\']10\.0\.0["\'])[^>]*/>',
            re.I,
        )
        if not confirmation.search(app_project_text):
            raise RuntimeError(f"System.Drawing.Common 10.0.0 could not be confirmed in {app_project}")

    shared = args.repo_root.resolve() / "platform-port" / "shared"
    for name in ("CloudSyncClient.cs", "CloudArchiveMerger.cs", "CloudSyncPage.cs"):
        source_path = shared / name
        if not source_path.is_file():
            raise FileNotFoundError(f"Shared cloud-sync source is missing: {source_path}")
        source = source_path.read_text(encoding="utf-8")
        if "__MOODPRESS_NAMESPACE__" not in source:
            raise RuntimeError(f"Namespace placeholder is missing from {source_path}")
        destination = app_dir / name
        destination.write_text(source.replace("__MOODPRESS_NAMESPACE__", namespace), encoding="utf-8")

    parity_path = args.repo_root.resolve() / "platform-port" / "shared" / "FeaturePage.Parity.cs"
    if not parity_path.is_file():
        raise FileNotFoundError(f"Shared parity UI source is missing: {parity_path}")
    parity_source = parity_path.read_text(encoding="utf-8")
    if "__MOODPRESS_NAMESPACE__" not in parity_source:
        raise RuntimeError(f"Namespace placeholder is missing from {parity_path}")
    (app_dir / "FeaturePage.Parity.cs").write_text(
        parity_source.replace("__MOODPRESS_NAMESPACE__", namespace), encoding="utf-8"
    )

    # Carry forward the latest routine reminder fields and encrypted support-handoff
    # schema after syncing the stable Android Core files. These helpers are idempotent.
    patch_routine_reminder_model(core_dir / "Data" / "Models.cs")
    patch_support_share_handoff(core_dir / "Data" / "SupportShareService.cs")
    add_vault_validator(core_dir / "Data" / "VaultStore.cs")
    patch_app_session(app_dir)
    patch_dashboard(app_dir)
    patch_feature_routing(app_dir, namespace, args.repo_root.resolve(), core_dir)
    sync_portable_feature_methods(args.android_source_root.resolve(), app_dir, namespace)
    patch_timer_and_routine_progress(app_dir)
    # Validate the full accessible journal insertion, including platform-specific delete flows.
    patch_journal_draft_and_dictation(app_dir, args.target)

    required = [
        app_dir / "CloudSyncClient.cs",
        app_dir / "CloudArchiveMerger.cs",
        app_dir / "CloudSyncPage.cs",
        core_dir / "Data" / "VaultStore.cs",
        app_dir / "AppSession.cs",
        app_dir / "DashboardPage.cs",
        app_dir / "FeaturePage.Parity.cs",
    ]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)
    print(f"Applied MoodPress cloud-sync/portal client to {args.target}: {app_dir}")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Cloud platform patch failed: {exc}", file=sys.stderr)
        raise
