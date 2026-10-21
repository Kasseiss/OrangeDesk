// Temporary, loopback-only HTTP proxy. Compatible with Windows PowerShell 5.1.
// TLS is tunnelled without inspection. No URLs, headers or credentials are logged.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace FastOrangeCombined
{
    public sealed class DomainPolicy
    {
        private readonly HashSet<string> suffixes = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private readonly HashSet<string> exact = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private readonly List<Regex> expressions = new List<Regex>();
        private readonly List<IpRange> ranges = new List<IpRange>();
        private readonly string[] vpnSuffixes = new string[] {
            "chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com",
            "claude.ai", "claude.com", "anthropic.com"
        };
        public int RuleCount { get; private set; }
        public int PrefixCount { get; private set; }
        public int RegexCount { get { return expressions.Count; } }
        public int UnsupportedRuleCount { get; private set; }
        public int InvalidPrefixCount { get; private set; }
        private int regexTimeoutCount;
        public int RegexTimeoutCount { get { return Interlocked.CompareExchange(ref regexTimeoutCount, 0, 0); } }
        private sealed class OverrideSnapshot
        {
            internal readonly Dictionary<string, string> Exact, Suffix;
            internal readonly int Count;
            internal OverrideSnapshot(Dictionary<string, string> exactRules, Dictionary<string, string> suffixRules, int count)
            { Exact = exactRules; Suffix = suffixRules; Count = count; }
        }
        // Published dictionaries are never modified. Readers capture one snapshot,
        // so refreshes cannot expose a partially replaced whitelist.
        private OverrideSnapshot overrides = new OverrideSnapshot(new Dictionary<string, string>(StringComparer.Ordinal),
            new Dictionary<string, string>(StringComparer.Ordinal), 0);
        public int OverrideCount { get { return Interlocked.CompareExchange(ref overrides, null, null).Count; } }

        private struct IpRange { public uint First; public uint Last; }

        public DomainPolicy(string rulesText, string cnPrefixes)
        {
            suffixes.Add("cn");
            foreach (string input in (rulesText ?? "").Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string rule = input.Trim();
                if (rule.Length == 0 || rule[0] == '#') continue;
                if (rule.StartsWith("regexp:", StringComparison.OrdinalIgnoreCase))
                {
                    try
                    {
                        expressions.Add(new Regex(rule.Substring(7), RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
                            TimeSpan.FromMilliseconds(50)));
                        RuleCount++;
                    }
                    catch (ArgumentException) { UnsupportedRuleCount++; }
                    continue;
                }
                bool full = rule.StartsWith("full:", StringComparison.OrdinalIgnoreCase);
                if (full) rule = rule.Substring(5);
                else if (rule.StartsWith("domain:", StringComparison.OrdinalIgnoreCase)) rule = rule.Substring(7);
                else if (rule.IndexOf(':') >= 0) { UnsupportedRuleCount++; continue; }
                rule = NormalizeHost(rule);
                IPAddress address;
                if (rule == null || IPAddress.TryParse(rule, out address)) { UnsupportedRuleCount++; continue; }
                if ((full ? exact : suffixes).Add(rule)) RuleCount++;
            }
            foreach (string input in (cnPrefixes ?? "").Split(new char[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string prefix = input.Trim();
                if (prefix.Length == 0 || prefix[0] == '#') continue;
                string[] parts = prefix.Split('/');
                IPAddress address;
                int bits;
                if (parts.Length != 2 || !IPAddress.TryParse(parts[0], out address) ||
                    address.AddressFamily != AddressFamily.InterNetwork || !Int32.TryParse(parts[1], out bits) || bits < 0 || bits > 32)
                { InvalidPrefixCount++; continue; }
                uint mask = bits == 0 ? 0 : UInt32.MaxValue << (32 - bits);
                uint first = Number(address) & mask;
                ranges.Add(new IpRange { First = first, Last = first | ~mask });
                PrefixCount++;
            }
            ranges.Sort(delegate(IpRange a, IpRange b) { return a.First.CompareTo(b.First); });
            List<IpRange> merged = new List<IpRange>();
            foreach (IpRange r in ranges)
            {
                if (merged.Count == 0) { merged.Add(r); continue; }
                IpRange previous = merged[merged.Count - 1];
                if (r.First <= previous.Last || (previous.Last != UInt32.MaxValue && r.First == previous.Last + 1))
                { previous.Last = Math.Max(previous.Last, r.Last); merged[merged.Count - 1] = previous; }
                else merged.Add(r);
            }
            ranges.Clear(); ranges.AddRange(merged);
        }

        public void SetOverrides(string[] hosts, string[] modes, bool[] subdomains)
        {
            if (hosts == null || modes == null || subdomains == null || hosts.Length != modes.Length || hosts.Length != subdomains.Length)
                throw new ArgumentException("Override arrays must have the same length.");
            if (hosts.Length > 1000) throw new ArgumentException("At most 1000 overrides are allowed.");
            Dictionary<string, string> exactRules = new Dictionary<string, string>(StringComparer.Ordinal);
            Dictionary<string, string> suffixRules = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int i = 0; i < hosts.Length; i++)
            {
                string host = hosts[i]; IPAddress address;
                if (host == null || host != NormalizeHost(host) || host.Length > 253)
                    throw new ArgumentException("Override hosts must be canonical ASCII names or IPv4 addresses.");
                foreach (char c in host) if (c > 127 || c == '_') throw new ArgumentException("Invalid override host.");
                if (IPAddress.TryParse(host, out address) && (address.AddressFamily != AddressFamily.InterNetwork || subdomains[i]))
                    throw new ArgumentException("IPv4 overrides must be exact.");
                if (modes[i] != "direct" && modes[i] != "vpn") throw new ArgumentException("Invalid override mode.");
                Dictionary<string, string> target = subdomains[i] ? suffixRules : exactRules;
                if (target.ContainsKey(host)) throw new ArgumentException("Duplicate override host and scope.");
                target.Add(host, modes[i]);
            }
            Interlocked.Exchange(ref overrides, new OverrideSnapshot(exactRules, suffixRules, hosts.Length));
        }

        public string GetOverride(string host)
        {
            host = NormalizeHost(host);
            if (host == null) return "";
            return GetOverrideNormalized(host, Interlocked.CompareExchange(ref overrides, null, null));
        }

        private static string GetOverrideNormalized(string host, OverrideSnapshot snapshot)
        {
            string mode;
            // Exact wins at the requested host; label walking naturally gives
            // more-specific suffixes priority and enforces literal dot boundaries.
            if (snapshot.Exact.TryGetValue(host, out mode)) return mode;
            string label = host;
            while (true)
            {
                if (snapshot.Suffix.TryGetValue(label, out mode)) return mode;
                int dot = label.IndexOf('.'); if (dot < 0) break; label = label.Substring(dot + 1);
            }
            return "";
        }

        public bool IsForcedVpn(string host)
        {
            host = NormalizeHost(host);
            if (host == null) return false;
            string mode = GetOverrideNormalized(host, Interlocked.CompareExchange(ref overrides, null, null));
            if (mode.Length != 0) return mode == "vpn";
            return IsAutomaticForcedVpn(host);
        }

        private bool IsAutomaticForcedVpn(string host)
        {
            foreach (string suffix in vpnSuffixes)
                if (host == suffix || host.EndsWith("." + suffix, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        public bool IsDirect(string host)
        {
            host = NormalizeHost(host);
            if (host == null) return false;
            string mode = GetOverrideNormalized(host, Interlocked.CompareExchange(ref overrides, null, null));
            if (mode.Length != 0) return mode == "direct";
            return IsAutomaticDirect(host);
        }

        internal bool IsAutomaticDirect(string host)
        {
            if (IsAutomaticForcedVpn(host)) return false;
            IPAddress address;
            if (IPAddress.TryParse(host, out address))
            {
                if (address.AddressFamily != AddressFamily.InterNetwork) return false;
                uint value = Number(address);
                // LAN traffic stays on the physical interface while this proxy is running.
                if ((value >> 24) == 127 || (value >> 24) == 10 || (value >> 16) == 0xC0A8 ||
                    (value >= 0xAC100000 && value <= 0xAC1FFFFF) || (value >> 16) == 0xA9FE) return true;
                int lo = 0, hi = ranges.Count - 1;
                while (lo <= hi)
                {
                    int middle = lo + ((hi - lo) / 2); IpRange range = ranges[middle];
                    if (value < range.First) hi = middle - 1;
                    else if (value > range.Last) lo = middle + 1;
                    else return true;
                }
                return false;
            }
            if (exact.Contains(host)) return true;
            string label = host;
            while (true)
            {
                if (suffixes.Contains(label)) return true;
                int dot = label.IndexOf('.'); if (dot < 0) break; label = label.Substring(dot + 1);
            }
            foreach (Regex expression in expressions)
            {
                try { if (expression.IsMatch(host)) return true; }
                catch (RegexMatchTimeoutException) { Interlocked.Increment(ref regexTimeoutCount); }
            }
            return false;
        }

        internal bool SelectRoute(string host, out string overrideMode, out bool forcedVpn)
        {
            OverrideSnapshot snapshot = Interlocked.CompareExchange(ref overrides, null, null);
            overrideMode = GetOverrideNormalized(host, snapshot);
            forcedVpn = overrideMode == "vpn" || (overrideMode.Length == 0 && IsAutomaticForcedVpn(host));
            return overrideMode.Length == 0 ? IsAutomaticDirect(host) : overrideMode == "direct";
        }

        internal static uint Number(IPAddress address)
        {
            byte[] b = address.GetAddressBytes();
            return ((uint)b[0] << 24) | ((uint)b[1] << 16) | ((uint)b[2] << 8) | b[3];
        }

        public static string NormalizeHost(string host)
        {
            if (String.IsNullOrWhiteSpace(host)) return null;
            host = host.Trim().TrimEnd('.');
            IPAddress address;
            if (IPAddress.TryParse(host, out address)) return address.ToString().ToLowerInvariant();
            try { host = new IdnMapping().GetAscii(host).ToLowerInvariant(); }
            catch (ArgumentException) { return null; }
            if (host.Length == 0 || host.Length > 253) return null;
            foreach (string label in host.Split('.'))
            {
                if (label.Length == 0 || label.Length > 63 || label[0] == '-' || label[label.Length - 1] == '-') return null;
                foreach (char c in label)
                    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_')) return null;
            }
            return host;
        }
    }

    // Internal seams are compiled with the loopback test harness, never exposed as a
    // production bypass. A binding identifies one adapter incarnation and address.
    internal sealed class VpnBinding
    {
        internal readonly int Index;
        internal readonly string Guid;
        internal readonly IPAddress Address;
        internal readonly long Generation;
        internal VpnBinding(int index, string guid, IPAddress address, long generation)
        { Index = index; Guid = guid; Address = address; Generation = generation; }
    }

    internal interface IVpnGuard
    {
        bool IsLive(VpnBinding binding, IPAddress target);
        void Bind(Socket socket, VpnBinding binding);
        bool IsBound(Socket socket, VpnBinding binding);
    }

    internal sealed class WindowsVpnGuard : IVpnGuard
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct SockAddrV4
        { internal ushort Family; internal ushort Port; internal uint Address; internal ulong Padding; }
        [DllImport("iphlpapi.dll")]
        private static extern uint GetBestInterfaceEx(ref SockAddrV4 destination, out uint bestIndex);

        internal static bool BestInterfaceIs(int interfaceIndex, IPAddress target)
        {
            if (target == null || target.AddressFamily != AddressFamily.InterNetwork) return false;
            SockAddrV4 destination = new SockAddrV4 { Family = 2, Address = BitConverter.ToUInt32(target.GetAddressBytes(), 0) };
            uint bestIndex;
            return GetBestInterfaceEx(ref destination, out bestIndex) == 0 && bestIndex == (uint)interfaceIndex;
        }

        public bool IsLive(VpnBinding binding, IPAddress target)
        {
            if (binding == null) return false;
            try
            {
                bool found = false;
                foreach (NetworkInterface nic in NetworkInterface.GetAllNetworkInterfaces())
                {
                    if (!String.Equals(nic.Id.Trim('{', '}'), binding.Guid, StringComparison.OrdinalIgnoreCase)) continue;
                    if (nic.OperationalStatus != OperationalStatus.Up || nic.Description.IndexOf("TAP-Windows", StringComparison.OrdinalIgnoreCase) < 0) return false;
                    IPInterfaceProperties properties = nic.GetIPProperties();
                    if (properties.GetIPv4Properties().Index != binding.Index) return false;
                    foreach (UnicastIPAddressInformation address in properties.UnicastAddresses)
                        if (address.Address.Equals(binding.Address)) { found = true; break; }
                    break;
                }
                if (!found) return false;
                if (target != null)
                {
                    if (!BestInterfaceIs(binding.Index, target)) return false;
                }
                return true;
            }
            catch (Exception) { return false; }
        }

        public void Bind(Socket socket, VpnBinding binding)
        {
            socket.SetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31, IPAddress.HostToNetworkOrder(binding.Index));
            socket.Bind(new IPEndPoint(binding.Address, 0));
        }
        public bool IsBound(Socket socket, VpnBinding binding)
        {
            try { return ((IPEndPoint)socket.LocalEndPoint).Address.Equals(binding.Address); }
            catch (Exception) { return false; }
        }
    }

    public sealed class BridgeServer : IDisposable
    {
        private const int MaxConnections = 128;
        private const int MaxHeader = 32768;
        private readonly DomainPolicy policy;
        private readonly string token;
        private readonly object gate = new object();
        private readonly Dictionary<int, Session> sessions = new Dictionary<int, Session>();
        private readonly Queue<string> events = new Queue<string>();
        private readonly DirectDns resolver = new DirectDns();
        private TcpListener listener;
        private bool running;
        private int nextId, physicalIndex;
        private readonly IVpnGuard vpnGuard;
        private VpnBinding vpnBinding;
        private long bindingGeneration;
        private long directCount, vpnCount, errorCount;
        public int Port { get; private set; }
        public string PacUrl { get { return "http://127.0.0.1:" + Port.ToString(CultureInfo.InvariantCulture) + "/fastorange/" + token + "/proxy.pac"; } }
        public long DirectCount { get { return Interlocked.Read(ref directCount); } }
        public long VpnCount { get { return Interlocked.Read(ref vpnCount); } }
        public long ErrorCount { get { return Interlocked.Read(ref errorCount); } }
        public int ActiveCount { get { lock (gate) return sessions.Count; } }

        public BridgeServer(DomainPolicy policy, string sessionToken)
            : this(policy, sessionToken, new WindowsVpnGuard()) { }

        internal BridgeServer(DomainPolicy policy, string sessionToken, IVpnGuard guard)
        {
            if (policy == null) throw new ArgumentNullException("policy");
            if (String.IsNullOrEmpty(sessionToken) || !Regex.IsMatch(sessionToken, "^[A-Za-z0-9_-]{8,128}$"))
                throw new ArgumentException("Session token must be 8-128 URL-safe characters.", "sessionToken");
            if (guard == null) throw new ArgumentNullException("guard");
            this.policy = policy; token = sessionToken; vpnGuard = guard;
        }

        public int Start()
        {
            lock (gate)
            {
                if (running) return Port;
                listener = new TcpListener(IPAddress.Loopback, 0);
                listener.Start(128); Port = ((IPEndPoint)listener.LocalEndpoint).Port; running = true; resolver.Restart();
                listener.BeginAcceptSocket(Accept, listener);
                return Port;
            }
        }

        public void Configure(int physicalInterfaceIndex, bool isVpnReady)
        {
            // A historical bool is not proof of an enforceable VPN binding.
            Configure(physicalInterfaceIndex, -1, "", "");
        }

        public void Configure(int physicalInterfaceIndex, int vpnInterfaceIndex, string vpnGuid, string vpnAddress)
        {
            if (physicalInterfaceIndex < -1) throw new ArgumentOutOfRangeException("physicalInterfaceIndex");
            IPAddress address = null; Guid parsedGuid;
            bool valid = vpnInterfaceIndex > 0 && System.Guid.TryParse(vpnGuid, out parsedGuid) &&
                IPAddress.TryParse(vpnAddress, out address) && address.AddressFamily == AddressFamily.InterNetwork && !address.Equals(IPAddress.Any);
            string guid = valid ? System.Guid.Parse(vpnGuid).ToString("D") : "";
            List<Session> close = new List<Session>();
            lock (gate)
            {
                if (physicalIndex != physicalInterfaceIndex) resolver.ChangeInterface();
                bool changed = valid ? vpnBinding == null || vpnBinding.Index != vpnInterfaceIndex ||
                    vpnBinding.Guid != guid || !vpnBinding.Address.Equals(address) : vpnBinding != null;
                foreach (Session session in sessions.Values)
                    if ((session.Direct && physicalIndex != physicalInterfaceIndex) || (!session.Direct && changed)) close.Add(session);
                physicalIndex = physicalInterfaceIndex;
                if (changed) vpnBinding = valid ? new VpnBinding(vpnInterfaceIndex, guid, address, ++bindingGeneration) : null;
            }
            foreach (Session session in close) session.Close();
        }

        private bool ValidateVpn(VpnBinding binding, IPAddress target)
        {
            lock (gate) { if (binding == null || !Object.ReferenceEquals(binding, vpnBinding)) return false; }
            if (!vpnGuard.IsLive(binding, target)) return false;
            lock (gate) { return Object.ReferenceEquals(binding, vpnBinding); }
        }

        private string VpnStatusJson()
        {
            VpnBinding binding; lock (gate) { binding = vpnBinding; }
            bool ready = ValidateVpn(binding, null);
            return "{\"vpnBindingEnforced\":true,\"pacDirectFallback\":false,\"vpnReady\":" + (ready ? "true" : "false") +
                ",\"claudeRouteIsVpn\":" + (!policy.IsDirect("api.anthropic.com") && !policy.IsDirect("claude.ai") && !policy.IsDirect("platform.claude.com") ? "true" : "false") +
                ",\"vpnInterfaceIndex\":" + (binding == null ? -1 : binding.Index).ToString(CultureInfo.InvariantCulture) +
                ",\"vpnAddress\":\"" + (binding == null ? "" : binding.Address.ToString()) + "\"}";
        }

        public string[] DrainEvents()
        {
            lock (gate) { string[] result = events.ToArray(); events.Clear(); return result; }
        }

        private void Event(string message)
        {
            lock (gate)
            {
                while (events.Count >= 128) events.Dequeue();
                events.Enqueue(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " " + message);
            }
        }

        private void Accept(IAsyncResult result)
        {
            TcpListener source = (TcpListener)result.AsyncState;
            Socket client = null;
            try { client = source.EndAcceptSocket(result); }
            catch (ObjectDisposedException) { return; }
            catch (SocketException) { }
            lock (gate)
            {
                if (!running || source != listener) { SafeClose(client); return; }
                try { source.BeginAcceptSocket(Accept, source); } catch (Exception) { SafeClose(client); return; }
                if (client == null) return;
                IPEndPoint remote = client.RemoteEndPoint as IPEndPoint;
                if (remote == null || !IPAddress.IsLoopback(remote.Address) || sessions.Count >= MaxConnections)
                { SafeClose(client); return; }
                Session session = new Session(this, client, ++nextId);
                sessions.Add(session.Id, session); session.ReadHeader();
            }
        }

        public void Stop()
        {
            Session[] close;
            lock (gate)
            {
                running = false;
                if (listener != null) { listener.Stop(); listener = null; }
                close = new List<Session>(sessions.Values).ToArray();
            }
            foreach (Session session in close) session.Close();
            resolver.Clear();
        }

        public void Dispose() { Stop(); }
        private static void SafeClose(Socket socket) { if (socket != null) try { socket.Close(); } catch (Exception) { } }
        private void Remove(Session session) { lock (gate) sessions.Remove(session.Id); }

        private sealed class Session
        {
            internal readonly int Id;
            internal bool Direct;
            private readonly BridgeServer owner;
            private readonly Socket client;
            private Socket upstream;
            private readonly byte[] header = new byte[MaxHeader];
            private int headerLength, closed, terminating, pipeCount;
            private readonly Timer timeout;
            private long touched;
            private int timeoutSeconds = 15;
            private string host;
            private int destinationPort, interfaceIndex;
            private bool tunnel;
            private byte[] initial;
            private IPAddress[] addresses;
            private int addressIndex;
            private string dnsSource;
            private string overrideMode;
            private bool forcedVpn;
            private VpnBinding binding;
            private IPAddress vpnTarget;
            private BodyLimit body;
            private DateTime connectDeadline;
            private long dnsStarted, tcpStarted;
            private long dnsMilliseconds;

            internal Session(BridgeServer owner, Socket client, int id)
            {
                this.owner = owner; this.client = client; Id = id;
                client.NoDelay = true; Touch(); timeout = new Timer(CheckTimeout, null, 1000, 1000);
            }

            private void Touch() { Interlocked.Exchange(ref touched, DateTime.UtcNow.Ticks); }
            private bool IsClosed { get { return Interlocked.CompareExchange(ref closed, 0, 0) != 0; } }
            private bool IsTerminating { get { return Interlocked.CompareExchange(ref terminating, 0, 0) != 0; } }
            private void CheckTimeout(object unused)
            {
                if (!IsClosed && !IsTerminating && !Direct && binding != null && !owner.ValidateVpn(binding, vpnTarget))
                { Fail(503, "vpn-binding-unavailable"); return; }
                if (!IsClosed && DateTime.UtcNow.Ticks - Interlocked.Read(ref touched) > TimeSpan.FromSeconds(timeoutSeconds).Ticks)
                { if (IsTerminating) Close(); else Fail(504, "timeout"); }
            }

            internal void ReadHeader()
            {
                if (IsClosed || IsTerminating) return;
                try { client.BeginReceive(header, headerLength, header.Length - headerLength, SocketFlags.None, HeaderRead, null); }
                catch (Exception) { Close(); }
            }

            private void HeaderRead(IAsyncResult result)
            {
                if (IsClosed || IsTerminating) return;
                int count;
                try { count = client.EndReceive(result); }
                catch (Exception) { Close(); return; }
                if (count == 0) { Close(); return; }
                headerLength += count;
                int end = -1;
                for (int i = 3; i < headerLength; i++)
                    if (header[i - 3] == 13 && header[i - 2] == 10 && header[i - 1] == 13 && header[i] == 10) { end = i + 1; break; }
                if (end < 0)
                {
                    if (headerLength == header.Length) Fail(431, "header-too-large"); else ReadHeader();
                    return;
                }
                try { ParseHeader(end); }
                catch (Exception) { Fail(400, "invalid-request"); }
            }

            private void ParseHeader(int end)
            {
                for (int i = 0; i < end; i++) if (header[i] == 0 || header[i] > 127) throw new FormatException();
                string[] lines = Encoding.ASCII.GetString(header, 0, end - 2).Split(new string[] { "\r\n" }, StringSplitOptions.None);
                string[] first = lines[0].Split(' ');
                if (first.Length != 3 || (first[2] != "HTTP/1.1" && first[2] != "HTTP/1.0") || !Regex.IsMatch(first[0], "^[A-Z]+$")) throw new FormatException();
                if (first[0] == "GET" && first[1] == "/fastorange/" + owner.token + "/proxy.pac")
                {
                    string pac = "function FindProxyForURL(url, host) { host = host.toLowerCase(); if (host == 'localhost' || host == '127.0.0.1' || host == '::1' || host == '[::1]') return 'DIRECT'; return 'PROXY 127.0.0.1:" + owner.Port.ToString(CultureInfo.InvariantCulture) + "'; }\n";
                    Respond(200, "OK", "application/x-ns-proxy-autoconfig", pac); return;
                }
                if (first[0] == "GET" && first[1] == "/fastorange/" + owner.token + "/vpn-status.json")
                { Respond(200, "OK", "application/json", owner.VpnStatusJson()); return; }
                tunnel = first[0] == "CONNECT";
                string origin = null;
                if (tunnel)
                {
                    int colon = first[1].LastIndexOf(':');
                    if (colon <= 0 || first[1].IndexOf(':') != colon || !Int32.TryParse(first[1].Substring(colon + 1), out destinationPort)) throw new FormatException();
                    host = DomainPolicy.NormalizeHost(first[1].Substring(0, colon));
                }
                else
                {
                    Uri target;
                    if (!Uri.TryCreate(first[1], UriKind.Absolute, out target) || target.Scheme != "http" || target.UserInfo.Length != 0 || target.HostNameType == UriHostNameType.IPv6) throw new FormatException();
                    host = DomainPolicy.NormalizeHost(target.DnsSafeHost); destinationPort = target.Port; origin = target.PathAndQuery;
                }
                if (host == null || destinationPort <= 0 || destinationPort > 65535) throw new FormatException();
                // The loopback listener cannot be used as its own upstream.
                IPAddress literal;
                if (IPAddress.TryParse(host, out literal) && literal.AddressFamily != AddressFamily.InterNetwork) throw new FormatException();
                if (literal != null && IPAddress.IsLoopback(literal) && destinationPort == owner.Port) throw new FormatException();
                byte[] leftover = new byte[headerLength - end]; Buffer.BlockCopy(header, end, leftover, 0, leftover.Length);
                if (tunnel) initial = leftover;
                else
                {
                    long length = 0; bool hasLength = false, chunked = false; string transfer = null;
                    StringBuilder rewritten = new StringBuilder(); rewritten.Append(first[0]).Append(' ').Append(origin).Append(" HTTP/1.1\r\n");
                    rewritten.Append("Host: ").Append(host); if (destinationPort != 80) rewritten.Append(':').Append(destinationPort.ToString(CultureInfo.InvariantCulture)); rewritten.Append("\r\n");
                    for (int i = 1; i < lines.Length; i++)
                    {
                        if (lines[i].Length == 0) continue;
                        if (lines[i][0] == ' ' || lines[i][0] == '\t') throw new FormatException();
                        foreach (char c in lines[i]) if (c < 32 && c != '\t') throw new FormatException();
                        int colon = lines[i].IndexOf(':'); if (colon <= 0) throw new FormatException();
                        string name = lines[i].Substring(0, colon).Trim(), value = lines[i].Substring(colon + 1).Trim();
                        if (!Regex.IsMatch(name, "^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")) throw new FormatException();
                        if (name.Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                        {
                            long parsed; if (!Int64.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out parsed) || parsed < 0 || (hasLength && parsed != length)) throw new FormatException();
                            hasLength = true; length = parsed; continue;
                        }
                        if (name.Equals("Transfer-Encoding", StringComparison.OrdinalIgnoreCase))
                        { if (transfer != null || !value.Equals("chunked", StringComparison.OrdinalIgnoreCase)) throw new FormatException(); transfer = value; chunked = true; continue; }
                        if (name.Equals("Host", StringComparison.OrdinalIgnoreCase) || name.Equals("Proxy-Authorization", StringComparison.OrdinalIgnoreCase) ||
                            name.Equals("Proxy-Connection", StringComparison.OrdinalIgnoreCase) || name.Equals("Connection", StringComparison.OrdinalIgnoreCase) ||
                            name.Equals("Keep-Alive", StringComparison.OrdinalIgnoreCase) || name.Equals("Upgrade", StringComparison.OrdinalIgnoreCase)) continue;
                        rewritten.Append(name).Append(": ").Append(value).Append("\r\n");
                    }
                    if (hasLength && chunked) throw new FormatException();
                    if (hasLength) rewritten.Append("Content-Length: ").Append(length.ToString(CultureInfo.InvariantCulture)).Append("\r\n");
                    if (chunked) rewritten.Append("Transfer-Encoding: chunked\r\n");
                    rewritten.Append("Connection: close\r\n\r\n");
                    body = new BodyLimit(length, chunked);
                    int accepted = body.Take(leftover, 0, leftover.Length);
                    byte[] rewrittenBytes = Encoding.ASCII.GetBytes(rewritten.ToString());
                    initial = new byte[rewrittenBytes.Length + accepted];
                    Buffer.BlockCopy(rewrittenBytes, 0, initial, 0, rewrittenBytes.Length);
                    Buffer.BlockCopy(leftover, 0, initial, rewrittenBytes.Length, accepted);
                }
                Direct = owner.policy.SelectRoute(host, out overrideMode, out forcedVpn);
                lock (owner.gate) { interfaceIndex = owner.physicalIndex; binding = owner.vpnBinding; }
                if (!Direct && !owner.ValidateVpn(binding, null)) { Fail(503, "vpn-not-ready"); return; }
                if (Direct && interfaceIndex <= 0 && (interfaceIndex < 0 || literal == null || !IPAddress.IsLoopback(literal)))
                { Fail(503, "physical-interface-unavailable"); return; }
                timeoutSeconds = 10; Touch(); dnsStarted = Stopwatch.GetTimestamp();
                if (literal != null) Resolved(new IPAddress[] { literal }, "literal");
                else if (Direct) owner.resolver.Resolve(host, interfaceIndex, Resolved);
                else ResolveSystem();
            }

            private void ResolveSystem()
            {
                try
                {
                    Dns.BeginGetHostAddresses(host, delegate(IAsyncResult result)
                    {
                        if (IsClosed || IsTerminating) return;
                        try { Resolved(DirectDns.IPv4(Dns.EndGetHostAddresses(result)), "systemDNS"); }
                        catch (Exception) { Fail(502, "dns-failed"); }
                    }, null);
                }
                catch (Exception) { Fail(502, "dns-failed"); }
            }

            private void Resolved(IPAddress[] resolved, string source)
            {
                if (IsClosed || IsTerminating) return;
                dnsMilliseconds = (Stopwatch.GetTimestamp() - dnsStarted) * 1000 / Stopwatch.Frequency;
                if (resolved == null || resolved.Length == 0) { Fail(502, "dns-no-ipv4"); return; }
                if (!Direct && !forcedVpn)
                {
                    List<IPAddress> domestic = new List<IPAddress>();
                    foreach (IPAddress address in resolved) if (owner.policy.IsAutomaticDirect(address.ToString())) domestic.Add(address);
                    if (domestic.Count > 0) { Direct = true; resolved = domestic.ToArray(); source = "CN-IP/" + source; }
                }
                lock (owner.gate)
                {
                    if (Direct && owner.physicalIndex != interfaceIndex) { Fail(503, "network-changed"); return; }
                }
                if (!Direct && !owner.ValidateVpn(binding, null)) { Fail(503, "vpn-binding-unavailable"); return; }
                if (Direct && interfaceIndex <= 0)
                {
                    foreach (IPAddress address in resolved)
                        if (interfaceIndex < 0 || !IPAddress.IsLoopback(address)) { Fail(503, "physical-interface-unavailable"); return; }
                }
                // The watchdog polls once per second; seven seconds bounds the
                // outstanding connect wait to at most eight seconds.
                addresses = resolved; dnsSource = source; connectDeadline = DateTime.UtcNow.AddSeconds(8); timeoutSeconds = 7; Touch(); tcpStarted = Stopwatch.GetTimestamp(); ConnectNext();
            }

            private void ConnectNext()
            {
                if (IsClosed || IsTerminating) return;
                if (addressIndex >= addresses.Length || DateTime.UtcNow >= connectDeadline) { Fail(502, "connect-failed"); return; }
                IPAddress address = addresses[addressIndex++];
                if (!Direct && !owner.ValidateVpn(binding, address)) { Fail(503, "vpn-binding-unavailable"); return; }
                vpnTarget = address;
                Socket socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                upstream = socket;
                try
                {
                    socket.NoDelay = true;
                    if (Direct)
                    {
                        if (interfaceIndex <= 0 && !IPAddress.IsLoopback(address)) throw new InvalidOperationException("Physical interface unavailable.");
                        if (interfaceIndex > 0 && !IPAddress.IsLoopback(address)) socket.SetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31, IPAddress.HostToNetworkOrder(interfaceIndex));
                    }
                    else
                    {
                        try { owner.vpnGuard.Bind(socket, binding); }
                        catch (Exception) { SafeClose(socket); Fail(503, "vpn-bind-failed"); return; }
                    }
                    socket.BeginConnect(new IPEndPoint(address, destinationPort), delegate(IAsyncResult result)
                    {
                        if (IsClosed || IsTerminating) { SafeClose(socket); return; }
                        try { socket.EndConnect(result); Connected(address); }
                        catch (Exception) { SafeClose(socket); ConnectNext(); }
                    }, null);
                }
                catch (Exception) { SafeClose(socket); ConnectNext(); }
            }

            private void Connected(IPAddress address)
            {
                if (IsClosed || IsTerminating) return;
                lock (owner.gate)
                {
                    if (Direct && owner.physicalIndex != interfaceIndex) { Fail(503, "network-changed"); return; }
                }
                if (!Direct && (!owner.ValidateVpn(binding, address) || !owner.vpnGuard.IsBound(upstream, binding)))
                { Fail(503, "vpn-binding-unavailable"); return; }
                string sourceIp = "unknown";
                try { sourceIp = ((IPEndPoint)upstream.LocalEndPoint).Address.ToString(); } catch (Exception) { }
                if (Direct) Interlocked.Increment(ref owner.directCount); else Interlocked.Increment(ref owner.vpnCount);
                long tcpMilliseconds = (Stopwatch.GetTimestamp() - tcpStarted) * 1000 / Stopwatch.Frequency;
                owner.Event((Direct ? "DIRECT " : "VPN ") + host + " IP=" + address + " sourceIP=" + sourceIp + " DNS=" + dnsSource +
                    " DNSms=" + dnsMilliseconds.ToString(CultureInfo.InvariantCulture) + " TCPms=" + tcpMilliseconds.ToString(CultureInfo.InvariantCulture) +
                    (String.IsNullOrEmpty(overrideMode) ? "" : " WHITELIST=" + overrideMode));
                timeoutSeconds = 300; Touch();
                if (tunnel)
                {
                    SendBytes(client, Encoding.ASCII.GetBytes("HTTP/1.1 200 Connection Established\r\n\r\n"), delegate
                    { if (initial.Length > 0) SendBytes(upstream, initial, StartPipes); else StartPipes(); });
                }
                else SendBytes(upstream, initial, StartPipes);
            }

            private void StartPipes()
            {
                if (IsClosed || IsTerminating) return;
                new Pipe(this, upstream, client, null, false).Start();
                if (!tunnel && body.Done) PipeEnded(upstream, false);
                else new Pipe(this, client, upstream, tunnel ? null : body, true).Start();
            }

            private void SendBytes(Socket target, byte[] bytes, Action complete) { new Writer(this, target, bytes, complete).Start(); }
            private void PipeEnded(Socket destination, bool response)
            {
                try { destination.Shutdown(SocketShutdown.Send); } catch (Exception) { }
                if ((!tunnel && response) || Interlocked.Increment(ref pipeCount) >= 2) Close();
            }

            private void Fail(int status, string reason)
            {
                if (IsClosed || Interlocked.Exchange(ref terminating, 1) != 0) return;
                Interlocked.Increment(ref owner.errorCount);
                owner.Event("ERROR " + (host ?? "request") + " mode=" + (Direct ? "DIRECT" : "VPN") + " reason=" + reason +
                    (String.IsNullOrEmpty(overrideMode) ? "" : " WHITELIST=" + overrideMode));
                // Once a tunnel or response is running, do not inject HTTP into the stream.
                if (timeoutSeconds == 300) { Close(); return; }
                SafeClose(upstream);
                Respond(status, "Proxy Error", "text/plain; charset=utf-8", "Temporary split proxy: " + reason + "\n");
            }

            private void Respond(int status, string title, string contentType, string message)
            {
                if (IsClosed) return;
                Interlocked.Exchange(ref terminating, 1);
                byte[] payload = Encoding.UTF8.GetBytes(message);
                byte[] prefix = Encoding.ASCII.GetBytes("HTTP/1.1 " + status.ToString(CultureInfo.InvariantCulture) + " " + title +
                    "\r\nContent-Type: " + contentType + "\r\nContent-Length: " + payload.Length.ToString(CultureInfo.InvariantCulture) + "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n");
                byte[] response = new byte[prefix.Length + payload.Length]; Buffer.BlockCopy(prefix, 0, response, 0, prefix.Length); Buffer.BlockCopy(payload, 0, response, prefix.Length, payload.Length);
                timeoutSeconds = 3; Touch(); SendBytes(client, response, FinishResponse);
            }

            private void FinishResponse()
            {
                if (IsClosed) return;
                // Send FIN before draining pending request bytes so an error response
                // is readable even when the rejected header exceeds our size limit.
                try { client.Shutdown(SocketShutdown.Send); } catch (Exception) { Close(); return; }
                timeoutSeconds = 2; Touch(); DrainRejected(new byte[4096], 65536);
            }

            private void DrainRejected(byte[] discard, int allowance)
            {
                if (IsClosed) return;
                try
                {
                    client.BeginReceive(discard, 0, discard.Length, SocketFlags.None, delegate(IAsyncResult result)
                    {
                        if (IsClosed) return;
                        try
                        {
                            int count = client.EndReceive(result);
                            if (count == 0 || count >= allowance) Close(); else DrainRejected(discard, allowance - count);
                        }
                        catch (Exception) { Close(); }
                    }, null);
                }
                catch (Exception) { Close(); }
            }

            internal void Close()
            {
                if (Interlocked.Exchange(ref closed, 1) != 0) return;
                timeout.Dispose(); SafeClose(client); SafeClose(upstream); owner.Remove(this);
            }

            private sealed class Writer
            {
                private readonly Session session; private readonly Socket socket; private readonly byte[] bytes; private readonly Action complete; private int offset;
                internal Writer(Session session, Socket socket, byte[] bytes, Action complete) { this.session = session; this.socket = socket; this.bytes = bytes; this.complete = complete; }
                internal void Start()
                {
                    if (session.IsClosed) return;
                    if (offset == bytes.Length) { complete(); return; }
                    try { socket.BeginSend(bytes, offset, bytes.Length - offset, SocketFlags.None, Sent, null); } catch (Exception) { session.Close(); }
                }
                private void Sent(IAsyncResult result)
                {
                    if (session.IsClosed) return;
                    try { int count = socket.EndSend(result); if (count <= 0) { session.Close(); return; } offset += count; session.Touch(); Start(); }
                    catch (Exception) { session.Close(); }
                }
            }

            private sealed class Pipe
            {
                private readonly Session session; private readonly Socket source, destination; private readonly BodyLimit limit; private readonly bool request;
                private readonly byte[] buffer = new byte[16384]; private int count, sent;
                internal Pipe(Session session, Socket source, Socket destination, BodyLimit limit, bool request)
                { this.session = session; this.source = source; this.destination = destination; this.limit = limit; this.request = request; }
                internal void Start()
                {
                    if (session.IsClosed) return;
                    try { source.BeginReceive(buffer, 0, buffer.Length, SocketFlags.None, Read, null); } catch (Exception) { session.Close(); }
                }
                private void Read(IAsyncResult result)
                {
                    if (session.IsClosed) return;
                    try
                    {
                        count = source.EndReceive(result); sent = 0; session.Touch();
                        if (count == 0) { session.PipeEnded(destination, !request); return; }
                        if (limit != null) count = limit.Take(buffer, 0, count);
                        if (count == 0) { session.PipeEnded(destination, !request); return; }
                        Send();
                    }
                    catch (Exception) { session.Close(); }
                }
                private void Send()
                {
                    if (session.IsClosed) return;
                    try { destination.BeginSend(buffer, sent, count - sent, SocketFlags.None, Written, null); } catch (Exception) { session.Close(); }
                }
                private void Written(IAsyncResult result)
                {
                    if (session.IsClosed) return;
                    try
                    {
                        int n = destination.EndSend(result); if (n <= 0) { session.Close(); return; } sent += n; session.Touch();
                        if (sent < count) Send();
                        else if (limit != null && limit.Done) session.PipeEnded(destination, !request);
                        else Start();
                    }
                    catch (Exception) { session.Close(); }
                }
            }
        }

        // Limits uploads to one HTTP message, including chunked framing and trailers.
        private sealed class BodyLimit
        {
            private long remaining; private readonly bool chunked; private int state, trailerBytes; private readonly StringBuilder line = new StringBuilder();
            internal bool Done { get; private set; }
            internal BodyLimit(long length, bool chunked) { remaining = length; this.chunked = chunked; Done = !chunked && length == 0; }
            internal int Take(byte[] data, int offset, int count)
            {
                if (Done) return 0;
                if (!chunked) { int accepted = (int)Math.Min(remaining, count); remaining -= accepted; Done = remaining == 0; return accepted; }
                int used = 0;
                for (; used < count && !Done; used++)
                {
                    byte b = data[offset + used];
                    if (state == 1) { if (--remaining == 0) state = 2; continue; }
                    if (state == 2) { if (b != 13) throw new FormatException(); state = 3; continue; }
                    if (state == 3) { if (b != 10) throw new FormatException(); state = 0; continue; }
                    if (state == 4 && ++trailerBytes > MaxHeader) throw new FormatException();
                    if (b > 127 || b == 0 || line.Length > 8192) throw new FormatException();
                    line.Append((char)b);
                    if (b != 10) continue;
                    if (line.Length < 2 || line[line.Length - 2] != '\r') throw new FormatException();
                    string value = line.ToString(0, line.Length - 2); line.Length = 0;
                    if (state == 4) { if (value.Length == 0) Done = true; continue; }
                    int semicolon = value.IndexOf(';'); if (semicolon >= 0) value = value.Substring(0, semicolon);
                    if (value.Length == 0 || !Int64.TryParse(value.Trim(), NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out remaining) || remaining < 0) throw new FormatException();
                    state = remaining == 0 ? 4 : 1;
                }
                return used;
            }
        }

        private sealed class DirectDns
        {
            private sealed class CacheEntry { internal IPAddress[] Addresses; internal DateTime Expires; }
            private sealed class Flight
            {
                internal string Key, Host;
                internal int Interface, Generation, Attempt;
                internal bool Done, HasSlot, FallbackStarted, Queued;
                internal DateTime DoHDeadline;
                internal Timer Timer;
                internal Action CancelProbe;
                internal readonly List<Action<IPAddress[], string>> Waiters = new List<Action<IPAddress[], string>>();
            }
            private readonly object gate = new object();
            private readonly Dictionary<string, CacheEntry> cache = new Dictionary<string, CacheEntry>();
            private readonly Dictionary<int, DateTime> failureCooldown = new Dictionary<int, DateTime>();
            private readonly Dictionary<string, Flight> flights = new Dictionary<string, Flight>();
            private readonly HashSet<Flight> pending = new HashSet<Flight>();
            private readonly Queue<Flight> waiting = new Queue<Flight>();
            private readonly Func<string, int, string, int, Action<IPAddress[], uint, string>, Action> startProbe;
            private int generation, activeProbes;
            private bool stopped;

            internal DirectDns() : this(null) { }
            // The overload provides a socket-free transport for the regression harness.
            private DirectDns(Func<string, int, string, int, Action<IPAddress[], uint, string>, Action> probe)
            {
                startProbe = probe ?? delegate(string host, int index, string server, int milliseconds, Action<IPAddress[], uint, string> complete)
                {
                    DnsQuery query = new DnsQuery(host, index, server, milliseconds, complete);
                    query.Start(); return query.Cancel;
                };
            }
            internal void Restart() { Reset(false); }
            internal void ChangeInterface() { Reset(false); }
            internal void Clear() { Reset(true); }
            private void Reset(bool stop)
            {
                Flight[] cancel;
                lock (gate)
                {
                    stopped = stop; generation++; cache.Clear(); failureCooldown.Clear();
                    cancel = new List<Flight>(pending).ToArray();
                    foreach (Flight flight in cancel) flight.Done = true;
                    pending.Clear(); flights.Clear(); waiting.Clear(); activeProbes = 0;
                }
                foreach (Flight flight in cancel)
                {
                    if (flight.Timer != null) flight.Timer.Dispose();
                    if (flight.CancelProbe != null) flight.CancelProbe();
                }
            }
            internal static IPAddress[] IPv4(IPAddress[] input)
            {
                List<IPAddress> result = new List<IPAddress>();
                foreach (IPAddress address in input) if (address.AddressFamily == AddressFamily.InterNetwork && !result.Contains(address)) result.Add(address);
                return result.ToArray();
            }
            internal void Resolve(string host, int interfaceIndex, Action<IPAddress[], string> complete)
            {
                string key = interfaceIndex.ToString(CultureInfo.InvariantCulture) + "/" + host;
                IPAddress[] cached = null;
                Flight flight = null; string fallback = null; bool queued = false;
                lock (gate)
                {
                    if (stopped) return;
                    CacheEntry entry;
                    if (cache.TryGetValue(key, out entry) && entry.Expires > DateTime.UtcNow) cached = entry.Addresses;
                    else cache.Remove(key);
                    if (cached == null)
                    {
                        if (flights.TryGetValue(key, out flight)) { flight.Waiters.Add(complete); return; }
                        flight = new Flight { Key = key, Host = host, Interface = interfaceIndex, Generation = generation,
                            DoHDeadline = DateTime.UtcNow.AddMilliseconds(1800) };
                        flight.Waiters.Add(complete); flights.Add(key, flight); pending.Add(flight);
                        if (interfaceIndex <= 0) fallback = "systemDNS-fallback/degraded/no-interface";
                        else if (IsCoolingDown(interfaceIndex)) fallback = "systemDNS-fallback/degraded/cooldown";
                        else if (activeProbes >= 16) { flight.Queued = true; queued = true; waiting.Enqueue(flight); }
                        else { flight.HasSlot = true; activeProbes++; }
                        Flight current = flight;
                        flight.Timer = new Timer(delegate(object unused)
                        { Finish(current, null, 0, "systemDNS-fallback/degraded/timeout"); }, null, 2500, Timeout.Infinite);
                    }
                }
                if (cached != null) { complete(cached, "physicalDoH/cache"); return; }
                if (fallback != null) Fallback(flight, fallback); else if (!queued) Query(flight, 0);
            }
            private bool Active(Flight flight)
            { return !stopped && !flight.Done && flight.Generation == generation; }
            private void Finish(Flight flight, IPAddress[] addresses, uint ttl, string source)
            {
                Action<IPAddress[], string>[] waiters;
                Action cancel; bool released;
                lock (gate)
                {
                    if (!Active(flight)) return;
                    flight.Done = true; flights.Remove(flight.Key); pending.Remove(flight);
                    released = flight.HasSlot;
                    if (released) { flight.HasSlot = false; activeProbes--; }
                    if (addresses != null && addresses.Length > 0 && ttl > 0 && !flight.FallbackStarted)
                    {
                        if (cache.Count >= 2048) cache.Clear();
                        cache[flight.Key] = new CacheEntry { Addresses = addresses,
                            Expires = DateTime.UtcNow.AddSeconds(Math.Min(ttl, 300U)) };
                    }
                    waiters = flight.Waiters.ToArray(); flight.Waiters.Clear(); cancel = flight.CancelProbe; flight.CancelProbe = null;
                }
                if (flight.Timer != null) flight.Timer.Dispose();
                if (cancel != null) cancel();
                if (released) Pump();
                foreach (Action<IPAddress[], string> waiter in waiters)
                    try { waiter(addresses, source); } catch (Exception) { }
            }
            // A failed interface is retried after 60 seconds. Unknown-TTL system
            // fallback answers are never put into our physical-DoH cache.
            private bool MarkFailure(int interfaceIndex)
            {
                lock (gate)
                {
                    if (stopped) return false;
                    failureCooldown[interfaceIndex] = DateTime.UtcNow.AddSeconds(60);
                    return true;
                }
            }
            private bool IsCoolingDown(int interfaceIndex)
            {
                lock (gate)
                {
                    DateTime until;
                    if (!failureCooldown.TryGetValue(interfaceIndex, out until)) return false;
                    if (until > DateTime.UtcNow) return true;
                    failureCooldown.Remove(interfaceIndex); return false;
                }
            }
            private void Fallback(Flight flight, string source)
            {
                Action cancel; bool released;
                lock (gate)
                {
                    if (!Active(flight) || flight.FallbackStarted) return;
                    flight.FallbackStarted = true;
                    released = flight.HasSlot;
                    if (released) { flight.HasSlot = false; activeProbes--; }
                    cancel = flight.CancelProbe; flight.CancelProbe = null;
                }
                if (cancel != null) cancel();
                if (released) Pump();
                try
                {
                    Dns.BeginGetHostAddresses(flight.Host, delegate(IAsyncResult result)
                    {
                        try { Finish(flight, IPv4(Dns.EndGetHostAddresses(result)), 0, source); }
                        catch (Exception) { Finish(flight, null, 0, source + "-failed"); }
                    }, null);
                }
                catch (Exception) { Finish(flight, null, 0, source + "-failed"); }
            }
            private void Pump()
            {
                List<Flight> start = new List<Flight>(), degraded = new List<Flight>();
                lock (gate)
                {
                    while (!stopped && activeProbes < 16 && waiting.Count > 0)
                    {
                        Flight flight = waiting.Dequeue();
                        if (!Active(flight) || flight.FallbackStarted) continue;
                        flight.Queued = false;
                        if (IsCoolingDown(flight.Interface)) degraded.Add(flight);
                        else { flight.HasSlot = true; activeProbes++; start.Add(flight); }
                    }
                }
                foreach (Flight flight in degraded) Fallback(flight, "systemDNS-fallback/degraded/cooldown");
                foreach (Flight flight in start) Query(flight, 0);
            }
            private void Query(Flight flight, int attempt)
            {
                int milliseconds;
                lock (gate)
                {
                    if (!Active(flight) || flight.FallbackStarted) return;
                    milliseconds = (int)Math.Min(900, (flight.DoHDeadline - DateTime.UtcNow).TotalMilliseconds);
                    flight.Attempt = attempt;
                }
                if (attempt >= 2 || milliseconds <= 0)
                {
                    lock (gate)
                    {
                        if (!Active(flight)) return;
                        // Waiting for a busy slot is not evidence of DNS failure.
                        if (attempt > 0) failureCooldown[flight.Interface] = DateTime.UtcNow.AddSeconds(60);
                    }
                    Fallback(flight, attempt > 0 ? "systemDNS-fallback/degraded/DoH-failed" : "systemDNS-fallback/degraded/queue-timeout"); return;
                }
                Action cancel = null;
                try
                {
                    cancel = startProbe(flight.Host, flight.Interface, attempt == 0 ? "223.5.5.5" : "223.6.6.6", milliseconds,
                        delegate(IPAddress[] addresses, uint ttl, string reason)
                        {
                            lock (gate) if (!Active(flight) || flight.FallbackStarted || flight.Attempt != attempt) return;
                            if (addresses != null) Finish(flight, addresses, ttl, addresses.Length == 0 ? "physicalDoH/no-A" : "physicalDoH/cold");
                            else Query(flight, attempt + 1);
                        });
                }
                catch (Exception) { Query(flight, attempt + 1); }
                bool keep;
                lock (gate)
                {
                    keep = Active(flight) && !flight.FallbackStarted && flight.Attempt == attempt;
                    if (keep) flight.CancelProbe = cancel;
                }
                if (!keep && cancel != null) cancel();
            }

            private sealed class DnsQuery
            {
                private readonly string host, server; private readonly int interfaceIndex;
                private readonly Action<IPAddress[], uint, string> complete; private Socket socket; private Timer timer; private int done;
                private SslStream stream;
                private readonly int milliseconds, serverPort;
                private readonly object queryGate = new object();
                private readonly byte[] buffer = new byte[4096];
                private readonly DnsHttpReply reply = new DnsHttpReply();
                internal DnsQuery(string host, int interfaceIndex, string server, int milliseconds, Action<IPAddress[], uint, string> complete)
                    : this(host, interfaceIndex, server, milliseconds, complete, 443) { }
                private DnsQuery(string host, int interfaceIndex, string server, int milliseconds, Action<IPAddress[], uint, string> complete, int port)
                { this.host = host; this.interfaceIndex = interfaceIndex; this.server = server; this.milliseconds = milliseconds; this.complete = complete; serverPort = port; }
                internal void Start()
                {
                    lock (queryGate)
                    {
                        if (Interlocked.CompareExchange(ref done, 0, 0) != 0) return;
                        try
                        {
                            socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                            socket.NoDelay = true;
                            socket.SetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31, IPAddress.HostToNetworkOrder(interfaceIndex));
                            timer = new Timer(delegate(object unused) { Finish(null, 0, "timeout"); }, null, milliseconds, Timeout.Infinite);
                            socket.BeginConnect(new IPEndPoint(IPAddress.Parse(server), serverPort), Connected, null);
                        }
                        catch (Exception) { Finish(null, 0, "connect"); }
                    }
                }
                internal void Cancel()
                {
                    Close(false, null, 0, "cancelled");
                }
                private bool IsDone { get { return Interlocked.CompareExchange(ref done, 0, 0) != 0; } }
                private void Connected(IAsyncResult result)
                {
                    try
                    {
                        socket.EndConnect(result);
                        lock (queryGate)
                        {
                            if (IsDone) return;
                            // The default SslStream validator checks both the certificate
                            // chain and dns.alidns.com hostname. No permissive callback.
                            stream = new SslStream(new NetworkStream(socket, false), false);
                            stream.BeginAuthenticateAsClient("dns.alidns.com", null, SslProtocols.Tls12, false, Authenticated, null);
                        }
                    }
                    catch (Exception) { Finish(null, 0, "TLS-connect"); }
                }
                private void Authenticated(IAsyncResult result)
                {
                    try
                    {
                        stream.EndAuthenticateAsClient(result); if (IsDone) return;
                        byte[] query = MakeQuery(host);
                        string encoded = Convert.ToBase64String(query).TrimEnd('=').Replace('+', '-').Replace('/', '_');
                        byte[] request = Encoding.ASCII.GetBytes("GET /dns-query?dns=" + encoded + " HTTP/1.1\r\nHost: dns.alidns.com\r\nAccept: application/dns-message\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n");
                        stream.BeginWrite(request, 0, request.Length, Written, null);
                    }
                    catch (Exception) { Finish(null, 0, "TLS-validation"); }
                }
                private void Written(IAsyncResult result)
                {
                    try { stream.EndWrite(result); if (!IsDone) stream.BeginRead(buffer, 0, buffer.Length, Received, null); }
                    catch (Exception) { Finish(null, 0, "HTTPS-write"); }
                }
                private void Received(IAsyncResult result)
                {
                    try
                    {
                        int count = stream.EndRead(result); if (IsDone) return;
                        reply.Add(buffer, count); byte[] body;
                        if (reply.TryBody(count == 0, out body))
                        {
                            uint ttl; IPAddress[] addresses = Parse(body, body.Length, 0, host, out ttl);
                            ttl = ttl > reply.Age ? ttl - reply.Age : 0;
                            Finish(addresses ?? new IPAddress[0], ttl, "ok");
                        }
                        else stream.BeginRead(buffer, 0, buffer.Length, Received, null);
                    }
                    catch (Exception) { Finish(null, 0, "HTTPS-response"); }
                }
                private void Finish(IPAddress[] addresses, uint ttl, string reason) { Close(true, addresses, ttl, reason); }
                private void Close(bool notify, IPAddress[] addresses, uint ttl, string reason)
                {
                    SslStream closeStream; Socket closeSocket;
                    lock (queryGate)
                    {
                        if (Interlocked.Exchange(ref done, 1) != 0) return;
                        if (timer != null) timer.Dispose(); closeStream = stream; closeSocket = socket;
                    }
                    SafeClose(closeSocket);
                    if (closeStream != null) try { closeStream.Dispose(); } catch (Exception) { }
                    if (notify) complete(addresses, ttl, reason);
                }
            }

            private static byte[] MakeQuery(string host)
            {
                List<byte> query = new List<byte>(64);
                // RFC8484 uses the HTTP exchange to correlate replies; fixed ID=0
                // also allows the provider to cache equivalent GET requests.
                query.AddRange(new byte[] { 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0 });
                foreach (string label in host.Split('.'))
                { byte[] bytes = Encoding.ASCII.GetBytes(label); if (bytes.Length == 0 || bytes.Length > 63) throw new FormatException(); query.Add((byte)bytes.Length); query.AddRange(bytes); }
                query.AddRange(new byte[] { 0, 0, 1, 0, 1 }); return query.ToArray();
            }

            private sealed class DnsHttpReply
            {
                private readonly MemoryStream data = new MemoryStream();
                internal uint Age { get; private set; }
                internal void Add(byte[] bytes, int count)
                { if (data.Length + count > 98304) throw new FormatException(); data.Write(bytes, 0, count); }
                internal bool TryBody(bool eof, out byte[] body)
                {
                    body = null; byte[] bytes = data.ToArray(); int start = -1;
                    for (int i = 3; i < bytes.Length; i++)
                        if (bytes[i - 3] == 13 && bytes[i - 2] == 10 && bytes[i - 1] == 13 && bytes[i] == 10) { start = i + 1; break; }
                    if (start < 0) { if (bytes.Length > 8192 || eof) throw new FormatException(); return false; }
                    if (start > 8192) throw new FormatException();
                    for (int i = 0; i < start; i++) if (bytes[i] == 0 || bytes[i] > 127) throw new FormatException();
                    string[] headers = Encoding.ASCII.GetString(bytes, 0, start - 4).Split(new string[] { "\r\n" }, StringSplitOptions.None);
                    string[] status = headers[0].Split(' '); if (status.Length < 2 || (status[0] != "HTTP/1.1" && status[0] != "HTTP/1.0") || status[1] != "200") throw new FormatException();
                    int length = -1; bool chunked = false, contentType = false;
                    for (int i = 1; i < headers.Length; i++)
                    {
                        int colon = headers[i].IndexOf(':'); if (colon <= 0) throw new FormatException();
                        string name = headers[i].Substring(0, colon).Trim(), value = headers[i].Substring(colon + 1).Trim();
                        if (name.Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                        { int n; if (!Int32.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out n) || n < 12 || n > 65535 || (length >= 0 && length != n)) throw new FormatException(); length = n; }
                        else if (name.Equals("Content-Type", StringComparison.OrdinalIgnoreCase))
                            contentType = value.Split(';')[0].Trim().Equals("application/dns-message", StringComparison.OrdinalIgnoreCase);
                        else if (name.Equals("Transfer-Encoding", StringComparison.OrdinalIgnoreCase))
                        { if (chunked || !value.Equals("chunked", StringComparison.OrdinalIgnoreCase)) throw new FormatException(); chunked = true; }
                        else if (name.Equals("Content-Encoding", StringComparison.OrdinalIgnoreCase) && !value.Equals("identity", StringComparison.OrdinalIgnoreCase)) throw new FormatException();
                        else if (name.Equals("Age", StringComparison.OrdinalIgnoreCase))
                        { uint age; if (!UInt32.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out age)) throw new FormatException(); Age = age; }
                    }
                    if (!contentType || (chunked && length >= 0)) throw new FormatException();
                    if (chunked)
                    {
                        using (MemoryStream decoded = new MemoryStream())
                        {
                            int position = start;
                            while (true)
                            {
                                int end = FindLine(bytes, position); if (end < 0) { if (eof) throw new FormatException(); return false; }
                                string sizeText = Encoding.ASCII.GetString(bytes, position, end - position).Split(';')[0].Trim(); int size;
                                if (sizeText.Length == 0 || !Int32.TryParse(sizeText, NumberStyles.AllowHexSpecifier, CultureInfo.InvariantCulture, out size) || size < 0 || decoded.Length + size > 65535) throw new FormatException();
                                position = end + 2;
                                if (size == 0)
                                {
                                    int trailerStart = position;
                                    while (true)
                                    {
                                        end = FindLine(bytes, position); if (end < 0) { if (eof) throw new FormatException(); return false; }
                                        if (end + 2 - trailerStart > 8192) throw new FormatException();
                                        if (end == position) { if (decoded.Length < 12) throw new FormatException(); body = decoded.ToArray(); return true; }
                                        position = end + 2;
                                    }
                                }
                                if (bytes.Length - position < size + 2) { if (eof) throw new FormatException(); return false; }
                                if (bytes[position + size] != 13 || bytes[position + size + 1] != 10) throw new FormatException();
                                decoded.Write(bytes, position, size); position += size + 2;
                            }
                        }
                    }
                    int available = bytes.Length - start;
                    if (length < 0) { if (!eof) return false; length = available; if (length < 12 || length > 65535) throw new FormatException(); }
                    if (available < length) { if (eof) throw new FormatException(); return false; }
                    body = new byte[length]; Buffer.BlockCopy(bytes, start, body, 0, length); return true;
                }
                private static int FindLine(byte[] bytes, int start)
                { for (int i = start + 1; i < bytes.Length; i++) if (bytes[i - 1] == 13 && bytes[i] == 10) return i - 1; return -1; }
            }

            private static ushort Read16(byte[] bytes, int offset, int count)
            { if (offset < 0 || offset + 2 > count) throw new FormatException(); return (ushort)((bytes[offset] << 8) | bytes[offset + 1]); }
            private static uint Read32(byte[] bytes, int offset, int count)
            { return ((uint)Read16(bytes, offset, count) << 16) | Read16(bytes, offset + 2, count); }
            private static string ReadName(byte[] bytes, int count, ref int position)
            {
                int cursor = position, steps = 0; bool jumped = false; StringBuilder name = new StringBuilder();
                HashSet<int> seen = new HashSet<int>();
                while (true)
                {
                    if (cursor < 0 || cursor >= count || ++steps > 128 || !seen.Add(cursor)) throw new FormatException();
                    byte length = bytes[cursor++];
                    if ((length & 0xC0) == 0xC0)
                    {
                        if (cursor >= count) throw new FormatException(); int pointer = ((length & 63) << 8) | bytes[cursor++];
                        if (!jumped) position = cursor; jumped = true; cursor = pointer; continue;
                    }
                    if ((length & 0xC0) != 0) throw new FormatException();
                    if (length == 0) { if (!jumped) position = cursor; return name.ToString().ToLowerInvariant(); }
                    if (cursor + length > count || name.Length + length + 1 > 254) throw new FormatException();
                    if (name.Length > 0) name.Append('.');
                    for (int i = 0; i < length; i++) { byte b = bytes[cursor++]; if (b <= 32 || b > 126) throw new FormatException(); name.Append((char)b); }
                }
            }
            private static IPAddress[] Parse(byte[] bytes, int count, ushort id, string host, out uint ttl)
            {
                ttl = 0;
                if (count < 12 || Read16(bytes, 0, count) != id) throw new FormatException();
                ushort flags = Read16(bytes, 2, count);
                int rcode = flags & 15;
                if ((flags & 0x8000) == 0 || (flags & 0x7A00) != 0 || (rcode != 0 && rcode != 3) || Read16(bytes, 4, count) != 1) throw new FormatException();
                int position = 12; string question = ReadName(bytes, count, ref position);
                if (question != host || Read16(bytes, position, count) != 1 || Read16(bytes, position + 2, count) != 1) throw new FormatException(); position += 4;
                int answers = Read16(bytes, 6, count); if (answers > 256) throw new FormatException();
                Dictionary<string, string> aliases = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                Dictionary<string, List<IPAddress>> addresses = new Dictionary<string, List<IPAddress>>(StringComparer.OrdinalIgnoreCase);
                Dictionary<string, uint> lifetimes = new Dictionary<string, uint>(StringComparer.OrdinalIgnoreCase);
                for (int i = 0; i < answers; i++)
                {
                    string name = ReadName(bytes, count, ref position); int type = Read16(bytes, position, count), cls = Read16(bytes, position + 2, count);
                    uint recordTtl = Read32(bytes, position + 4, count); int length = Read16(bytes, position + 8, count); position += 10;
                    int end = position + length; if (end > count) throw new FormatException();
                    if (cls == 1 && type == 1 && length == 4)
                    {
                        List<IPAddress> list; if (!addresses.TryGetValue(name, out list)) { list = new List<IPAddress>(); addresses[name] = list; }
                        list.Add(new IPAddress(new byte[] { bytes[position], bytes[position + 1], bytes[position + 2], bytes[position + 3] }));
                        uint previous; lifetimes[name] = lifetimes.TryGetValue(name, out previous) ? Math.Min(previous, recordTtl) : recordTtl;
                    }
                    else if (cls == 1 && type == 5)
                    {
                        int aliasPosition = position; string alias = ReadName(bytes, count, ref aliasPosition); if (aliasPosition != end) throw new FormatException(); aliases[name] = alias;
                        uint previous; lifetimes[name] = lifetimes.TryGetValue(name, out previous) ? Math.Min(previous, recordTtl) : recordTtl;
                    }
                    position = end;
                }
                string current = host; uint minimum = 300; HashSet<string> visited = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                for (int i = 0; i < 32 && visited.Add(current); i++)
                {
                    uint lifetime; if (lifetimes.TryGetValue(current, out lifetime)) minimum = Math.Min(minimum, lifetime);
                    List<IPAddress> list; if (addresses.TryGetValue(current, out list)) { ttl = minimum; return IPv4(list.ToArray()); }
                    string alias; if (!aliases.TryGetValue(current, out alias)) break; current = alias;
                }
                return null;
            }
        }
    }
}
