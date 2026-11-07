// Kitbridge setup and uninstaller, one .exe with the plugin embedded as payload.zip.
// Built by dev/build_installer.py with the C# compiler that ships with Windows (C# 5).
// Runs without admin rights: everything it touches is inside the user's profile.
//
//   KitbridgeSetup.exe                          window, install or update
//   KitbridgeSetup.exe /uninstall               window, remove
//   /silent  no window, exit code 0 ok, 1 failed, 2 Cinema 4D is running
//   /purge   with /uninstall: also remove downloaded assets, settings and the login
//   /plugins=<dir> /state=<dir> /data=<dir> /noregister   sandbox, used by dev/check_installer.py

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Kitbridge Setup")]
[assembly: AssemblyProduct("Kitbridge")]
[assembly: AssemblyVersion(BuildInfo.Version + ".0")]

static class Text
{
    static readonly bool Ru = new[] { "ru", "uk", "be" }.Contains(CultureInfo.CurrentUICulture.TwoLetterISOLanguageName);
    public static string L(string ru, string en) { return Ru ? ru : en; }
}

class Options
{
    public bool Uninstall, Silent, Purge, NoRegister;
    public string Plugins = "", State = "", Data = "";

    public static Options Parse(string[] args)
    {
        var o = new Options();
        foreach (var a in args)
        {
            var low = a.ToLowerInvariant();
            if (low == "/uninstall") o.Uninstall = true;
            else if (low == "/silent") o.Silent = true;
            else if (low == "/purge") o.Purge = true;
            else if (low == "/noregister") o.NoRegister = true;
            else if (low.StartsWith("/plugins=")) o.Plugins = a.Substring(9);
            else if (low.StartsWith("/state=")) o.State = a.Substring(7);
            else if (low.StartsWith("/data=")) o.Data = a.Substring(6);
        }
        return o;
    }

    public bool Sandboxed { get { return Plugins != "" || Data != ""; } }

    public string StateDir
    {
        get
        {
            return State != "" ? State
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Kitbridge");
        }
    }

    public string DataDir
    {
        get
        {
            if (Data != "") return Data;
            var env = (Environment.GetEnvironmentVariable("KITBRIDGE_GLOBAL_DIR") ?? "").Trim();
            return env != "" ? env
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "blenderkit_data");
        }
    }
}

static class Core
{
    const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\Kitbridge";
    const string CredentialTarget = "Kitbridge:tokens";
    static readonly string[] AssetFolders = { "models", "materials", "hdris", "other" };

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredDeleteW(string target, int type, int flags);

    public static bool CinemaRunning()
    {
        return Process.GetProcesses().Any(p => p.ProcessName.StartsWith("Cinema 4D", StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>Plugins folders of every Cinema 4D that has run, most recently used first.</summary>
    public static List<string> FindPluginDirs()
    {
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Maxon");
        if (!Directory.Exists(root)) return new List<string>();
        // A settings folder has "prefs" inside; _p _x _c _w are helper apps (Commandline, Team Render...).
        return new DirectoryInfo(root).GetDirectories()
            .Where(d => Regex.IsMatch(d.Name, "^(Maxon )?Cinema 4D") && !Regex.IsMatch(d.Name, "_[pxcw]$")
                        && Directory.Exists(Path.Combine(d.FullName, "prefs")))
            .OrderByDescending(d => d.LastWriteTime)
            .Select(d => Path.Combine(d.FullName, "plugins"))
            .ToList();
    }

    public static string Describe(string pluginsDir)
    {
        var settings = Path.GetDirectoryName(pluginsDir);
        var when = Directory.GetLastWriteTime(settings).ToShortDateString();
        var name = Path.GetFileName(settings).Replace("_", " · ");
        return name + "  ·  " + Text.L("запускалась ", "last used ") + when;
    }

    public static bool HasPlugin(string pluginsDir)
    {
        return File.Exists(Path.Combine(pluginsDir, "kitbridge", "kitbridge.pyp"));
    }

    /// <summary>Removes a folder even if it holds read-only files; a file in use throws.</summary>
    static void DeleteTree(string path)
    {
        if (!Directory.Exists(path)) return;
        foreach (var f in Directory.GetFiles(path, "*", SearchOption.AllDirectories))
            File.SetAttributes(f, FileAttributes.Normal);
        for (int attempt = 0; ; attempt++)
        {
            try { Directory.Delete(path, true); return; }
            catch (IOException) { if (attempt >= 4) throw; Thread.Sleep(400); }
            catch (UnauthorizedAccessException) { if (attempt >= 4) throw; Thread.Sleep(400); }
        }
    }

    public static void Install(Options o, List<string> targets, Action<double> progress, List<string> skipped)
    {
        var installed = new List<string>();
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
        {
            var entries = zip.Entries.Where(e => e.FullName.StartsWith("kitbridge/") && !e.FullName.EndsWith("/")).ToList();
            int total = Math.Max(1, entries.Count * targets.Count), done = 0;
            foreach (var plugins in targets)
            {
                var dest = Path.Combine(plugins, "kitbridge");
                if (Directory.Exists(dest) && !HasPlugin(plugins))
                {
                    skipped.Add(dest);  // somebody else's folder: never replace it
                    done += entries.Count;
                    continue;
                }
                // Unpack beside it first, so a failure leaves the working version alone.
                var fresh = Path.GetFullPath(dest + ".kbnew");
                DeleteTree(fresh);
                foreach (var entry in entries)
                {
                    var path = Path.GetFullPath(Path.Combine(fresh, entry.FullName.Substring("kitbridge/".Length).Replace('/', '\\')));
                    if (!path.StartsWith(fresh + "\\", StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Bad path in the installer: " + entry.FullName);
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using (var from = entry.Open())
                    using (var to = File.Create(path))
                        from.CopyTo(to);
                    progress((double)(++done) / total);
                }
                DeleteTree(dest);
                Directory.Move(fresh, dest);
                installed.Add(plugins);
            }
        }
        if (installed.Count > 0) Register(o, installed);
    }

    static string TargetsFile(Options o) { return Path.Combine(o.StateDir, "targets.txt"); }

    static List<string> RecordedTargets(Options o)
    {
        var file = TargetsFile(o);
        return File.Exists(file) ? File.ReadAllLines(file).Where(l => l.Trim() != "").ToList() : new List<string>();
    }

    static void Register(Options o, List<string> installed)
    {
        Directory.CreateDirectory(o.StateDir);
        var uninstaller = Path.Combine(o.StateDir, "uninstall.exe");
        var self = Assembly.GetExecutingAssembly().Location;
        if (!string.Equals(Path.GetFullPath(self), Path.GetFullPath(uninstaller), StringComparison.OrdinalIgnoreCase))
            File.Copy(self, uninstaller, true);
        File.WriteAllLines(TargetsFile(o), RecordedTargets(o).Union(installed, StringComparer.OrdinalIgnoreCase));
        if (o.NoRegister) return;
        using (var key = Registry.CurrentUser.CreateSubKey(UninstallKey))
        {
            key.SetValue("DisplayName", "Kitbridge for Cinema 4D");
            key.SetValue("DisplayVersion", BuildInfo.Version);
            key.SetValue("Publisher", "Kitbridge");
            key.SetValue("DisplayIcon", uninstaller);
            key.SetValue("InstallLocation", o.StateDir);
            key.SetValue("UninstallString", "\"" + uninstaller + "\" /uninstall");
            key.SetValue("QuietUninstallString", "\"" + uninstaller + "\" /uninstall /silent");
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            key.SetValue("EstimatedSize", (int)(new FileInfo(uninstaller).Length / 512), RegistryValueKind.DWord);
        }
    }

    public static bool IsRegistered(Options o)
    {
        if (o.NoRegister) return File.Exists(TargetsFile(o));
        using (var key = Registry.CurrentUser.OpenSubKey(UninstallKey))
            return key != null;
    }

    public static List<string> InstalledTargets(Options o)
    {
        IEnumerable<string> all = RecordedTargets(o);
        if (o.Plugins != "") all = all.Concat(new[] { o.Plugins });
        else if (!o.Sandboxed) all = all.Concat(FindPluginDirs());
        return all.Distinct(StringComparer.OrdinalIgnoreCase).Where(HasPlugin).ToList();
    }

    /// <summary>Folders Kitbridge made under the download folder, the same test as core/paths.py.</summary>
    static List<string> CustomPackages(string root)
    {
        var found = new List<string>();
        foreach (var parent in AssetFolders.Select(f => Path.Combine(root, f)).Concat(new[] { root }))
        {
            if (!Directory.Exists(parent)) continue;
            found.AddRange(Directory.GetDirectories(parent).Where(d =>
                Regex.IsMatch(Path.GetFileName(d), @"^.+_(?:\d+|ORIGINAL)$")
                && (File.Exists(Path.Combine(d, "kitbridge.json")) || Directory.Exists(Path.Combine(d, "source")))));
        }
        return found;
    }

    static string CustomDownloadDir(Options o)
    {
        try
        {
            var text = File.ReadAllText(Path.Combine(o.DataDir, "kitbridge", "prefs.json"));
            var m = Regex.Match(text, "\"download_dir\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
            return m.Success ? Regex.Unescape(m.Groups[1].Value) : "";
        }
        catch (IOException) { return ""; }
        catch (UnauthorizedAccessException) { return ""; }
    }

    /// <summary>What /purge removes. Never the rest of blenderkit_data: that is the Blender add-on's.</summary>
    static List<string> PurgeFolders(Options o)
    {
        var list = new List<string> { Path.Combine(o.DataDir, "kitbridge"), Path.Combine(o.DataDir, "kitbridge_cache") };
        var custom = CustomDownloadDir(o);
        if (custom != "" && Directory.Exists(custom)) list.AddRange(CustomPackages(custom));
        return list;
    }

    public static long PurgeSize(Options o)
    {
        long size = 0;
        foreach (var dir in PurgeFolders(o).Where(Directory.Exists))
        {
            try { size += new DirectoryInfo(dir).GetFiles("*", SearchOption.AllDirectories).Sum(f => f.Length); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        return size;
    }

    public static void Uninstall(Options o, bool purge)
    {
        var custom = CustomDownloadDir(o);
        foreach (var plugins in InstalledTargets(o))
            DeleteTree(Path.Combine(plugins, "kitbridge"));
        if (purge)
        {
            foreach (var dir in PurgeFolders(o))
                DeleteTree(dir);
            if (custom != "" && Directory.Exists(custom))
                foreach (var name in AssetFolders)
                {
                    var sub = Path.Combine(custom, name);
                    if (Directory.Exists(sub) && !Directory.EnumerateFileSystemEntries(sub).Any())
                        Directory.Delete(sub);
                }
            if (!o.Sandboxed) CredDeleteW(CredentialTarget, 1, 0);
        }
        if (!o.NoRegister) Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
        RemoveStateDir(o);
    }

    static void RemoveStateDir(Options o)
    {
        if (!Directory.Exists(o.StateDir)) return;
        var self = Path.GetDirectoryName(Path.GetFullPath(Assembly.GetExecutingAssembly().Location));
        if (!string.Equals(self, Path.GetFullPath(o.StateDir).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
        {
            DeleteTree(o.StateDir);
            return;
        }
        // A running .exe can't delete itself: leave that to a hidden shell once this process has exited.
        Process.Start(new ProcessStartInfo("cmd.exe",
            "/c ping -n 3 127.0.0.1 >nul & rmdir /s /q \"" + o.StateDir + "\"")
        { CreateNoWindow = true, UseShellExecute = false, WorkingDirectory = Path.GetTempPath() });
    }
}

class SetupForm : Form
{
    static readonly Color Bg = ColorTranslator.FromHtml("#262626"), Field = ColorTranslator.FromHtml("#1D1D1D"),
        Panel_ = ColorTranslator.FromHtml("#2E2E2E"), Line = ColorTranslator.FromHtml("#424242"),
        Ink = ColorTranslator.FromHtml("#E2E2E2"), Muted = ColorTranslator.FromHtml("#8E8E8E"),
        Accent = ColorTranslator.FromHtml("#4D7FD6"), Warn = ColorTranslator.FromHtml("#E8A23C"),
        Bad = ColorTranslator.FromHtml("#E5645A"), Good = ColorTranslator.FromHtml("#5BC46A");

    readonly Options opts;
    bool uninstallMode, busy, finished;
    readonly Label heading, note, warning;
    readonly CheckedListBox targets;
    readonly CheckBox purge;
    readonly Panel track, fill;
    readonly Button primary, secondary, removeButton;
    readonly System.Windows.Forms.Timer timer;

    public SetupForm(Options options)
    {
        opts = options;
        uninstallMode = options.Uninstall;
        SuspendLayout();
        Font = new Font("Segoe UI", 9.5f);
        Text = "Kitbridge " + BuildInfo.Version;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Bg;
        ForeColor = Ink;
        ClientSize = new Size(520, 346);
        try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch (ArgumentException) { }

        var band = new Panel { Bounds = new Rectangle(0, 0, 520, 64), BackColor = Accent };
        band.Controls.Add(new Label
        {
            Text = "Kitbridge",
            Font = new Font("Segoe UI Semibold", 17f),
            ForeColor = Color.White,
            Bounds = new Rectangle(18, 10, 300, 44),
            TextAlign = ContentAlignment.MiddleLeft
        });
        band.Controls.Add(new Label
        {
            Text = BuildInfo.Version,
            ForeColor = Color.White,
            Bounds = new Rectangle(402, 10, 100, 44),
            TextAlign = ContentAlignment.MiddleRight
        });
        Controls.Add(band);

        heading = Make(new Rectangle(18, 78, 484, 24), Ink);
        targets = new CheckedListBox
        {
            Bounds = new Rectangle(18, 106, 484, 84),
            BackColor = Field,
            ForeColor = Ink,
            BorderStyle = BorderStyle.None,
            CheckOnClick = true,
            IntegralHeight = false
        };
        // ItemCheck fires before the new state applies and also while the list is being filled.
        targets.ItemCheck += delegate { if (IsHandleCreated) BeginInvoke((Action)Refresh_); };
        Shown += delegate { Refresh_(); };
        Controls.Add(targets);
        purge = new CheckBox
        {
            Bounds = new Rectangle(18, 196, 484, 40),
            ForeColor = Ink,
            FlatStyle = FlatStyle.Standard,
            TextAlign = ContentAlignment.TopLeft,
            CheckAlign = ContentAlignment.TopLeft,
            Visible = false
        };
        Controls.Add(purge);
        note = Make(new Rectangle(18, 196, 484, 44), Muted);
        warning = Make(new Rectangle(18, 244, 484, 22), Warn);
        track = new Panel { Bounds = new Rectangle(18, 272, 484, 5), BackColor = Line, Visible = false };
        fill = new Panel { Bounds = new Rectangle(0, 0, 0, 5), BackColor = Accent };
        track.Controls.Add(fill);
        Controls.Add(track);

        primary = MakeButton(new Rectangle(382, 292, 120, 36), Accent, Color.White);
        secondary = MakeButton(new Rectangle(270, 292, 104, 36), Panel_, Ink);
        removeButton = MakeButton(new Rectangle(18, 292, 104, 36), Panel_, Ink);
        removeButton.Text = Text_("Удалить", "Uninstall");
        primary.Click += delegate { if (finished) Close(); else Start(); };
        secondary.Click += delegate { Close(); };
        removeButton.Click += delegate { uninstallMode = true; ShowPage(); };
        AcceptButton = primary;

        AutoScaleDimensions = new SizeF(96f, 96f);
        AutoScaleMode = AutoScaleMode.Dpi;
        ResumeLayout(false);
        PerformLayout();

        timer = new System.Windows.Forms.Timer { Interval = 1000 };
        timer.Tick += delegate { Refresh_(); };
        timer.Start();
        FormClosing += (s, e) => { if (busy) e.Cancel = true; };
        ShowPage();
    }

    static string Text_(string ru, string en) { return global::Text.L(ru, en); }

    // Bounds set in the constructor are DPI-scaled by AutoScaleMode.Dpi; anything moved later must
    // be scaled the same way or it lands on top of other controls at 125 %/150 % display scaling.
    // The heading was created 484 logical px wide, so its real width gives the factor.
    int S(int logical) { return (int)Math.Round(logical * (heading.Width / 484f)); }

    Label Make(Rectangle r, Color color)
    {
        var l = new Label { Bounds = r, ForeColor = color, BackColor = Color.Transparent };
        Controls.Add(l);
        return l;
    }

    Button MakeButton(Rectangle r, Color back, Color ink)
    {
        var b = new Button { Bounds = r, BackColor = back, ForeColor = ink, FlatStyle = FlatStyle.Flat, UseVisualStyleBackColor = false };
        b.FlatAppearance.BorderColor = back == Accent ? back : Line;
        Controls.Add(b);
        return b;
    }

    void ShowPage()
    {
        secondary.Text = Text_("Отмена", "Cancel");
        targets.Items.Clear();
        if (uninstallMode)
        {
            var found = Core.InstalledTargets(opts);
            heading.Text = found.Count > 0 ? Text_("Плагин будет удалён из:", "The plugin will be removed from:")
                                           : Text_("Плагин в Cinema 4D не найден.", "The plugin was not found in Cinema 4D.");
            foreach (var t in found) targets.Items.Add(Core.Describe(t), true);
            targets.Enabled = false;
            purge.Visible = true;
            purge.Text = Text_("Также удалить скачанные ассеты, настройки и вход в аккаунт",
                               "Also remove downloaded assets, settings and the login");
            note.Top = S(240);
            note.Height = S(22);
            warning.Top = S(262);
            note.Text = Text_("Файлы аддона BlenderKit для Блендера не затрагиваются.", "The BlenderKit add-on for Blender is left alone.");
            primary.Text = Text_("Удалить", "Uninstall");
            removeButton.Visible = false;
            var worker = new Thread(() =>
            {
                var size = Core.PurgeSize(opts);
                try { BeginInvoke((Action)(() => purge.Text += " (" + FormatSize(size) + ")")); }
                catch (InvalidOperationException) { }
            }) { IsBackground = true };
            HandleCreated += delegate { worker.Start(); };
            if (IsHandleCreated) worker.Start();
        }
        else
        {
            var found = opts.Plugins != "" ? new List<string> { opts.Plugins } : Core.FindPluginDirs();
            bool update = Core.IsRegistered(opts);
            heading.Text = found.Count > 0 ? Text_("Куда установить:", "Install into:")
                                           : Text_("Cinema 4D не найдена. Запусти её один раз, закрой и открой установщик снова.",
                                                   "Cinema 4D was not found. Start it once, close it and run this again.");
            for (int i = 0; i < found.Count; i++) targets.Items.Add(Core.Describe(found[i]), i == 0);
            targetPaths = found;
            note.Text = Text_("Модели и материалы конвертируются через Blender 4.2 или новее, он должен быть установлен. Удалить плагин можно будет в «Приложения» Windows.",
                              "Models and materials are converted by Blender 4.2 or newer, which has to be installed. You can remove the plugin later in Windows Apps.");
            primary.Text = update ? Text_("Обновить", "Update") : Text_("Установить", "Install");
            removeButton.Visible = update;
        }
        Refresh_();
    }

    List<string> targetPaths = new List<string>();

    static string FormatSize(long bytes)
    {
        if (bytes >= 1L << 30) return (bytes / (double)(1L << 30)).ToString("0.0") + Text_(" ГБ", " GB");
        return (bytes >> 20) + Text_(" МБ", " MB");
    }

    void Refresh_()
    {
        if (finished || busy) return;
        bool running = !opts.Sandboxed && Core.CinemaRunning();
        warning.Text = running ? Text_("Закрой Cinema 4D, чтобы продолжить.", "Close Cinema 4D to continue.") : "";
        primary.Enabled = !running && (uninstallMode || targets.CheckedItems.Count > 0);
    }

    void Start()
    {
        busy = true;
        primary.Enabled = secondary.Enabled = removeButton.Enabled = targets.Enabled = purge.Enabled = false;
        warning.Text = "";
        var skipped = new List<string>();
        var chosen = uninstallMode ? new List<string>()
            : targets.CheckedIndices.Cast<int>().Select(i => targetPaths[i]).ToList();
        bool wipe = purge.Checked, removing = uninstallMode;
        track.Visible = !removing;
        var worker = new Thread(() =>
        {
            string error = null;
            try
            {
                if (removing) Core.Uninstall(opts, wipe);
                else Core.Install(opts, chosen, f => BeginInvoke((Action)(() => fill.Width = (int)(track.Width * f))), skipped);
            }
            catch (Exception ex) { error = ex.Message; }
            BeginInvoke((Action)(() => Finish(error, skipped, removing)));
        }) { IsBackground = true };
        worker.Start();
    }

    void Finish(string error, List<string> skipped, bool removing)
    {
        busy = false;
        finished = true;
        targets.Visible = purge.Visible = track.Visible = secondary.Visible = removeButton.Visible = false;
        warning.Text = "";
        primary.Enabled = true;
        primary.Text = Text_("Закрыть", "Close");
        heading.Font = new Font(Font.FontFamily, 13f);
        heading.Height = S(30);
        note.Top = heading.Bottom + S(10);
        note.Height = primary.Top - note.Top - S(12);
        if (error != null)
        {
            heading.ForeColor = Bad;
            heading.Text = Text_("Не получилось", "That failed");
            note.ForeColor = Ink;
            note.Text = error;
            return;
        }
        heading.ForeColor = Good;
        if (removing)
        {
            heading.Text = Text_("Kitbridge удалён", "Kitbridge was removed");
            note.Text = purge.Checked ? Text_("Плагин, скачанные ассеты, настройки и вход в аккаунт удалены.", "The plugin, downloaded assets, settings and the login are gone.")
                : Text_("Скачанные ассеты и вход в аккаунт сохранены: при повторной установке они подхватятся сами.", "Downloaded assets and the login were kept and will be picked up if you install again.");
            return;
        }
        heading.Text = skipped.Count > 0 && targets.CheckedItems.Count == skipped.Count
            ? Text_("Ничего не установлено", "Nothing was installed") : Text_("Готово", "Done");
        if (skipped.Count > 0 && targets.CheckedItems.Count == skipped.Count) heading.ForeColor = Warn;
        note.ForeColor = Ink;
        note.Text = Text_("Запусти Cinema 4D: плагин появится в Extensions > Kitbridge и в главном меню.",
                          "Start Cinema 4D: the plugin is under Extensions > Kitbridge and in the main menu.");
        if (skipped.Count > 0)
            note.Text = Text_("Пропущено, эта папка не от Kitbridge: ", "Skipped, this folder is not Kitbridge: ") + string.Join(", ", skipped)
                        + "\n\n" + note.Text;
    }
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        var o = Options.Parse(args);
        if (!o.Silent)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm(o));
            return 0;
        }
        if (!o.Sandboxed && Core.CinemaRunning()) return 2;
        try
        {
            if (o.Uninstall) { Core.Uninstall(o, o.Purge); return 0; }
            var dirs = o.Plugins != "" ? new List<string> { o.Plugins } : Core.FindPluginDirs().Take(1).ToList();
            if (dirs.Count == 0) return 1;
            var skipped = new List<string>();
            Core.Install(o, dirs, f => { }, skipped);
            return skipped.Count == 0 ? 0 : 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex);
            return 1;
        }
    }
}
