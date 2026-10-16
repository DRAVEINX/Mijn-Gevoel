using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Maui.Devices;
using Microsoft.Maui.Storage;
using MijnGevoel.Core.Data;

namespace __MOODPRESS_NAMESPACE__;

/// <summary>Client for encrypted cloud snapshots. Tokens and local baseline keys use platform SecureStorage.</summary>
public static class CloudSyncClient
{
    const string EndpointKey = "moodpress.cloud.endpoint";
    const string TokenKey = "moodpress.cloud.token";
    const string WorkspaceKey = "moodpress.cloud.workspace";
    const string AutoSyncKey = "moodpress.cloud.auto-sync";
    const long MaxArchiveBytes = 100L * 1024 * 1024;
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(120) };
    static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    static readonly SemaphoreSlim SyncLock = new(1, 1);
    static DateTime _lastAutoAttemptUtc = DateTime.MinValue;

    public static bool AutoSyncEnabled
    {
        get => Preferences.Default.Get(AutoSyncKey, true);
        set => Preferences.Default.Set(AutoSyncKey, value);
    }

    public static async Task<string> LoadEndpointAsync() => (await SecureStorage.Default.GetAsync(EndpointKey)) ?? "";
    public static async Task<string> LoadEmailHintAsync() => (await SecureStorage.Default.GetAsync("moodpress.cloud.email")) ?? "";
    public static async Task<bool> HasSessionAsync() => !string.IsNullOrWhiteSpace(await SecureStorage.Default.GetAsync(TokenKey));

    public static async Task AuthenticateAsync(string endpoint, string email, string password, bool register,
        string displayName, CancellationToken cancellationToken = default)
    {
        if (AppSession.Store is null) throw new InvalidOperationException("Ontgrendel eerst je MoodPress-account.");
        var baseUrl = NormalizeEndpoint(endpoint);
        if (string.IsNullOrWhiteSpace(email) || string.IsNullOrEmpty(password))
            throw new InvalidOperationException("Vul je portal-e-mailadres en wachtwoord in.");
        if (register && string.IsNullOrWhiteSpace(displayName))
            throw new InvalidOperationException("Vul een naam in voor het nieuwe portalaccount.");

        using var request = new HttpRequestMessage(HttpMethod.Post, baseUrl + (register ? "/api/v1/auth/register" : "/api/v1/auth/login"));
        object payload = register
            ? new { email = email.Trim(), displayName = displayName.Trim(), password }
            : new { email = email.Trim(), password };
        request.Content = JsonContent.Create(payload, options: Json);
        using var response = await Http.SendAsync(request, cancellationToken);
        var session = await ReadResponseAsync<SessionDto>(response, cancellationToken);
        if (string.IsNullOrWhiteSpace(session.AccessToken)) throw new InvalidDataException("De portal gaf geen toegangstoken terug.");

        try
        {
            var me = await SendApiAsync<MeDto>(baseUrl, session.AccessToken, null, HttpMethod.Get, "/api/v1/me", null, cancellationToken);
            var owner = me.Workspaces.FirstOrDefault(w => string.Equals(w.Role, "owner", StringComparison.OrdinalIgnoreCase));
            if (owner is null) throw new InvalidOperationException("Dit portalaccount heeft geen eigen werkruimte. Cloudsynchronisatie kan alleen vanuit je eigen werkruimte.");
            await SecureStorage.Default.SetAsync(EndpointKey, baseUrl);
            await SecureStorage.Default.SetAsync(TokenKey, session.AccessToken);
            await SecureStorage.Default.SetAsync(WorkspaceKey, owner.Id);
            await SecureStorage.Default.SetAsync("moodpress.cloud.email", session.User.Email);
        }
        catch
        {
            try
            {
                using var revoke = new HttpRequestMessage(HttpMethod.Post, baseUrl + "/api/v1/auth/logout");
                revoke.Headers.Authorization = new AuthenticationHeaderValue("Bearer", session.AccessToken);
                using var ignored = await Http.SendAsync(revoke, cancellationToken);
            }
            catch { }
            throw;
        }
    }

    public static async Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        var endpoint = await SecureStorage.Default.GetAsync(EndpointKey);
        var token = await SecureStorage.Default.GetAsync(TokenKey);
        if (!string.IsNullOrWhiteSpace(endpoint) && !string.IsNullOrWhiteSpace(token))
        {
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Post, endpoint.TrimEnd('/') + "/api/v1/auth/logout");
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                using var response = await Http.SendAsync(request, cancellationToken);
            }
            catch { /* Local credentials must still be cleared if the network is unavailable. */ }
        }
        SecureStorage.Default.Remove(TokenKey);
        SecureStorage.Default.Remove(WorkspaceKey);
        SecureStorage.Default.Remove(EndpointKey);
        SecureStorage.Default.Remove("moodpress.cloud.email");
    }

    public static async Task<CloudSyncResult> SyncNowAsync(CancellationToken cancellationToken = default)
    {
        if (AppSession.Store is null || string.IsNullOrWhiteSpace(AppSession.Username))
            throw new InvalidOperationException("Ontgrendel eerst je MoodPress-account.");
        await SyncLock.WaitAsync(cancellationToken);
        var temporary = new List<string>();
        try
        {
            var endpoint = await SecureStorage.Default.GetAsync(EndpointKey);
            var token = await SecureStorage.Default.GetAsync(TokenKey);
            var workspace = await SecureStorage.Default.GetAsync(WorkspaceKey);
            if (string.IsNullOrWhiteSpace(endpoint) || string.IsNullOrWhiteSpace(token) || string.IsNullOrWhiteSpace(workspace))
                throw new InvalidOperationException("Koppel eerst je beveiligde MoodPress-cloudaccount in dit scherm.");

            var accountPathId = Path.GetFileNameWithoutExtension(AppSession.Accounts.AccountFilePath(AppSession.Username));
            var vaultId = AppSession.Store.GetStableCloudVaultId();
            if (accountPathId.Length != 64 || accountPathId.Any(c => !Uri.IsHexDigit(c)) ||
                vaultId.Length != 64 || vaultId.Any(c => !Uri.IsHexDigit(c)))
                throw new InvalidDataException("De lokale kluis-ID kan niet veilig worden vastgesteld.");

            var localZip = NewTempPath("local");
            var remoteZip = NewTempPath("remote");
            var mergedZip = NewTempPath("merged");
            temporary.Add(localZip); temporary.Add(remoteZip); temporary.Add(mergedZip);
            await Task.Run(() => BackupService.Create(AppSession.Accounts, AppSession.Username, localZip), cancellationToken);
            EnsureArchiveSize(localZip);

            var remote = await TryGetSnapshotAsync(endpoint, token, workspace, cancellationToken);
            if (remote is null)
            {
                var plainBytes = await File.ReadAllBytesAsync(localZip, cancellationToken);
                byte[]? encryptedBytes = null;
                try
                {
                    encryptedBytes = AppSession.Store.EncryptCloudSnapshot(plainBytes);
                    var digest = Convert.ToHexString(SHA256.HashData(encryptedBytes));
                    var saved = await PutSnapshotAsync(endpoint, token, workspace, 0, vaultId, digest, encryptedBytes, cancellationToken);
                    await SaveBaselineAsync(vaultId, localZip, cancellationToken);
                    return new CloudSyncResult(true, "Eerste cloudsnapshot opgeslagen.", saved.Version, 0, true);
                }
                finally
                {
                    CryptographicOperations.ZeroMemory(plainBytes);
                    if (encryptedBytes is not null) CryptographicOperations.ZeroMemory(encryptedBytes);
                }
            }

            if (!string.Equals(remote.VaultId, vaultId, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Deze cloudwerkruimte is gekoppeld aan een andere lokale MoodPress-kluis. Er is niets aangepast.");

            byte[] remoteBytes;
            try { remoteBytes = Convert.FromBase64String(remote.EncryptedArchiveBase64); }
            catch (FormatException) { throw new InvalidDataException("De cloudback-up heeft een ongeldig bestandsformaat."); }
            try
            {
                if (remoteBytes.LongLength > MaxArchiveBytes) throw new InvalidDataException("De versleutelde cloudback-up is groter dan 100 MB.");
                var remoteSha = Convert.ToHexString(SHA256.HashData(remoteBytes));
                if (!string.Equals(remoteSha, remote.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new CryptographicException("De cloudback-up faalt de SHA-256-controle. Er is niets aangepast.");
                byte[]? decryptedBytes = null;
                try
                {
                    decryptedBytes = AppSession.Store.DecryptCloudSnapshot(remoteBytes);
                    if (decryptedBytes.LongLength > MaxArchiveBytes)
                        throw new InvalidDataException("De ontsleutelde cloudback-up is groter dan 100 MB.");
                    await File.WriteAllBytesAsync(remoteZip, decryptedBytes, cancellationToken);
                }
                finally { if (decryptedBytes is not null) CryptographicOperations.ZeroMemory(decryptedBytes); }
            }
            finally { CryptographicOperations.ZeroMemory(remoteBytes); }

            var baselineZip = await LoadBaselineAsync(vaultId, cancellationToken);
            if (baselineZip is not null) temporary.Add(baselineZip);
            var merge = CloudArchiveMerger.CreateMergedArchive(localZip, remoteZip, baselineZip, AppSession.Store,
                AppSession.Accounts, AppSession.Username, mergedZip);
            if (!merge.Success)
                throw new CloudSyncConflictException("Er zijn wijzigingen op beide apparaten in hetzelfde item. Er is niets overschreven. Conflicterende items: " + string.Join(", ", merge.Conflicts.Take(5)));

            var remoteFingerprint = CloudArchiveMerger.ContentFingerprint(remoteZip, AppSession.Store, accountPathId);
            var uploadVersion = remote.Version;
            EnsureArchiveSize(mergedZip);
            var mergedBytes = await File.ReadAllBytesAsync(mergedZip, cancellationToken);
            byte[]? encryptedMergedBytes = null;
            try
            {
                if (!string.Equals(merge.ContentFingerprint, remoteFingerprint, StringComparison.Ordinal))
                {
                    encryptedMergedBytes = AppSession.Store.EncryptCloudSnapshot(mergedBytes);
                    var digest = Convert.ToHexString(SHA256.HashData(encryptedMergedBytes));
                    var saved = await PutSnapshotAsync(endpoint, token, workspace, remote.Version, vaultId, digest, encryptedMergedBytes, cancellationToken);
                    uploadVersion = saved.Version;
                }
            }
            finally
            {
                CryptographicOperations.ZeroMemory(mergedBytes);
                if (encryptedMergedBytes is not null) CryptographicOperations.ZeroMemory(encryptedMergedBytes);
            }

            var applied = await Task.Run(() => CloudArchiveMerger.ApplyMergedArchive(mergedZip, AppSession.Store, AppSession.Accounts, AppSession.Username), cancellationToken);
            await SaveBaselineAsync(vaultId, mergedZip, cancellationToken);
            var uploaded = uploadVersion != remote.Version;
            return new CloudSyncResult(true,
                uploaded ? "Cloudsync voltooid; de gezamenlijke gegevens zijn opgeslagen." : "Cloudsync voltooid; beide apparaten hadden dezelfde gegevens.",
                uploadVersion, applied, uploaded);
        }
        finally
        {
            foreach (var path in temporary.Distinct(StringComparer.Ordinal))
                try { if (File.Exists(path)) VaultStore.SecureDelete(path); } catch { }
            SyncLock.Release();
        }
    }

    public static async Task TryAutoSyncAsync()
    {
        if (!AutoSyncEnabled || AppSession.Store is null || !await HasSessionAsync()) return;
        var now = DateTime.UtcNow;
        if (now - _lastAutoAttemptUtc < TimeSpan.FromMinutes(2)) return;
        _lastAutoAttemptUtc = now;
        try { await SyncNowAsync(); }
        catch (OperationCanceledException) { }
        catch (Exception ex) { AppLog.Write(ex); }
    }

    static async Task<SnapshotDto?> TryGetSnapshotAsync(string endpoint, string token, string workspace, CancellationToken cancellationToken)
    {
        using var request = NewApiRequest(endpoint, token, workspace, HttpMethod.Get, "/api/v1/sync", null);
        using var response = await Http.SendAsync(request, cancellationToken);
        if (response.StatusCode == HttpStatusCode.NotFound)
        {
            var raw = await response.Content.ReadAsStringAsync(cancellationToken);
            if (raw.Contains("geen cloudsnapshot", StringComparison.OrdinalIgnoreCase)) return null;
            throw new InvalidOperationException(ExtractError(raw, response.StatusCode));
        }
        return await ReadResponseAsync<SnapshotDto>(response, cancellationToken);
    }

    static async Task<PutSnapshotDto> PutSnapshotAsync(string endpoint, string token, string workspace, long expectedVersion,
        string vaultId, string sha256, byte[] archive, CancellationToken cancellationToken)
    {
        if (archive.LongLength > MaxArchiveBytes) throw new InvalidDataException("De cloudback-up is groter dan 100 MB.");
        var deviceName = DeviceInfo.Name ?? "MoodPress";
        var payload = new { expectedVersion, sha256, vaultId,
            encryptedArchiveBase64 = Convert.ToBase64String(archive),
            deviceId = deviceName.Length <= 128 ? deviceName : deviceName[..128] };
        using var request = NewApiRequest(endpoint, token, workspace, HttpMethod.Put, "/api/v1/sync", JsonContent.Create(payload, options: Json));
        using var response = await Http.SendAsync(request, cancellationToken);
        return await ReadResponseAsync<PutSnapshotDto>(response, cancellationToken);
    }

    static HttpRequestMessage NewApiRequest(string endpoint, string token, string workspace, HttpMethod method, string path, HttpContent? content)
    {
        var request = new HttpRequestMessage(method, endpoint.TrimEnd('/') + path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Headers.Add("X-MoodPress-Workspace", workspace);
        request.Headers.CacheControl = new CacheControlHeaderValue { NoCache = true, NoStore = true };
        request.Content = content;
        return request;
    }

    static async Task<T> SendApiAsync<T>(string endpoint, string token, string? workspace, HttpMethod method, string path, HttpContent? content, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(method, endpoint.TrimEnd('/') + path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (!string.IsNullOrWhiteSpace(workspace)) request.Headers.Add("X-MoodPress-Workspace", workspace);
        request.Content = content;
        using var response = await Http.SendAsync(request, cancellationToken);
        return await ReadResponseAsync<T>(response, cancellationToken);
    }

    static async Task<T> ReadResponseAsync<T>(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var raw = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException(ExtractError(raw, response.StatusCode));
        try { return JsonSerializer.Deserialize<T>(raw, Json) ?? throw new InvalidDataException("De portal gaf een leeg antwoord."); }
        catch (JsonException ex) { throw new InvalidDataException("De portal gaf een ongeldig antwoord.", ex); }
    }

    static string ExtractError(string raw, HttpStatusCode code)
    {
        try
        {
            using var document = JsonDocument.Parse(raw);
            if (document.RootElement.TryGetProperty("error", out var error)) return error.GetString() ?? $"Cloudfout {(int)code}.";
        }
        catch (JsonException) { }
        return $"Cloudfout {(int)code}. Controleer je verbinding en portalinstellingen.";
    }

    static string NormalizeEndpoint(string endpoint)
    {
        var value = (endpoint ?? "").Trim().TrimEnd('/');
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
            throw new InvalidOperationException("Vul het volledige HTTPS-adres van de MoodPress-server in.");
        if (uri.Scheme != Uri.UriSchemeHttps && !(uri.IsLoopback && uri.Scheme == Uri.UriSchemeHttp))
            throw new InvalidOperationException("Voor veiligheid is HTTPS verplicht. Alleen localhost mag HTTP gebruiken tijdens lokaal testen.");
        return value;
    }

    static string NewTempPath(string kind) => Path.Combine(FileSystem.CacheDirectory, $"moodpress-cloud-{kind}-{Guid.NewGuid():N}.zip");

    static void EnsureArchiveSize(string path)
    {
        var info = new FileInfo(path);
        if (!info.Exists || info.Length <= 0 || info.Length > MaxArchiveBytes)
            throw new InvalidDataException("De MoodPress-back-up is leeg of groter dan 100 MB; de cloudversie is niet gewijzigd.");
    }

    static string BaselinePath(string vaultId) => Path.Combine(FileSystem.AppDataDirectory, $"moodpress-cloud-baseline-{vaultId}.bin");
    static string BaselineKeyName(string vaultId) => $"moodpress.cloud.baseline-key.{vaultId}";

    static async Task SaveBaselineAsync(string vaultId, string archivePath, CancellationToken cancellationToken)
    {
        var plain = await File.ReadAllBytesAsync(archivePath, cancellationToken);
        byte[]? key = null, nonce = null, cipher = null, tag = null, output = null;
        try
        {
            var encodedKey = await SecureStorage.Default.GetAsync(BaselineKeyName(vaultId));
            if (string.IsNullOrWhiteSpace(encodedKey))
            {
                key = RandomNumberGenerator.GetBytes(32);
                await SecureStorage.Default.SetAsync(BaselineKeyName(vaultId), Convert.ToBase64String(key));
            }
            else key = Convert.FromBase64String(encodedKey);
            if (key.Length != 32) throw new CryptographicException("De lokale cloud-back-upsleutel is ongeldig.");
            nonce = RandomNumberGenerator.GetBytes(12);
            cipher = new byte[plain.Length];
            tag = new byte[16];
            using (var aes = new AesGcm(key, 16)) aes.Encrypt(nonce, plain, cipher, tag);
            output = new byte[7 + nonce.Length + tag.Length + cipher.Length];
            "MPBASE1"u8.CopyTo(output.AsSpan(0, 7));
            nonce.CopyTo(output, 7);
            tag.CopyTo(output, 19);
            cipher.CopyTo(output, 35);
            var path = BaselinePath(vaultId);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            await File.WriteAllBytesAsync(temporary, output, cancellationToken);
            File.Move(temporary, path, true);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(plain);
            if (key is not null) CryptographicOperations.ZeroMemory(key);
            if (nonce is not null) CryptographicOperations.ZeroMemory(nonce);
            if (cipher is not null) CryptographicOperations.ZeroMemory(cipher);
            if (tag is not null) CryptographicOperations.ZeroMemory(tag);
            if (output is not null) CryptographicOperations.ZeroMemory(output);
        }
    }

    static async Task<string?> LoadBaselineAsync(string vaultId, CancellationToken cancellationToken)
    {
        var path = BaselinePath(vaultId);
        if (!File.Exists(path)) return null;
        var input = await File.ReadAllBytesAsync(path, cancellationToken);
        byte[]? key = null, plain = null;
        var zipPath = NewTempPath("baseline");
        try
        {
            if (input.Length < 51 || !input.AsSpan(0, 7).SequenceEqual("MPBASE1"u8))
                throw new CryptographicException("De lokale basislijn voor cloudsynchronisatie is ongeldig.");
            var encodedKey = await SecureStorage.Default.GetAsync(BaselineKeyName(vaultId));
            if (string.IsNullOrWhiteSpace(encodedKey)) throw new CryptographicException("De lokale sleutel van de synchronisatiebasislijn ontbreekt.");
            key = Convert.FromBase64String(encodedKey);
            if (key.Length != 32) throw new CryptographicException("De lokale basislijnsleutel heeft een ongeldige lengte.");
            var nonce = input.AsSpan(7, 12).ToArray();
            var tag = input.AsSpan(19, 16).ToArray();
            var cipher = input.AsSpan(35).ToArray();
            plain = new byte[cipher.Length];
            try { using var aes = new AesGcm(key, 16); aes.Decrypt(nonce, cipher, tag, plain); }
            finally
            {
                CryptographicOperations.ZeroMemory(nonce);
                CryptographicOperations.ZeroMemory(tag);
                CryptographicOperations.ZeroMemory(cipher);
            }
            if (plain.Length == 0 || plain.LongLength > MaxArchiveBytes) throw new CryptographicException("De lokale basislijn is te groot of leeg.");
            await File.WriteAllBytesAsync(zipPath, plain, cancellationToken);
            return zipPath;
        }
        catch
        {
            try { if (File.Exists(zipPath)) VaultStore.SecureDelete(zipPath); } catch { }
            throw;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(input);
            if (plain is not null) CryptographicOperations.ZeroMemory(plain);
            if (key is not null) CryptographicOperations.ZeroMemory(key);
        }
    }

    private sealed record SessionDto(string AccessToken, DateTimeOffset ExpiresAt, CloudUserDto User);
    private sealed record CloudUserDto(string Id, string Email, string DisplayName);
    private sealed record WorkspaceDto(string Id, string Name, string Role, string[] Categories, string MembershipId);
    private sealed record MeDto(CloudUserDto User, WorkspaceDto[] Workspaces);
    private sealed record SnapshotDto(long Version, string Sha256, string VaultId, string EncryptedArchiveBase64, DateTimeOffset UpdatedAt, string DeviceId);
    private sealed record PutSnapshotDto(long Version, string Sha256, DateTimeOffset UpdatedAt);
}

public sealed record CloudSyncResult(bool Succeeded, string Message, long Version, int AppliedRecords, bool Uploaded);
public sealed class CloudSyncConflictException(string message) : Exception(message);
