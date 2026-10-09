// Rebuild with launcher/Build-Launcher.ps1. No Python or model files are bundled.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("StartupForge Launcher")]
[assembly: AssemblyDescription("Starts the local StartupForge workspace and opens its browser interface.")]
[assembly: AssemblyCompany("StartupForge")]
[assembly: AssemblyProduct("StartupForge")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace StartupForge.Launcher
{
    internal sealed class Options
    {
        public bool Check;
        public bool Headless;
        public string Workspace;
        public string Report;

        public static Options Parse(string[] arguments)
        {
            Options result = new Options();
            result.Workspace = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            for (int i = 0; i < arguments.Length; i++)
            {
                switch (arguments[i])
                {
                    case "--check": result.Check = true; break;
                    case "--headless": result.Headless = true; break;
                    case "--workspace":
                    case "--report":
                        string option = arguments[i];
                        if (++i == arguments.Length || arguments[i].StartsWith("--", StringComparison.Ordinal))
                            throw new ArgumentException(option + " requires a path.");
                        if (option == "--workspace") result.Workspace = Path.GetFullPath(arguments[i]);
                        else result.Report = Path.GetFullPath(arguments[i]);
                        break;
                    default: throw new ArgumentException("Unknown launcher option: " + arguments[i]);
                }
            }
            return result;
        }
    }

    internal sealed class Launcher
    {
        public const string AppUrl = "http://127.0.0.1:8000/";
        private const string HealthUrl = AppUrl + "api/v1/health";
        private const string OllamaVersionUrl = "http://127.0.0.1:11434/api/version";
        private readonly Options options;
        private readonly object logLock = new object();
        private readonly JavaScriptSerializer serializer = new JavaScriptSerializer();
        public readonly string LogPath;
        public string BackendDirectory;
        public volatile bool Cancelled;
        public Action<string, int> Progress = delegate { };

        public Launcher(Options configuration)
        {
            options = configuration;
            string logDirectory = Path.Combine(options.Workspace, "launcher", ".runtime");
            Directory.CreateDirectory(logDirectory);
            LogPath = Path.Combine(logDirectory, "launcher-" + DateTime.Now.ToString("yyyyMMdd") + ".log");
        }

        public Dictionary<string, object> CheckWorkspace()
        {
            string memberBackend = Path.Combine(options.Workspace, "member1", "backend");
            string legacyBackend = Path.Combine(options.Workspace, "backend");
            // Prefer the member layout once it exists; never silently switch to an
            // old backend when the selected member layout is missing dependencies.
            BackendDirectory = Directory.Exists(memberBackend) ? memberBackend : legacyBackend;
            string frontend = Path.Combine(Path.GetDirectoryName(BackendDirectory), "frontend", "dist", "index.html");
            Dictionary<string, object> checks = new Dictionary<string, object>();
            checks["backend_directory"] = Directory.Exists(BackendDirectory);
            checks["python_environment"] = File.Exists(Path.Combine(BackendDirectory, ".venv", "Scripts", "python.exe"));
            checks["startup_script"] = File.Exists(Path.Combine(BackendDirectory, "scripts", "start_local_services.py"));
            checks["service_worker"] = File.Exists(Path.Combine(BackendDirectory, "scripts", "service_worker.py"));
            checks["portable_ollama"] = File.Exists(Path.Combine(BackendDirectory, ".runtime", "ollama-v0.40.1", "ollama.exe"));
            checks["frontend_build"] = File.Exists(frontend);
            List<string> missing = new List<string>();
            foreach (KeyValuePair<string, object> check in checks)
                if (!(bool)check.Value) missing.Add(check.Key);
            Dictionary<string, object> report = NewReport(missing.Count == 0 ? "check_passed" : "check_failed");
            report["checks"] = checks;
            report["missing"] = missing;
            if (missing.Count > 0)
                report["message"] = "Missing workspace components: " + string.Join(", ", missing.ToArray()) + ". Follow the environment setup in README.md, then rebuild the frontend if needed.";
            return report;
        }

        public void Start()
        {
            Update("Checking the local workspace…", 8);
            Dictionary<string, object> check = CheckWorkspace();
            if ((string)check["status"] != "check_passed") throw new InvalidOperationException((string)check["message"]);
            CheckCancelled();
            Update("Checking local service ports…", 18);
            EnsureExpectedService(8000, HealthUrl, true);
            EnsureExpectedService(11434, OllamaVersionUrl, false);
            Update("Starting the local AI and app services…", 35);
            string python = Path.Combine(BackendDirectory, ".venv", "Scripts", "python.exe");
            string script = Path.Combine(BackendDirectory, "scripts", "start_local_services.py");
            ProcessStartInfo start = new ProcessStartInfo(python, QuoteArgument(script));
            start.WorkingDirectory = BackendDirectory;
            start.CreateNoWindow = true;
            start.UseShellExecute = false;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            using (Process process = new Process())
            {
                process.StartInfo = start;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args) { if (args.Data != null) Log(args.Data); };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args) { if (args.Data != null) Log(args.Data); };
                if (!process.Start()) throw new InvalidOperationException("Windows could not start the local service launcher.");
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                Stopwatch timer = Stopwatch.StartNew();
                while (!process.WaitForExit(200))
                {
                    if (Cancelled || timer.Elapsed.TotalSeconds > 180)
                    {
                        // Only stop our transient starter. The WMI-owned services
                        // are independent and are never recursively terminated.
                        try { process.Kill(); } catch (InvalidOperationException) { }
                        if (Cancelled) throw new OperationCanceledException("Startup was cancelled.");
                        throw new TimeoutException("Startup took more than three minutes. Check the launcher and backend service logs before trying again.");
                    }
                }
                process.WaitForExit(); // Finish draining the redirected log streams.
                if (process.ExitCode != 0)
                    throw new InvalidOperationException("The service startup script exited with code " + process.ExitCode + ". Inspect " + Path.Combine(BackendDirectory, ".runtime") + " and the launcher log.");
            }
            Update("Waiting for StartupForge to be ready…", 80);
            Stopwatch readyTimer = Stopwatch.StartNew();
            while (!IsExpectedService(ReadJson(HealthUrl), true))
            {
                CheckCancelled();
                if (readyTimer.Elapsed.TotalSeconds > 30)
                    throw new TimeoutException("The StartupForge health endpoint did not become ready. Inspect the backend API logs.");
                Thread.Sleep(300);
            }
            // A healthy API without the compiled UI would otherwise open a 404.
            if (!IsFrontendReady()) throw new InvalidOperationException("The API is ready, but the app page is unavailable. Rebuild member1/frontend (or frontend) and restart the API.");
            Update("StartupForge is ready.", 100);
        }

        public void OpenBrowser()
        {
            Process.Start(new ProcessStartInfo(AppUrl) { UseShellExecute = true });
            Log("Opened the default browser at " + AppUrl);
        }

        public void WriteReport(Dictionary<string, object> report)
        {
            string path = options.Report;
            if (string.IsNullOrEmpty(path))
                path = Path.Combine(options.Workspace, "launcher", ".runtime", options.Check ? "check-result.json" : "launch-result.json");
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, serializer.Serialize(report), new UTF8Encoding(false));
        }

        public Dictionary<string, object> NewReport(string status)
        {
            return new Dictionary<string, object> {
                { "status", status }, { "workspace", options.Workspace },
                { "backend_directory", BackendDirectory }, { "app_url", AppUrl },
                { "log_path", LogPath }, { "timestamp_utc", DateTime.UtcNow.ToString("o") }
            };
        }

        public void Log(string message)
        {
            lock (logLock)
            {
                File.AppendAllText(LogPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine, new UTF8Encoding(false));
            }
        }

        private void Update(string message, int percent)
        {
            CheckCancelled();
            Log(message);
            Progress(message, percent);
        }

        private void CheckCancelled()
        {
            if (Cancelled) throw new OperationCanceledException("Startup was cancelled.");
        }

        private void EnsureExpectedService(int port, string address, bool app)
        {
            if (!PortInUse(port)) return;
            if (!IsExpectedService(ReadJson(address), app))
                throw new InvalidOperationException("Port " + port + " is already occupied by a service that is not " + (app ? "StartupForge" : "Ollama") + ". Close that service or change its port before launching StartupForge. The launcher has left it running.");
            Log("Reusing " + (app ? "StartupForge" : "Ollama") + " on port " + port + ".");
        }

        private static bool PortInUse(int port)
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult attempt = client.BeginConnect(IPAddress.Loopback, port, null, null);
                try
                {
                    if (!attempt.AsyncWaitHandle.WaitOne(750)) return false;
                    client.EndConnect(attempt);
                    return true;
                }
                catch (SocketException) { return false; }
                finally { attempt.AsyncWaitHandle.Close(); }
            }
        }

        private Dictionary<string, object> ReadJson(string address)
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(address);
                request.Proxy = null;
                request.Timeout = 2000;
                request.ReadWriteTimeout = 2000;
                request.AllowAutoRedirect = false;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                {
                    if (response.StatusCode != HttpStatusCode.OK) return null;
                    string text = reader.ReadToEnd();
                    if (text.Length > 65536) return null;
                    return serializer.DeserializeObject(text) as Dictionary<string, object>;
                }
            }
            catch (WebException) { return null; }
            catch (ArgumentException) { return null; }
            catch (InvalidOperationException) { return null; }
        }

        private static bool IsExpectedService(Dictionary<string, object> body, bool app)
        {
            if (body == null) return false;
            object value;
            if (!app) return body.TryGetValue("version", out value) && value is string && !string.IsNullOrWhiteSpace((string)value);
            object status;
            if (!body.TryGetValue("status", out status) || !string.Equals(status as string, "ok", StringComparison.Ordinal)) return false;
            if (!body.TryGetValue("modules", out value)) return false;
            object[] modules = value as object[];
            if (modules == null) return false;
            foreach (object module in modules)
                if (string.Equals(module as string, "intelligence", StringComparison.Ordinal)) return true;
            return false;
        }

        private static bool IsFrontendReady()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(AppUrl);
                request.Proxy = null;
                request.Timeout = 3000;
                request.ReadWriteTimeout = 3000;
                request.AllowAutoRedirect = false;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    return response.StatusCode == HttpStatusCode.OK && response.ContentType.StartsWith("text/html", StringComparison.OrdinalIgnoreCase);
            }
            catch (WebException) { return false; }
        }

        // Windows CommandLineToArgvW-compatible quoting, without a shell.
        private static string QuoteArgument(string value)
        {
            StringBuilder result = new StringBuilder("\"");
            int backslashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { backslashes++; continue; }
                if (character == '"') { result.Append('\\', backslashes * 2 + 1); result.Append(character); backslashes = 0; continue; }
                result.Append('\\', backslashes); backslashes = 0; result.Append(character);
            }
            result.Append('\\', backslashes * 2);
            result.Append('"');
            return result.ToString();
        }
    }

    internal sealed class StartupWindow : Form
    {
        private readonly Launcher launcher;
        private readonly Label detail;
        private readonly ProgressBar bar;
        private readonly BackgroundWorker worker;
        private readonly Button cancel;
        public int Result = 1;

        public StartupWindow(Launcher runner)
        {
            launcher = runner;
            Text = "StartupForge";
            Icon = SystemIcons.Application;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(570, 225);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            Font = new Font("Segoe UI", 10F);
            BackColor = Color.FromArgb(248, 250, 252);
            Label title = new Label { Text = "Starting StartupForge", Location = new Point(24, 24), AutoSize = true, Font = new Font("Segoe UI", 18F, FontStyle.Bold) };
            detail = new Label { Text = "Preparing your local idea intelligence workspace…", Location = new Point(26, 79), Size = new Size(518, 40) };
            bar = new ProgressBar { Location = new Point(26, 130), Size = new Size(518, 14), Minimum = 0, Maximum = 100 };
            cancel = new Button { Text = "Cancel", Location = new Point(439, 168), Size = new Size(105, 32) };
            cancel.Click += delegate { RequestCancel(); };
            Controls.AddRange(new Control[] { title, detail, bar, cancel });
            worker = new BackgroundWorker { WorkerReportsProgress = true };
            launcher.Progress = delegate(string message, int percent) { worker.ReportProgress(percent, message); };
            worker.DoWork += delegate { launcher.Start(); };
            worker.ProgressChanged += delegate(object sender, ProgressChangedEventArgs args) { detail.Text = (string)args.UserState; bar.Value = args.ProgressPercentage; };
            worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs args)
            {
                cancel.Enabled = false;
                try
                {
                    if (args.Error != null) throw args.Error;
                    launcher.OpenBrowser();
                    launcher.WriteReport(launcher.NewReport("ready"));
                    Result = 0;
                }
                catch (OperationCanceledException) { Result = 2; }
                catch (Exception error)
                {
                    launcher.Log("ERROR: " + error.Message);
                    Dictionary<string, object> report = launcher.NewReport("failed"); report["message"] = error.Message; launcher.WriteReport(report);
                    Hide();
                    Program.ShowError(error.Message, launcher.LogPath, launcher.BackendDirectory);
                }
                Close();
            };
            Shown += delegate { worker.RunWorkerAsync(); };
            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (worker.IsBusy) { args.Cancel = true; RequestCancel(); }
            };
        }

        private void RequestCancel()
        {
            launcher.Cancelled = true;
            cancel.Enabled = false;
            detail.Text = "Cancelling the launcher…";
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] arguments)
        {
            Options options = null;
            Launcher launcher = null;
            try
            {
                options = Options.Parse(arguments);
                launcher = new Launcher(options);
                if (options.Check)
                {
                    Dictionary<string, object> check = launcher.CheckWorkspace();
                    launcher.WriteReport(check);
                    launcher.Log("Workspace check: " + check["status"]);
                    return (string)check["status"] == "check_passed" ? 0 : 1;
                }
                string rootKey;
                using (SHA256 sha = SHA256.Create())
                    rootKey = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(options.Workspace.ToUpperInvariant()))).Replace("-", "");
                bool acquired;
                using (Mutex mutex = new Mutex(true, "Local\\StartupForgeLauncher-" + rootKey, out acquired))
                {
                    if (!acquired) throw new InvalidOperationException("StartupForge is already being started. Wait for the existing launcher to finish.");
                    try
                    {
                        if (options.Headless)
                        {
                            launcher.Start();
                            launcher.WriteReport(launcher.NewReport("ready"));
                            return 0;
                        }
                        Application.EnableVisualStyles();
                        Application.SetCompatibleTextRenderingDefault(false);
                        using (StartupWindow window = new StartupWindow(launcher))
                        {
                            Application.Run(window);
                            return window.Result;
                        }
                    }
                    finally { mutex.ReleaseMutex(); }
                }
            }
            catch (Exception error)
            {
                if (launcher != null)
                {
                    launcher.Log("ERROR: " + error.Message);
                    try { Dictionary<string, object> report = launcher.NewReport("failed"); report["message"] = error.Message; launcher.WriteReport(report); } catch (IOException) { }
                }
                // Unknown arguments should also be usable from a test harness.
                bool headless = Array.IndexOf(arguments, "--check") >= 0 || Array.IndexOf(arguments, "--headless") >= 0;
                if (!headless) ShowError(error.Message, launcher == null ? null : launcher.LogPath, launcher == null ? null : launcher.BackendDirectory);
                return 1;
            }
        }

        public static void ShowError(string message, string logPath, string backendDirectory)
        {
            string logs = string.IsNullOrEmpty(backendDirectory) ? logPath : Path.Combine(backendDirectory, ".runtime");
            string text = message + Environment.NewLine + Environment.NewLine + "Launcher log: " + (logPath ?? "Unavailable") + Environment.NewLine + "Service logs: " + (logs ?? "Unavailable") + Environment.NewLine + Environment.NewLine + "Open the launcher log now?";
            if (MessageBox.Show(text, "StartupForge could not start", MessageBoxButtons.YesNo, MessageBoxIcon.Error) == DialogResult.Yes && logPath != null)
                Process.Start(new ProcessStartInfo(logPath) { UseShellExecute = true });
        }
    }
}
