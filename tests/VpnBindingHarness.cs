namespace FastOrangeCombined.Tests
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Net;
    using System.Net.Sockets;
    using System.Net.NetworkInformation;
    using System.Text;
    using System.Threading;

    public static class VpnBindingHarness
    {
        private const string TestGuid = "00000000-0000-0000-0000-000000000001";
        private static int checks, accepts;
        private static TcpListener echo;
        private static int echoPort;

        private sealed class Guard : IVpnGuard
        {
            internal volatile bool Live = true, RouteLive = true, BindFails, Bound = true;
            internal int BindCalls, TargetChecks;
            internal int DropOnTargetCheck;
            internal int NullChecks, DropOnNullCheck;
            public bool IsLive(VpnBinding binding, IPAddress target)
            {
                if (target == null)
                {
                    int count = Interlocked.Increment(ref NullChecks);
                    if (DropOnNullCheck > 0 && count >= DropOnNullCheck) Live = false;
                }
                if (target != null)
                {
                    int count = Interlocked.Increment(ref TargetChecks);
                    if (DropOnTargetCheck > 0 && count >= DropOnTargetCheck) RouteLive = false;
                }
                return Live && binding != null && IPAddress.IsLoopback(binding.Address) &&
                    (target == null || (IPAddress.IsLoopback(target) && RouteLive));
            }
            public void Bind(Socket socket, VpnBinding binding)
            {
                Interlocked.Increment(ref BindCalls);
                if (BindFails) throw new SocketException();
                socket.Bind(new IPEndPoint(IPAddress.Loopback, 0));
            }
            public bool IsBound(Socket socket, VpnBinding binding)
            { return Bound && ((IPEndPoint)socket.LocalEndPoint).Address.Equals(binding.Address); }
        }

        private static void Check(bool condition, string name)
        { if (!condition) throw new Exception("FAIL " + name); checks++; Console.WriteLine("PASS " + name); }
        private static string Header(NetworkStream stream)
        {
            List<byte> bytes = new List<byte>();
            while (bytes.Count < 65536)
            {
                int value = stream.ReadByte(); if (value < 0) throw new EndOfStreamException(); bytes.Add((byte)value);
                int n = bytes.Count;
                if (n >= 4 && bytes[n-4] == 13 && bytes[n-3] == 10 && bytes[n-2] == 13 && bytes[n-1] == 10)
                    return Encoding.ASCII.GetString(bytes.ToArray());
            }
            throw new Exception("Test header overflow.");
        }
        private static string Call(int port, string request)
        {
            using (TcpClient client = new TcpClient("127.0.0.1", port))
            {
                client.ReceiveTimeout = 5000; NetworkStream stream = client.GetStream();
                byte[] bytes = Encoding.ASCII.GetBytes(request); stream.Write(bytes, 0, bytes.Length);
                using (MemoryStream result = new MemoryStream())
                {
                    byte[] buffer = new byte[4096]; int count;
                    while ((count = stream.Read(buffer, 0, buffer.Length)) > 0) result.Write(buffer, 0, count);
                    return Encoding.ASCII.GetString(result.ToArray());
                }
            }
        }
        private static string ConnectError(int port)
        { return Call(port, "CONNECT 127.0.0.1:" + echoPort + " HTTP/1.1\r\n\r\n"); }
        private static TcpClient Tunnel(int port)
        {
            TcpClient client = new TcpClient("127.0.0.1", port); client.ReceiveTimeout = 5000;
            byte[] request = Encoding.ASCII.GetBytes("CONNECT 127.0.0.1:" + echoPort + " HTTP/1.1\r\n\r\n");
            client.GetStream().Write(request, 0, request.Length);
            string response = Header(client.GetStream());
            if (!response.StartsWith("HTTP/1.1 200")) { client.Close(); throw new Exception(response); }
            return client;
        }
        private static void Echo(TcpClient client)
        {
            byte[] bytes = Encoding.ASCII.GetBytes("local-test"); NetworkStream stream = client.GetStream();
            stream.Write(bytes, 0, bytes.Length); byte[] reply = new byte[bytes.Length]; int position = 0;
            while (position < reply.Length) { int count = stream.Read(reply, position, reply.Length-position); if(count == 0) throw new EndOfStreamException(); position += count; }
            Check(Encoding.ASCII.GetString(reply) == "local-test", "loopback echo remains intact");
        }
        private static bool Closed(TcpClient client)
        {
            try { return client.GetStream().ReadByte() < 0; }
            catch (IOException error)
            {
                SocketException socketError = error.InnerException as SocketException;
                if (socketError != null && socketError.SocketErrorCode == SocketError.TimedOut) return false;
                return true;
            }
        }
        private static void Accept(IAsyncResult result)
        {
            TcpClient client;
            try { client = echo.EndAcceptTcpClient(result); Interlocked.Increment(ref accepts); echo.BeginAcceptTcpClient(Accept, null); }
            catch (Exception) { return; }
            ThreadPool.QueueUserWorkItem(delegate
            {
                using (client)
                {
                    try { NetworkStream stream = client.GetStream(); byte[] buffer = new byte[4096]; int count;
                        while ((count = stream.Read(buffer, 0, buffer.Length)) > 0) stream.Write(buffer, 0, count); }
                    catch (Exception) { }
                }
            });
        }
        private static DomainPolicy Policy()
        {
            DomainPolicy policy = new DomainPolicy("claude.com\nexample.com", "127.0.0.0/8");
            policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "vpn" }, new bool[] { false });
            return policy;
        }
        private static void Configure(BridgeServer bridge)
        { bridge.Configure(0, 1, TestGuid, "127.0.0.1"); }
        private static string Status(int port)
        { return Call(port, "GET /fastorange/vpnbindingtests123/vpn-status.json HTTP/1.1\r\n\r\n"); }

        public static void Run()
        {
            echo = new TcpListener(IPAddress.Loopback, 0); echo.Start();
            echoPort = ((IPEndPoint)echo.LocalEndpoint).Port; echo.BeginAcceptTcpClient(Accept, null);
            try
            {
                int loopbackIndex = NetworkInterface.LoopbackInterfaceIndex;
                Check(WindowsVpnGuard.BestInterfaceIs(loopbackIndex, IPAddress.Loopback), "native best-interface lookup reads loopback route without packets");
                using (Socket bound = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp))
                {
                    VpnBinding loopbackBinding = new VpnBinding(loopbackIndex, TestGuid, IPAddress.Loopback, 1);
                    WindowsVpnGuard native = new WindowsVpnGuard(); native.Bind(bound, loopbackBinding);
                    int selectedInterface = (int)bound.GetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31);
                    Check(native.IsBound(bound, loopbackBinding) && selectedInterface == loopbackIndex,
                        "production binder sets selected interface and source IPv4 on loopback socket (get=" + selectedInterface + ", expected=" + loopbackIndex + ")");
                }
                Guard guard = new Guard(); DomainPolicy policy = Policy();
                using (BridgeServer bridge = new BridgeServer(policy, "vpnbindingtests123", guard))
                {
                    int port = bridge.Start(); Configure(bridge);
                    string pac = Call(port, "GET /fastorange/vpnbindingtests123/proxy.pac HTTP/1.1\r\n\r\n");
                    Check(pac.Contains("PROXY 127.0.0.1:" + port) && !pac.Contains("; DIRECT"), "PAC has no direct fallback");
                    string status = Status(port);
                    Check(status.Contains("\"vpnBindingEnforced\":true") && status.Contains("\"pacDirectFallback\":false") && status.Contains("\"vpnReady\":true") && status.Contains("\"claudeRouteIsVpn\":true"), "local status reports live enforced binding");
                    Check(policy.IsForcedVpn("platform.claude.com") && !policy.IsDirect("platform.claude.com"), "claude.com cannot be automatically reclassified as domestic");
                    policy.SetOverrides(new string[] { "api.anthropic.com", "127.0.0.1" }, new string[] { "direct", "vpn" }, new bool[] { false, false });
                    Check(Status(port).Contains("\"claudeRouteIsVpn\":false"), "status exposes manual Claude direct override without changing it");
                    policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "vpn" }, new bool[] { false });

                    guard.Live = false; int before = accepts;
                    Check(ConnectError(port).Contains("vpn-not-ready") && accepts == before, "stale ready refuses request before upstream connect");
                    Check(Status(port).Contains("\"vpnReady\":false"), "status uses live guard rather than Configure cache");
                    guard.Live = true; guard.RouteLive = false;
                    Check(ConnectError(port).Contains("vpn-binding-unavailable") && accepts == before, "target route leaving VPN refuses before connect");
                    guard.RouteLive = true; guard.NullChecks = 0; guard.DropOnNullCheck = 2;
                    Check(ConnectError(port).Contains("vpn-binding-unavailable") && accepts == before, "binding loss between admission and resolved result refuses before connect");
                    guard.DropOnNullCheck = 0; guard.Live = true;
                    guard.RouteLive = true; guard.BindFails = true;
                    Check(ConnectError(port).Contains("vpn-bind-failed") && accepts == before, "bind failure never retries through default route");
                    guard.BindFails = false; guard.Bound = false;
                    Check(ConnectError(port).Contains("vpn-binding-unavailable"), "wrong post-connect source refuses CONNECT success");
                    guard.Bound = true;
                    using (TcpClient vpn = Tunnel(port))
                    {
                        Echo(vpn);
                        policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "direct" }, new bool[] { false });
                        using (TcpClient direct = Tunnel(port))
                        {
                            Echo(direct); guard.Live = false;
                            Check(Closed(vpn), "one-second guard closes established VPN after loss without Configure");
                            Echo(direct);
                            Check(true, "VPN loss preserves established domestic connection");
                        }
                    }
                    guard.Live = true;
                    policy.SetOverrides(new string[] { "127.0.0.1" }, new string[] { "vpn" }, new bool[] { false });
                    using (TcpClient vpn = Tunnel(port))
                    {
                        bridge.Configure(0, 1, "00000000-0000-0000-0000-000000000002", "127.0.0.1");
                        Check(Closed(vpn), "adapter identity replacement closes old VPN generation");
                    }
                    Configure(bridge);
                    using (TcpClient vpn = Tunnel(port))
                    {
                        bridge.Configure(0, 1, TestGuid, "127.0.0.2");
                        Check(Closed(vpn), "VPN IPv4 replacement closes old VPN generation");
                    }
                    Configure(bridge);
                    using (TcpClient vpn = Tunnel(port))
                    {
                        guard.RouteLive = false;
                        Check(Closed(vpn), "one-second guard closes established VPN when target route changes");
                    }
                    guard.RouteLive = true;
                    bridge.Configure(0, true);
                    Check(ConnectError(port).Contains("vpn-not-ready"), "legacy bool Configure cannot enable unbound VPN");
                    Configure(bridge); guard.TargetChecks = 0; guard.DropOnTargetCheck = 2;
                    Check(ConnectError(port).Contains("vpn-binding-unavailable"), "route changes between connect and completion refuse success");
                }
                using (BridgeServer production = new BridgeServer(Policy(), "vpnbindingtests123"))
                {
                    int port = production.Start(); production.Configure(0, 1, TestGuid, "127.0.0.1");
                    Check(ConnectError(port).Contains("vpn-not-ready"), "production guard rejects fake loopback binding");
                }
                Console.WriteLine("ALL " + checks + " VPN BINDING CHECKS PASSED");
            }
            finally { echo.Stop(); }
        }
    }
}
