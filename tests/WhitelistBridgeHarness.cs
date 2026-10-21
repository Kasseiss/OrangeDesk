namespace FastOrangeCombined.Tests
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Net;
    using System.Net.Sockets;
    using System.Text;
    using System.Threading;

    public static class WhitelistBridgeHarness
    {
        private sealed class LoopbackVpnGuard : IVpnGuard
        {
            public bool IsLive(VpnBinding binding, IPAddress target)
            { return binding != null && IPAddress.IsLoopback(binding.Address) && (target == null || IPAddress.IsLoopback(target)); }
            public void Bind(Socket socket, VpnBinding binding) { socket.Bind(new IPEndPoint(IPAddress.Loopback, 0)); }
            public bool IsBound(Socket socket, VpnBinding binding)
            { return IPAddress.IsLoopback(((IPEndPoint)socket.LocalEndPoint).Address); }
        }
        private static int checks;
        private static void Check(bool condition, string name)
        { if (!condition) throw new Exception("FAIL " + name); checks++; Console.WriteLine("PASS " + name); }
        private static string Header(NetworkStream stream)
        {
            List<byte> bytes = new List<byte>();
            while (bytes.Count < 65536)
            {
                int value = stream.ReadByte(); if (value < 0) throw new EndOfStreamException(); bytes.Add((byte)value);
                int count = bytes.Count;
                if (count >= 4 && bytes[count - 4] == 13 && bytes[count - 3] == 10 && bytes[count - 2] == 13 && bytes[count - 1] == 10)
                    return Encoding.ASCII.GetString(bytes.ToArray());
            }
            throw new Exception("Oversized test reply.");
        }
        private static void EchoAccept(IAsyncResult result)
        {
            TcpListener listener = (TcpListener)result.AsyncState; TcpClient client;
            try { client = listener.EndAcceptTcpClient(result); listener.BeginAcceptTcpClient(EchoAccept, listener); }
            catch (ObjectDisposedException) { return; }
            catch (SocketException) { return; }
            ThreadPool.QueueUserWorkItem(delegate(object unused)
            {
                using (client)
                {
                    try
                    {
                        client.ReceiveTimeout = 5000; NetworkStream stream = client.GetStream(); byte[] bytes = new byte[2048]; int count;
                        while ((count = stream.Read(bytes, 0, bytes.Length)) > 0) stream.Write(bytes, 0, count);
                    }
                    catch (IOException) { }
                }
            });
        }
        private static TcpClient Tunnel(int bridgePort, int destinationPort, string host)
        {
            TcpClient client = new TcpClient("127.0.0.1", bridgePort); client.ReceiveTimeout = 5000;
            NetworkStream stream = client.GetStream();
            byte[] request = Encoding.ASCII.GetBytes("CONNECT " + host + ":" + destinationPort + " HTTP/1.1\r\nHost: " + host + "\r\n\r\n");
            stream.Write(request, 0, request.Length);
            string response = Header(stream);
            if (!response.StartsWith("HTTP/1.1 200", StringComparison.Ordinal)) { client.Close(); throw new Exception(response); }
            return client;
        }
        private static void Echo(TcpClient client, string message)
        {
            NetworkStream stream = client.GetStream(); byte[] bytes = Encoding.ASCII.GetBytes(message); stream.Write(bytes, 0, bytes.Length);
            byte[] received = new byte[bytes.Length]; int position = 0;
            while (position < received.Length) { int count = stream.Read(received, position, received.Length - position); if (count == 0) throw new EndOfStreamException(); position += count; }
            Check(message == Encoding.ASCII.GetString(received), "active tunnel passes opaque data " + message);
        }
        private static string Error(int bridgePort, int destinationPort, string host)
        {
            using (TcpClient client = new TcpClient("127.0.0.1", bridgePort))
            {
                client.ReceiveTimeout = 5000; NetworkStream stream = client.GetStream();
                byte[] request = Encoding.ASCII.GetBytes("CONNECT " + host + ":" + destinationPort + " HTTP/1.1\r\n\r\n");
                stream.Write(request, 0, request.Length); string response = Header(stream);
                using (MemoryStream body = new MemoryStream()) { byte[] bytes = new byte[4096]; int count; while ((count = stream.Read(bytes, 0, bytes.Length)) > 0) body.Write(bytes, 0, count); return response + Encoding.ASCII.GetString(body.ToArray()); }
            }
        }
        private static bool EventHas(string[] events, string route, string evidence)
        { foreach (string entry in events) if (entry.Contains(route) && entry.Contains(evidence)) return true; return false; }
        private static void Reject(DomainPolicy policy, string[] hosts, string[] modes, bool[] scopes, string message)
        {
            bool rejected = false; try { policy.SetOverrides(hosts, modes, scopes); } catch (ArgumentException) { rejected = true; }
            Check(rejected && policy.GetOverride("127.0.0.1") == "vpn" && policy.OverrideCount == 1, message);
        }
        public static int Run()
        {
            checks = 0;
            DomainPolicy policy = new DomainPolicy("domain:example.com\nregexp:^regex[0-9]+\\.test$\nopenai.com", "127.0.0.0/8\n1.0.1.0/24");
            policy.SetOverrides(new string[] { "example.com", "child.example.com", "child.example.com", "deep.child.example.com", "openai.com", "regex1.test", "1.0.1.8" },
                new string[] { "vpn", "vpn", "direct", "direct", "direct", "vpn", "vpn" }, new bool[] { true, true, false, true, true, false, false });
            Check(policy.GetOverride("child.example.com") == "direct" && policy.GetOverride("x.child.example.com") == "vpn", "same-host exact precedes suffix");
            Check(policy.GetOverride("x.deep.child.example.com") == "direct" && policy.GetOverride("example.com") == "vpn", "longest suffix precedes parent and includes root");
            Check(policy.GetOverride("notexample.com") == "" && policy.GetOverride("example.com.evil.test") == "", "literal suffix boundaries");
            Check(!policy.IsDirect("example.com") && !policy.IsDirect("regex1.test") && !policy.IsDirect("1.0.1.8"), "explicit VPN overrides automatic domain regex and CN IP");
            Check(policy.IsDirect("api.openai.com") && !policy.IsForcedVpn("api.openai.com"), "explicit direct overrides built-in VPN suffix");
            Check(policy.GetOverride("API.OPENAI.COM.") == "direct", "request host normalization remains supported");
            policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "vpn" }, new bool[] { false });
            Reject(policy, new string[] { "example.com" }, new string[0], new bool[] { true }, "mismatched arrays reject atomically");
            Reject(policy, new string[] { "API.OPENAI.COM" }, new string[] { "direct" }, new bool[] { false }, "noncanonical host rejects atomically");
            Reject(policy, new string[] { "127.0.0.1" }, new string[] { "direct" }, new bool[] { true }, "IPv4 suffix rejects atomically");
            Reject(policy, new string[] { "a.test", "a.test" }, new string[] { "direct", "vpn" }, new bool[] { false, false }, "duplicate scope rejects atomically");
            string[] manyHosts = new string[1001], manyModes = new string[1001]; bool[] manyScopes = new bool[1001];
            Reject(policy, manyHosts, manyModes, manyScopes, "1000-rule limit rejects atomically");

            TcpListener echo = new TcpListener(IPAddress.Loopback, 0); echo.Start(); int echoPort = ((IPEndPoint)echo.LocalEndpoint).Port;
            echo.BeginAcceptTcpClient(EchoAccept, echo);
            try
            {
                using (BridgeServer bridge = new BridgeServer(policy, "whitelisttests123", new LoopbackVpnGuard()))
                {
                    int port = bridge.Start(); bridge.Configure(0, 1, "00000000-0000-0000-0000-000000000001", "127.0.0.1");
                    using (TcpClient active = Tunnel(port, echoPort, "127.0.0.1"))
                    {
                        Echo(active, "before-refresh");
                        Check(bridge.VpnCount == 1 && bridge.DirectCount == 0 && EventHas(bridge.DrainEvents(), "VPN 127.0.0.1 ", "WHITELIST=vpn"), "forced VPN literal loopback avoids LAN/CN reclassification");
                        policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "direct" }, new bool[] { false });
                        Echo(active, "after-refresh");
                        using (TcpClient direct = Tunnel(port, echoPort, "127.0.0.1")) { Echo(direct, "new-direct"); }
                        Check(bridge.VpnCount == 1 && bridge.DirectCount == 1 && EventHas(bridge.DrainEvents(), "DIRECT 127.0.0.1 ", "WHITELIST=direct"), "new stream uses replacement; existing VPN stream survives");
                        policy.SetOverrides(new string[0], new string[0], new bool[0]);
                        Echo(active, "after-removal");
                    }
                    policy.SetOverrides(new string[] { "localhost" }, new string[] { "vpn" }, new bool[] { false });
                    using (TcpClient systemDns = Tunnel(port, echoPort, "localhost")) { Echo(systemDns, "vpn-system-dns"); }
                    string[] entries = bridge.DrainEvents();
                    Check(EventHas(entries, "VPN localhost ", "DNS=systemDNS") && EventHas(entries, "VPN localhost ", "WHITELIST=vpn"), "forced VPN system DNS loopback avoids CN IP fallback");
                    bridge.Configure(0, false);
                    Check(Error(port, echoPort, "localhost").Contains("vpn-not-ready") && EventHas(bridge.DrainEvents(), "ERROR localhost ", "WHITELIST=vpn"), "forced VPN respects VPN-ready failure");
                    policy.SetOverrides(new string[] { "api.openai.com" }, new string[] { "direct" }, new bool[] { false }); bridge.Configure(-1, false);
                    Check(Error(port, 443, "api.openai.com").Contains("physical-interface-unavailable") && EventHas(bridge.DrainEvents(), "ERROR api.openai.com mode=DIRECT", "WHITELIST=direct"), "built-in VPN host forced direct requires physical path without mode fallback");
                    policy.SetOverrides(new string[0], new string[0], new bool[0]);
                    Check(!policy.IsDirect("api.openai.com") && policy.OverrideCount == 0, "removal restores automatic policy");
                }
            }
            finally { echo.Stop(); }
            Console.WriteLine("ALL " + checks + " WHITELIST BRIDGE CHECKS PASSED"); return checks;
        }
    }
}
