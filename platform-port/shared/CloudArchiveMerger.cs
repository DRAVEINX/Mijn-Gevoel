using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using MijnGevoel.Core.Data;
using MijnGevoel.Core.Security;

namespace __MOODPRESS_NAMESPACE__;

/// <summary>Three-way merge of encrypted MoodPress records; conflicts abort before either side is changed.</summary>
public static class CloudArchiveMerger
{
    const int MaxEntries = 20_000;
    const long MaxEntryBytes = 64L * 1024 * 1024;
    const long MaxArchiveBytes = 100L * 1024 * 1024;
    static readonly Regex AccountPath = new(@"^accounts/(?<hash>[A-Fa-f0-9]{64})\.json$", RegexOptions.CultureInvariant);
    static readonly Regex DataPath = new(@"^data/(?<hash>[A-Fa-f0-9]{64})/(?<kind>[a-z0-9][a-z0-9-]*)-(?<id>[A-Fa-f0-9]{32})\.mg$", RegexOptions.CultureInvariant);

    sealed record Archive(string AccountPath, byte[] AccountBytes, Dictionary<string, byte[]> Records, string Fingerprint);
    public sealed record MergeResult(bool Success, string[] Conflicts, string ContentFingerprint);

    public static string ContentFingerprint(string archivePath, VaultStore store, string vaultId)
    {
        var archive = ReadArchive(archivePath, store, vaultId);
        try { return archive.Fingerprint; }
        finally { WipeArchive(archive); }
    }

    public static MergeResult CreateMergedArchive(string localZip, string remoteZip, string? baselineZip, VaultStore store,
        AccountService accounts, string username, string outputZip)
    {
        var vaultId = Path.GetFileNameWithoutExtension(accounts.AccountFilePath(username));
        var local = ReadArchive(localZip, store, vaultId);
        Archive? remote = null;
        Archive? baseline = null;
        try
        {
            remote = ReadArchive(remoteZip, store, vaultId);
            if (baselineZip is not null) baseline = ReadArchive(baselineZip, store, vaultId);
            var keys = local.Records.Keys.Concat(remote.Records.Keys)
                .Concat(baseline is null ? Enumerable.Empty<string>() : baseline.Records.Keys)
                .Distinct(StringComparer.Ordinal).OrderBy(x => x, StringComparer.Ordinal).ToArray();
            var selected = new Dictionary<string, byte[]>(StringComparer.Ordinal);
            var conflicts = new List<string>();

            foreach (var path in keys)
            {
                local.Records.TryGetValue(path, out var localBytes);
                remote.Records.TryGetValue(path, out var remoteBytes);
                byte[]? baseBytes = null;
                if (baseline is not null) baseline.Records.TryGetValue(path, out baseBytes);

                var localChanged = baseline is null ? localBytes is not null : !Same(localBytes, baseBytes);
                var remoteChanged = baseline is null ? remoteBytes is not null : !Same(remoteBytes, baseBytes);
                byte[]? chosen;

                if (localChanged && remoteChanged)
                {
                    if (!Same(localBytes, remoteBytes))
                    {
                        conflicts.Add(Path.GetFileName(path));
                        continue;
                    }
                    chosen = localBytes;
                }
                else if (localChanged) chosen = localBytes;
                else if (remoteChanged) chosen = remoteBytes;
                else chosen = baseBytes ?? localBytes ?? remoteBytes;

                if (chosen is not null) selected[path] = chosen;
            }

            if (conflicts.Count > 0) return new MergeResult(false, conflicts.ToArray(), "");
            WriteArchive(outputZip, local.AccountPath, local.AccountBytes, selected);
            return new MergeResult(true, Array.Empty<string>(), Fingerprint(selected));
        }
        finally
        {
            WipeArchive(local);
            if (remote is not null) WipeArchive(remote);
            if (baseline is not null) WipeArchive(baseline);
        }
    }

    public static int ApplyMergedArchive(string mergedZip, VaultStore store, AccountService accounts, string username)
    {
        var vaultId = Path.GetFileNameWithoutExtension(accounts.AccountFilePath(username));
        var archive = ReadArchive(mergedZip, store, vaultId);
        try
        {
            var dataFolder = Path.GetFullPath(accounts.DataFolder(username));
            var parent = Path.GetDirectoryName(dataFolder)!;
            Directory.CreateDirectory(parent);
            var stage = Path.Combine(parent, ".moodpress-sync-stage-" + Guid.NewGuid().ToString("N"));
            var rollback = Path.Combine(parent, ".moodpress-sync-rollback-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(stage);
            var movedOld = false;
            try
            {
                // Preserve auxiliary files; the validated .mg set is the complete merged record state.
                if (Directory.Exists(dataFolder))
                {
                    foreach (var file in Directory.GetFiles(dataFolder, "*", SearchOption.AllDirectories))
                    {
                        if (string.Equals(Path.GetExtension(file), ".mg", StringComparison.OrdinalIgnoreCase)) continue;
                        var relative = Path.GetRelativePath(dataFolder, file);
                        var copy = Path.GetFullPath(Path.Combine(stage, relative));
                        if (!IsWithin(stage, copy)) throw new InvalidDataException("Een lokaal bestand staat buiten de verwachte gegevensmap.");
                        Directory.CreateDirectory(Path.GetDirectoryName(copy)!);
                        File.Copy(file, copy, overwrite: false);
                    }
                }

                var oldHashes = new Dictionary<string, string>(StringComparer.Ordinal);
                if (Directory.Exists(dataFolder))
                {
                    foreach (var file in Directory.GetFiles(dataFolder, "*.mg", SearchOption.TopDirectoryOnly))
                    {
                        var bytes = File.ReadAllBytes(file);
                        try { oldHashes[Path.GetFileName(file)] = Convert.ToHexString(SHA256.HashData(bytes)); }
                        finally { CryptographicOperations.ZeroMemory(bytes); }
                    }
                }

                foreach (var entry in archive.Records)
                    File.WriteAllBytes(Path.Combine(stage, Path.GetFileName(entry.Key)), entry.Value);

                var newHashes = archive.Records.ToDictionary(
                    entry => Path.GetFileName(entry.Key),
                    entry => Convert.ToHexString(SHA256.HashData(entry.Value)),
                    StringComparer.Ordinal);
                var changed = 0;
                foreach (var name in oldHashes.Keys.Union(newHashes.Keys, StringComparer.Ordinal))
                    if (!oldHashes.TryGetValue(name, out var oldHash) || !newHashes.TryGetValue(name, out var newHash) ||
                        !string.Equals(oldHash, newHash, StringComparison.Ordinal)) changed++;

                if (Directory.Exists(dataFolder))
                {
                    Directory.Move(dataFolder, rollback);
                    movedOld = true;
                }
                try { Directory.Move(stage, dataFolder); }
                catch
                {
                    if (movedOld && !Directory.Exists(dataFolder)) Directory.Move(rollback, dataFolder);
                    movedOld = false;
                    throw;
                }

                if (movedOld)
                {
                    try { SecureDeleteDirectory(rollback); } catch { /* New data is already committed; cleanup can be retried later. */ }
                }
                movedOld = false;
                return changed;
            }
            finally
            {
                try { if (Directory.Exists(stage)) SecureDeleteDirectory(stage); } catch { }
                if (movedOld && Directory.Exists(rollback) && !Directory.Exists(dataFolder))
                {
                    try { Directory.Move(rollback, dataFolder); } catch { }
                }
            }
        }
        finally { WipeArchive(archive); }
    }

    static Archive ReadArchive(string path, VaultStore store, string vaultId)
    {
        using var zip = ZipFile.OpenRead(path);
        if (zip.Entries.Count == 0 || zip.Entries.Count > MaxEntries || new FileInfo(path).Length > MaxArchiveBytes)
            throw new InvalidDataException("De cloudback-up is leeg, te groot of bevat te veel bestanden.");
        byte[]? accountBytes = null;
        string? accountPath = null;
        var records = new Dictionary<string, byte[]>(StringComparer.Ordinal);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        long totalBytes = 0;
        try
        {
            foreach (var entry in zip.Entries)
            {
                var name = entry.FullName;
                if (string.IsNullOrWhiteSpace(name) || name.Contains('\\') || name.StartsWith('/') ||
                    name.Split('/').Any(part => part is "." or ".."))
                    throw new InvalidDataException("De cloudback-up bevat een onveilige bestandsnaam.");
                if (!seen.Add(name)) throw new InvalidDataException("De cloudback-up bevat dubbele bestandsnamen.");
                totalBytes = checked(totalBytes + entry.Length);
                if (totalBytes > MaxArchiveBytes) throw new InvalidDataException("De cloudback-up is te groot.");

                var accountMatch = AccountPath.Match(name);
                if (accountMatch.Success)
                {
                    if (accountPath is not null || !string.Equals(accountMatch.Groups["hash"].Value, vaultId, StringComparison.OrdinalIgnoreCase) || entry.Length > 2 * 1024 * 1024)
                        throw new InvalidDataException("De cloudback-up hoort niet bij het ingelogde MoodPress-account.");
                    accountPath = name;
                    accountBytes = ReadEntry(entry, 2 * 1024 * 1024);
                    continue;
                }

                var dataMatch = DataPath.Match(name);
                if (!dataMatch.Success || !string.Equals(dataMatch.Groups["hash"].Value, vaultId, StringComparison.OrdinalIgnoreCase) || entry.Length > MaxEntryBytes)
                    throw new InvalidDataException("De cloudback-up bevat een ongeldig of verkeerd gekoppeld gegevensrecord.");
                var kind = dataMatch.Groups["kind"].Value;
                if (!Guid.TryParseExact(dataMatch.Groups["id"].Value, "N", out var id))
                    throw new InvalidDataException("Een gegevensrecord heeft een ongeldige ID.");
                var bytes = ReadEntry(entry, checked((int)MaxEntryBytes));
                if (!store.IsEncryptedRecordForCurrentKey(kind, id, bytes))
                {
                    CryptographicOperations.ZeroMemory(bytes);
                    throw new CryptographicException("De cloudback-up bevat een record dat niet bij de huidige kluissleutel hoort.");
                }
                records.Add(name, bytes);
            }

            if (accountPath is null || accountBytes is null)
                throw new InvalidDataException("De cloudback-up mist het huidige accountbestand.");
            return new Archive(accountPath, accountBytes, records, Fingerprint(records));
        }
        catch
        {
            if (accountBytes is not null) CryptographicOperations.ZeroMemory(accountBytes);
            foreach (var value in records.Values) CryptographicOperations.ZeroMemory(value);
            throw;
        }
    }

    static byte[] ReadEntry(ZipArchiveEntry entry, int maxBytes)
    {
        if (entry.Length < 0 || entry.Length > maxBytes) throw new InvalidDataException("Een bestand in de cloudback-up is te groot.");
        using var input = entry.Open();
        using var output = new MemoryStream((int)entry.Length);
        var buffer = new byte[81920];
        int read;
        while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
        {
            if (output.Length + read > maxBytes) throw new InvalidDataException("Een bestand in de cloudback-up is groter dan toegestaan.");
            output.Write(buffer, 0, read);
        }
        if (output.Length != entry.Length) throw new InvalidDataException("Een bestand in de cloudback-up is onvolledig.");
        return output.ToArray();
    }

    static void WriteArchive(string path, string accountPath, byte[] accountBytes, Dictionary<string, byte[]> records)
    {
        using var zip = ZipFile.Open(path, ZipArchiveMode.Create);
        var account = zip.CreateEntry(accountPath, CompressionLevel.Optimal);
        using (var stream = account.Open()) stream.Write(accountBytes);
        foreach (var item in records.OrderBy(p => p.Key, StringComparer.Ordinal))
        {
            var entry = zip.CreateEntry(item.Key, CompressionLevel.Optimal);
            using var stream = entry.Open();
            stream.Write(item.Value);
        }
    }

    static string Fingerprint(Dictionary<string, byte[]> records)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var item in records.OrderBy(p => p.Key, StringComparer.Ordinal))
        {
            var pathBytes = Encoding.UTF8.GetBytes(item.Key);
            var contentHash = SHA256.HashData(item.Value);
            hash.AppendData(pathBytes);
            hash.AppendData(new byte[] { 0 });
            hash.AppendData(contentHash);
            CryptographicOperations.ZeroMemory(pathBytes);
            CryptographicOperations.ZeroMemory(contentHash);
        }
        return Convert.ToHexString(hash.GetHashAndReset());
    }

    static bool Same(byte[]? left, byte[]? right)
    {
        if (ReferenceEquals(left, right)) return true;
        if (left is null || right is null || left.Length != right.Length) return false;
        var leftHash = SHA256.HashData(left);
        var rightHash = SHA256.HashData(right);
        try { return CryptographicOperations.FixedTimeEquals(leftHash, rightHash); }
        finally { CryptographicOperations.ZeroMemory(leftHash); CryptographicOperations.ZeroMemory(rightHash); }
    }

    static void WipeArchive(Archive archive)
    {
        CryptographicOperations.ZeroMemory(archive.AccountBytes);
        foreach (var record in archive.Records.Values) CryptographicOperations.ZeroMemory(record);
    }

    static bool IsWithin(string root, string target)
    {
        var relative = Path.GetRelativePath(Path.GetFullPath(root), Path.GetFullPath(target));
        return !Path.IsPathRooted(relative) && relative != ".." && !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    static void SecureDeleteDirectory(string directory)
    {
        if (!Directory.Exists(directory)) return;
        foreach (var file in Directory.GetFiles(directory, "*", SearchOption.AllDirectories))
            try { VaultStore.SecureDelete(file); } catch { try { File.Delete(file); } catch { } }
        Directory.Delete(directory, recursive: true);
    }
}
