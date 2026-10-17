#!/usr/bin/env python3
"""Harden the MoodPress 4.0 Android feature directory and restore visible core shortcuts."""
from pathlib import Path
import re
import sys

if len(sys.argv) != 2:
    raise SystemExit("Usage: fix-moodpress-feature-directory.py <source-root>")

root = Path(sys.argv[1])
app = root / "MijnGevoel.Android"
more_path = app / "MorePage.cs"
feature_path = app / "FeaturePage.cs"
checks_path = root / "scripts" / "check-moodpress-4.sh"

for required in (more_path, feature_path):
    if not required.is_file():
        raise SystemExit(f"Required source file not found: {required}")

more = more_path.read_text(encoding="utf-8")

# Build the directory from the catalogue itself; Home shortcut visibility must never
# hide features from this dedicated page.
all_pos = more.find("var all = FeatureCatalog.All;")
if all_pos < 0:
    raise SystemExit("MorePage.cs: FeatureCatalog.All lookup not found.")
matches_pos = more.find("var matches =", all_pos)
if matches_pos < 0:
    raise SystemExit("MorePage.cs: function filtering block not found.")
to_array_pos = more.find(".ToArray();", matches_pos)
if to_array_pos < 0:
    raise SystemExit("MorePage.cs: end of function filtering block not found.")
replacement = "var matches = all.Where(item => string.IsNullOrWhiteSpace(query) || MatchesSearch(item, query)).ToArray();"
more = more[:matches_pos] + replacement + more[to_array_pos + len(".ToArray();"):]

# Make every function discoverable through the search box, including speech controls,
# which live inside the diary and free-writing screens.
search_helper = r'''
    static bool IsVoiceSearch(string query)
    {
        var term = (query ?? string.Empty).Trim();
        return new[] { "inspreken", "spraak", "spraakbericht", "opname", "opnemen", "microfoon", "audio", "geluid" }
            .Any(alias => alias.Contains(term, StringComparison.CurrentCultureIgnoreCase)
                || term.Contains(alias, StringComparison.CurrentCultureIgnoreCase));
    }

    static bool MatchesSearch(FeatureCatalogItem item, string query)
    {
        if (item.Label.Contains(query, StringComparison.CurrentCultureIgnoreCase)
            || item.Group.Contains(query, StringComparison.CurrentCultureIgnoreCase)
            || item.Description.Contains(query, StringComparison.CurrentCultureIgnoreCase))
            return true;

        // Voice notes are part of the diary/free-writing screen, so expose them under their own search terms.
        return item.Kind is FeatureKind.Journal or FeatureKind.Notes && IsVoiceSearch(query);
    }

'''
if "static bool MatchesSearch(FeatureCatalogItem item, string query)" not in more:
    marker = "    void RenderResults()\n"
    if marker not in more:
        raise SystemExit("MorePage.cs: RenderResults marker not found.")
    more = more.replace(marker, search_helper + marker, 1)

# Add persistent, obvious entry points for the four core activities.
quick_links = r'''
        var quickLinks = new Grid { ColumnSpacing = 8, RowSpacing = 8 };
        quickLinks.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        quickLinks.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        quickLinks.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
        quickLinks.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
        quickLinks.Add(AppUi.AsyncButton("🎨 Tekenen", async () => await OpenFeatureAsync(FeatureKind.Draw), quiet: true), 0, 0);
        quickLinks.Add(AppUi.AsyncButton("📝 Van me afschrijven", async () => await OpenFeatureAsync(FeatureKind.Notes), quiet: true), 1, 0);
        quickLinks.Add(AppUi.AsyncButton("🎙 Inspreken", async () => await OpenFeatureAsync(FeatureKind.Journal, focusVoiceCapture: true), quiet: true), 0, 1);
        quickLinks.Add(AppUi.AsyncButton("📓 Dagboek", async () => await OpenFeatureAsync(FeatureKind.Journal), quiet: true), 1, 1);
'''
if "var quickLinks = new Grid" not in more:
    marker = "        _search.TextChanged += (_, _) =>\n"
    if marker not in more:
        raise SystemExit("MorePage.cs: search TextChanged handler not found.")
    more = more.replace(marker, quick_links + "\n" + marker, 1)

# Re-render on every text change; provide explicit Zoek and Wis actions too.
text_changed = re.compile(r"        _search\.TextChanged \+= \(_, _\) =>\s*\{.*?\n        \};", re.S)
search_event = r'''        _search.TextChanged += (_, _) =>
        {
            quickLinks.IsVisible = string.IsNullOrWhiteSpace(_search.Text);
            RenderResultsSafely(force: true);
        };'''
more, count = text_changed.subn(search_event, more, count=1)
if count != 1:
    raise SystemExit("MorePage.cs: could not replace the search change handler.")

if "var searchRow = new Grid" not in more:
    marker = "        _results = new VerticalStackLayout { Spacing = 12 };\n"
    search_row = r'''        var searchRow = new Grid { ColumnSpacing = 6 };
        searchRow.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        searchRow.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Auto));
        searchRow.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Auto));
        searchRow.Add(_search, 0, 0);
        searchRow.Add(AppUi.Button("Zoek", () => RenderResultsSafely(force: true), quiet: true), 1, 0);
        searchRow.Add(AppUi.Button("Wis", () => _search.Text = string.Empty, quiet: true), 2, 0);
'''
    if marker not in more:
        raise SystemExit("MorePage.cs: results layout marker not found.")
    more = more.replace(marker, search_row + marker, 1)

# Include the quick links in the page shell and make Voice searches open directly at recording.
more = more.replace(
    "Children = { header, _search, _results }",
    "Children = { header, searchRow, quickLinks, _results }",
    1
)
more = more.replace(
    "grid.Add(CreateCard(item), i % columns, i / columns);",
    "grid.Add(CreateCard(item, focusVoiceCapture: IsVoiceSearch(query)), i % columns, i / columns);",
    1
)
more = more.replace(
    "Border CreateCard(FeatureCatalogItem item)",
    "Border CreateCard(FeatureCatalogItem item, bool focusVoiceCapture = false)",
    1
)
more = more.replace(
    "async () => await OpenFeatureAsync(item.Kind), quiet: true",
    "async () => await OpenFeatureAsync(item.Kind, focusVoiceCapture), quiet: true",
    1
)
more = more.replace(
    "async Task OpenFeatureAsync(FeatureKind kind)",
    "async Task OpenFeatureAsync(FeatureKind kind, bool focusVoiceCapture = false)",
    1
)
more = more.replace(
    "default: await Navigation.PushAsync(new FeaturePage(kind)); break;",
    "default: await Navigation.PushAsync(new FeaturePage(kind, focusVoiceCapture: focusVoiceCapture)); break;",
    1
)
if "Children = { header, searchRow, quickLinks, _results }" not in more:
    raise SystemExit("MorePage.cs: quick links/search row not included in the rendered page.")
if "FeatureVisibilityService.IsEnabled(AppSession.Settings, item.Kind.ToString())" in more:
    raise SystemExit("MorePage.cs still hides catalog features based on visibility settings.")
more_path.write_text(more, encoding="utf-8")

# Allow the voice shortcut to bring the recording control into view without changing saved data.
feature = feature_path.read_text(encoding="utf-8")
if "bool _focusVoiceCapture;" not in feature:
    feature = feature.replace(
        "    bool _stoppingVoiceRecording;\n",
        "    bool _stoppingVoiceRecording;\n    bool _focusVoiceCapture;\n    ScrollView? _pageScrollView;\n",
        1
    )
feature = feature.replace(
    "public FeaturePage(FeatureKind kind, Guid? targetShareId = null, Guid? editingMoodCheckInId = null)\n    {\n        _kind = kind;",
    "public FeaturePage(FeatureKind kind, Guid? targetShareId = null, Guid? editingMoodCheckInId = null, bool focusVoiceCapture = false)\n    {\n        _kind = kind;\n        _focusVoiceCapture = focusVoiceCapture;",
    1
)
feature = feature.replace(
    "        Content = new ScrollView { Content = _root };\n        Build();",
    "        _pageScrollView = new ScrollView { Content = _root };\n        Content = _pageScrollView;\n        Build();",
    1
)
if "_focusVoiceCapture" in feature and "FocusVoiceCaptureAsync" not in feature:
    feature = feature.replace(
        "        AppSession.Touch();\n        if (_kind == FeatureKind.Timer)",
        "        AppSession.Touch();\n        if (_focusVoiceCapture) _ = FocusVoiceCaptureAsync();\n        if (_kind == FeatureKind.Timer)",
        1
    )
    marker = "    protected override void OnDisappearing()\n"
    focus_method = r'''    async Task FocusVoiceCaptureAsync()
    {
        _focusVoiceCapture = false;
        try
        {
            await Task.Delay(180);
            if (_pageScrollView is not null && _voiceRecordButton is not null)
                await _pageScrollView.ScrollToAsync(_voiceRecordButton, ScrollToPosition.Center, true);
        }
        catch (Exception ex) { AppLog.Write(ex); }
    }

'''
    if marker not in feature:
        raise SystemExit("FeaturePage.cs: OnDisappearing marker not found.")
    feature = feature.replace(marker, focus_method + marker, 1)

required_feature_markers = [
    "bool _focusVoiceCapture;",
    "FocusVoiceCaptureAsync",
    "ScrollToAsync(_voiceRecordButton, ScrollToPosition.Center, true)",
    "focusVoiceCapture: false",
]
for marker in required_feature_markers[:3]:
    if marker not in feature:
        raise SystemExit(f"FeaturePage.cs: missing expected voice-focus change: {marker}")
feature_path.write_text(feature, encoding="utf-8")

# Add regression assertions to the project validation script.
if checks_path.is_file():
    checks = checks_path.read_text(encoding="utf-8")
    test_block = r'''
# Core activities remain visible and searchable on More; the voice shortcut focuses recording.
grep -q 'Tekenen' "$more"
grep -q 'Van me afschrijven' "$more"
grep -q 'Inspreken' "$more"
grep -q 'Dagboek' "$more"
grep -q 'searchRow, quickLinks, _results' "$more"
grep -q 'MatchesSearch(item, query)' "$more"
grep -q 'FocusVoiceCaptureAsync' "MijnGevoel.Android/FeaturePage.cs"
'''
    if "Core activities remain visible and searchable on More" not in checks:
        checks = checks.rstrip() + "\n" + test_block.strip() + "\n"
        checks_path.write_text(checks, encoding="utf-8")

print("MoodPress: full feature list, explicit search controls and core activity shortcuts repaired.")
