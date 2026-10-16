using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Collections.Generic;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

class Setup : Form {
    TextBox log = new TextBox();
    Button install = new Button();
    Button close = new Button();
    CheckBox desktop = new CheckBox();
    CheckBox launch = new CheckBox();
    bool busy;
    const string Product = "BMW DDE5 MS43 Tuner";
    const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\BMW_DDE5_MS43_Tuner";
    static string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "BMW_DDE5_MS43_Tuner");

    Setup() {
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Text = "BMW DDE5 MS43 Tuner Setup"; ClientSize = new Size(680, 470);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false;
        Label title = new Label { Text = "Install BMW DDE5 MS43 Tuner", Font = new Font("Segoe UI", 15), AutoSize = true, Left = 20, Top = 18 };
        Label description = new Label { Text = "Setup downloads a private Python runtime from python.org and libraries from PyPI.\r\nInternet access is required. System Python is unchanged; no administrator access is needed.\r\nInstalled for this Windows user, with a Start menu shortcut and an uninstaller.", Left = 20, Top = 58, Width = 640, Height = 65 };
        Label location = new Label { Text = "Location: " + root, Left = 20, Top = 129, Width = 640, Height = 32 };
        desktop.Text = "Create a desktop shortcut"; desktop.SetBounds(20, 166, 300, 24);
        launch.Text = "Launch editor when setup finishes"; launch.Checked = true; launch.SetBounds(330, 166, 330, 24);
        log.Multiline = true; log.ReadOnly = true; log.ScrollBars = ScrollBars.Vertical; log.SetBounds(20, 201, 640, 205);
        install.Text = "Install / Update"; install.SetBounds(400, 426, 145, 30);
        close.Text = "Close"; close.SetBounds(555, 426, 105, 30); close.Click += delegate { Close(); };
        install.Click += delegate { bool wantDesktop = desktop.Checked, wantLaunch = launch.Checked; busy = true; install.Enabled = close.Enabled = desktop.Enabled = launch.Enabled = false; Task.Factory.StartNew(delegate { RunInstall(wantDesktop, wantLaunch); }); };
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) { e.Cancel = true; MessageBox.Show(this, "Wait for setup to finish before closing this window."); } };
        Controls.AddRange(new Control[] { title, description, location, desktop, launch, log, install, close });
    }
    void Append(string text) { if (text != null && !IsDisposed) BeginInvoke((Action)(() => log.AppendText(text + Environment.NewLine))); }
    static void Resource(string name, string path) {
        using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        using (FileStream output = File.Create(path)) { if (input == null) throw new Exception("Setup resource missing: " + name); input.CopyTo(output); }
    }
    void RunInstall(bool wantDesktop, bool wantLaunch) {
        string scratch = Path.Combine(Path.GetTempPath(), "bmw-editor-setup-" + Guid.NewGuid().ToString("N"));
        int result = 1;
        Mutex mutex = null; bool held = false;
        try {
            mutex = new Mutex(false, @"Local\BMW_DDE5_MS43_Tuner_Setup"); held = mutex.WaitOne(0);
            if (!held) throw new Exception("Another setup is already running.");
            if (!Environment.Is64BitOperatingSystem) throw new Exception("Windows x64 is required.");
            if (AppRunning()) throw new Exception("Close the editor before updating.");
            Directory.CreateDirectory(scratch);
            string payload = Path.Combine(scratch, "payload.zip"); Resource("payload.zip", payload);
            string extracted = Path.Combine(scratch, "app"); ExtractSafe(payload, extracted);
            Dictionary<string, object> runtime = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(extracted, "installer", "runtime.json")));
            string version = (string)runtime["version"], url = (string)runtime["url"];
            if (!url.StartsWith("https://www.python.org/ftp/python/", StringComparison.Ordinal)) throw new Exception("Unexpected Python download source.");
            Directory.CreateDirectory(root);
            string runtimeRoot = Path.Combine(root, "python-" + version), python = Path.Combine(runtimeRoot, "python.exe");
            if (!File.Exists(python)) {
                Append("Downloading private Python " + version + " from python.org...");
                string download = Path.Combine(scratch, "python.zip"); ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
                using (WebClient client = new WebClient()) client.DownloadFile(url, download);
                using (SHA256 hash = SHA256.Create()) using (Stream stream = File.OpenRead(download)) {
                    string actual = BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                    if (actual != (string)runtime["sha256"]) throw new Exception("Python download hash mismatch.");
                }
                string stage = Path.Combine(root, "python-stage-" + Guid.NewGuid().ToString("N")); ExtractSafe(download, stage);
                if (Directory.Exists(runtimeRoot)) throw new Exception("Incomplete runtime folder exists: " + runtimeRoot);
                Directory.Move(stage, runtimeRoot);
            }
            ProcessStartInfo info = new ProcessStartInfo(python, Quote(Path.Combine(extracted, "installer", "install.py")) + " --payload " + Quote(payload) + " --install-root " + Quote(root));
            info.UseShellExecute = false; info.CreateNoWindow = true; info.RedirectStandardOutput = info.RedirectStandardError = true;
            using (Process process = new Process { StartInfo = info }) {
                process.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { Append(e.Data); };
                process.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { Append(e.Data); };
                process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine(); process.WaitForExit(); result = process.ExitCode;
            }
            if (result == 0) {
                string app = File.ReadAllText(Path.Combine(root, "current-app.txt")).Trim();
                string uninstaller = Path.Combine(root, "Uninstall.exe"); File.Copy(Application.ExecutablePath, uninstaller, true);
                string menu = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Product); Directory.CreateDirectory(menu);
                MakeShortcut(Path.Combine(menu, Product + ".lnk"), app);
                string desktopLink = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Product + ".lnk");
                if (wantDesktop || File.Exists(desktopLink)) MakeShortcut(desktopLink, app);
                File.WriteAllText(Path.Combine(root, "installation-id.txt"), "BMW_DDE5_MS43_Tuner");
                using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath)) {
                    key.SetValue("DisplayName", Product); key.SetValue("DisplayVersion", Assembly.GetExecutingAssembly().GetName().Version.ToString(3)); key.SetValue("Publisher", "Emil Larsson");
                    key.SetValue("InstallLocation", root); key.SetValue("DisplayIcon", Path.Combine(app, "assets", "editor.ico")); key.SetValue("UninstallString", Quote(uninstaller) + " --uninstall");
                    key.SetValue("NoModify", 1, RegistryValueKind.DWord); key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                }
                Append(Product + " is ready in the Start menu and Installed apps.");
                if (wantLaunch) {
                    ProcessStartInfo start = new ProcessStartInfo(Path.Combine(root, "environment", "Scripts", "pythonw.exe"), Quote(Path.Combine(app, "run_editor.py")));
                    start.WorkingDirectory = app; start.UseShellExecute = false; start.CreateNoWindow = true; Process.Start(start);
                }
            }
        } catch (Exception error) { result = 1; Append(error.Message); }
        finally {
            if (held) mutex.ReleaseMutex(); if (mutex != null) mutex.Dispose();
            string tempRoot = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (Path.GetFullPath(scratch).StartsWith(tempRoot, StringComparison.OrdinalIgnoreCase)) { try { Directory.Delete(scratch, true); } catch {} }
            BeginInvoke((Action)(() => { busy = false; close.Enabled = true; if (result == 0) { install.Text = "Installed"; Append("Setup finished."); } else { install.Enabled = desktop.Enabled = launch.Enabled = true; Append("Setup failed. Fix the error above, then retry."); } }));
        }
    }
    static string Quote(string path) { return "\"" + path + "\""; }
    static void ExtractSafe(string zipPath, string destination) {
        string expected = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        using (ZipArchive zip = ZipFile.OpenRead(zipPath)) foreach (ZipArchiveEntry entry in zip.Entries) {
            string path = Path.GetFullPath(Path.Combine(destination, entry.FullName));
            if (!path.StartsWith(expected, StringComparison.OrdinalIgnoreCase)) throw new Exception("Unsafe archive path.");
        }
        ZipFile.ExtractToDirectory(zipPath, destination);
    }
    static void MakeShortcut(string path, string app) {
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")); dynamic link = shell.CreateShortcut(path);
        link.TargetPath = Path.Combine(root, "environment", "Scripts", "pythonw.exe"); link.Arguments = Quote(Path.Combine(app, "run_editor.py"));
        link.WorkingDirectory = app; link.IconLocation = Path.Combine(app, "assets", "editor.ico"); link.Save();
    }
    static bool AppRunning() {
        foreach (Process process in Process.GetProcesses()) {
            try { if ((process.ProcessName == "python" || process.ProcessName == "pythonw") && process.MainModule.FileName.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) return true; }
            catch {} finally { process.Dispose(); }
        }
        return false;
    }
    static int Uninstall() {
        if (MessageBox.Show("Remove " + Product + " and its private runtime? Files inside its installation folder will be removed. Preferences and calibration files elsewhere are kept.", Product, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return 0;
        if (AppRunning()) { MessageBox.Show("Close the editor and run uninstall again."); return 1; }
        string scratch = Path.Combine(Path.GetTempPath(), "bmw-tuner-remove-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(scratch);
        string helper = Path.Combine(scratch, "Remove.exe"); File.Copy(Application.ExecutablePath, helper);
        Process.Start(new ProcessStartInfo(helper, "--remove-installation " + Process.GetCurrentProcess().Id) { UseShellExecute = false, CreateNoWindow = true }); return 0;
    }
    static int RemoveInstallation(int parent) {
        string temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!Path.GetFullPath(Application.ExecutablePath).StartsWith(temp, StringComparison.OrdinalIgnoreCase)) return 2;
        try { if (!Process.GetProcessById(parent).WaitForExit(30000)) return 3; } catch (ArgumentException) {}
        if (AppRunning()) return 4;
        string expected = Path.GetFullPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "BMW_DDE5_MS43_Tuner"));
        if (!String.Equals(Path.GetFullPath(root), expected, StringComparison.OrdinalIgnoreCase)) return 5;
        string marker = Path.Combine(root, "installation-id.txt");
        if (!File.Exists(marker) || File.ReadAllText(marker).Trim() != "BMW_DDE5_MS43_Tuner") return 6;
        string programs = Path.GetFullPath(Environment.GetFolderPath(Environment.SpecialFolder.Programs)).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string menu = Path.GetFullPath(Path.Combine(programs, Product));
        if (menu.StartsWith(programs, StringComparison.OrdinalIgnoreCase) && Directory.Exists(menu)) Directory.Delete(menu, true);
        string desktop = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Product + ".lnk"); if (File.Exists(desktop)) File.Delete(desktop);
        Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
        if (Directory.Exists(root)) Directory.Delete(root, true);
        MessageBox.Show(Product + " was removed."); return 0;
    }
    [STAThread] static int Main(string[] args) {
        if (args.Length == 2 && args[0] == "--extract-only") {
            if (Directory.Exists(args[1])) return 2;
            Directory.CreateDirectory(args[1]); Resource("payload.zip", Path.Combine(args[1], "payload.zip")); return 0;
        }
        if (args.Length == 1 && args[0] == "--uninstall") return Uninstall();
        if (args.Length == 2 && args[0] == "--remove-installation") return RemoveInstallation(Int32.Parse(args[1]));
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new Setup()); return 0;
    }
}
