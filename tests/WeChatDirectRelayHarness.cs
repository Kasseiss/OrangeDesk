namespace OrangeDeskWeChatRelay
{
    internal sealed class FakeBinding : IBindingProvider
    {
        internal bool Ready = true, FailBind, DropAfterBind, DropAfterConnect;
        internal int Binds;
        internal System.Net.Sockets.Socket LastSocket;
        internal Binding Value = new Binding { Guid = new System.Guid("12345678-1234-1234-1234-123456789abc"),
            Name = "mock physical", Address = System.Net.IPAddress.Loopback, Index = 7 };
        public Binding Current()
        {
            if (!Ready || (DropAfterConnect && LastSocket != null && LastSocket.Connected))
                throw new System.InvalidOperationException("mock unavailable");
            return Value;
        }
        public void Bind(System.Net.Sockets.Socket socket, Binding value)
        {
            System.Threading.Interlocked.Increment(ref Binds); LastSocket = socket;
            if (FailBind) throw new System.Net.Sockets.SocketException((int)System.Net.Sockets.SocketError.AccessDenied);
            socket.Bind(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 0));
            if (DropAfterBind) Ready = false;
        }
    }

    public static class RelayHarness
    {
        private static int count;
        private static void Check(bool valid, string name)
        { if (!valid) throw new System.InvalidOperationException("relay-test-failed: " + name); count++; }
        private static Configuration Config()
        { return new Configuration { Port = 0, InterfaceGuid = new System.Guid("12345678-1234-1234-1234-123456789abc"),
            Username = System.Text.Encoding.UTF8.GetBytes("mock-user"), Password = System.Text.Encoding.UTF8.GetBytes("mock-password") }; }
        private static Relay Start(FakeBinding binding)
        { Relay relay = new Relay(Config(), binding, delegate(System.Collections.Generic.Dictionary<string, object> status) { }, true); relay.Start(); return relay; }
        private static System.Net.Sockets.TcpClient Client(Relay relay)
        {
            System.Net.Sockets.TcpClient client = new System.Net.Sockets.TcpClient();
            client.ReceiveTimeout = 3000; client.SendTimeout = 3000; client.Connect(System.Net.IPAddress.Loopback, relay.BoundPort); return client;
        }
        private static byte[] Read(System.Net.Sockets.NetworkStream stream, int length)
        {
            byte[] bytes = new byte[length]; int offset = 0;
            while (offset < length) { int n = stream.Read(bytes, offset, length - offset); if (n == 0) throw new System.IO.EndOfStreamException(); offset += n; }
            return bytes;
        }
        private static void Write(System.Net.Sockets.NetworkStream stream, byte[] data) { stream.Write(data, 0, data.Length); }
        private static void Authenticate(System.Net.Sockets.TcpClient client, bool valid)
        {
            System.Net.Sockets.NetworkStream stream = client.GetStream();
            Write(stream, new byte[] { 5, 2, 0, 2 });
            byte[] selected = Read(stream, 2); Check(selected[0] == 5 && selected[1] == 2, "auth-required");
            byte[] user = Config().Username, password = Config().Password;
            if (!valid) password[0] ^= 1;
            byte[] packet = new byte[3 + user.Length + password.Length]; packet[0] = 1; packet[1] = (byte)user.Length;
            System.Buffer.BlockCopy(user, 0, packet, 2, user.Length); packet[2 + user.Length] = (byte)password.Length;
            System.Buffer.BlockCopy(password, 0, packet, 3 + user.Length, password.Length); Write(stream, packet);
            byte[] result = Read(stream, 2); Check(result[0] == 1 && result[1] == (valid ? 0 : 1), valid ? "auth-success" : "auth-rejected");
        }
        private static byte[] Request(System.Net.Sockets.TcpClient client, byte command, System.Net.IPAddress ip, int port)
        {
            byte[] packet = new byte[10]; packet[0] = 5; packet[1] = command; packet[3] = 1;
            System.Buffer.BlockCopy(ip.GetAddressBytes(), 0, packet, 4, 4); packet[8] = (byte)(port >> 8); packet[9] = (byte)port;
            Write(client.GetStream(), packet); return Read(client.GetStream(), 10);
        }
        private static bool Closed(System.Net.Sockets.TcpClient client)
        { try { return client.GetStream().ReadByte() == -1; } catch (System.IO.IOException) { return true; } }
        private static System.Net.Sockets.Socket Udp()
        {
            System.Net.Sockets.Socket socket = new System.Net.Sockets.Socket(System.Net.Sockets.AddressFamily.InterNetwork,
                System.Net.Sockets.SocketType.Dgram, System.Net.Sockets.ProtocolType.Udp);
            socket.Bind(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 0)); socket.ReceiveTimeout = 400; return socket;
        }
        private static byte[] UdpPacket(System.Net.IPEndPoint remote, byte fragment)
        {
            byte[] packet = new byte[13]; packet[2] = fragment; packet[3] = 1;
            System.Buffer.BlockCopy(remote.Address.GetAddressBytes(), 0, packet, 4, 4);
            packet[8] = (byte)(remote.Port >> 8); packet[9] = (byte)remote.Port; packet[10] = 17; packet[11] = 18; packet[12] = 19; return packet;
        }
        private static bool Timeout(System.Net.Sockets.Socket socket)
        {
            try { byte[] bytes = new byte[64]; socket.Receive(bytes); return false; }
            catch (System.Net.Sockets.SocketException ex) { return ex.SocketErrorCode == System.Net.Sockets.SocketError.TimedOut; }
        }
        private static void Protocol()
        {
            FakeBinding binding = new FakeBinding();
            using (Relay relay = Start(binding))
            {
                using (System.Net.Sockets.TcpClient client = Client(relay))
                { Write(client.GetStream(), new byte[] { 5, 1, 0 }); Check(Read(client.GetStream(), 2)[1] == 255, "anonymous-rejected"); }
                using (System.Net.Sockets.TcpClient client = Client(relay)) { Authenticate(client, false); Check(Closed(client), "bad-auth-closed"); }
                foreach (byte atyp in new byte[] { 3, 4 })
                    using (System.Net.Sockets.TcpClient client = Client(relay))
                    { Authenticate(client, true); Write(client.GetStream(), new byte[] { 5, 1, 0, atyp }); Check(Read(client.GetStream(), 10)[1] == 8, "unsupported-address-no-DNS"); }
                using (System.Net.Sockets.TcpClient client = Client(relay))
                { Authenticate(client, true); Check(Request(client, 2, System.Net.IPAddress.Loopback, 80)[1] == 7, "bind-command-rejected"); }
                using (System.Net.Sockets.TcpClient client = Client(relay))
                { Authenticate(client, true); Check(Request(client, 1, System.Net.IPAddress.Loopback, 0)[1] == 2, "zero-destination-port-rejected"); }
                Check(binding.Binds == 0, "rejected-protocols-never-create-external-socket");
                bool occupied = false;
                Relay second = new Relay(new Configuration { Port = relay.BoundPort, InterfaceGuid = Config().InterfaceGuid }, binding,
                    delegate(System.Collections.Generic.Dictionary<string, object> status) { }, true);
                try { second.Start(); } catch (System.Net.Sockets.SocketException) { occupied = true; } finally { second.Dispose(); }
                Check(occupied, "exclusive-loopback-listener");
            }
            using (Relay productionTargets = new Relay(Config(), binding,
                delegate(System.Collections.Generic.Dictionary<string, object> status) { }, false))
            {
                productionTargets.Start();
                using (System.Net.Sockets.TcpClient client = Client(productionTargets))
                { Authenticate(client, true); Check(Request(client, 1, System.Net.IPAddress.Loopback, 80)[1] == 2, "production-loopback-target-rejected"); }
            }
        }
        private static void Tcp()
        {
            FakeBinding binding = new FakeBinding();
            System.Net.Sockets.TcpListener target = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0); target.Start();
            try
            {
                using (Relay relay = Start(binding))
                using (System.Net.Sockets.TcpClient client = Client(relay))
                {
                    Authenticate(client, true);
                    Check(Request(client, 1, System.Net.IPAddress.Loopback, ((System.Net.IPEndPoint)target.LocalEndpoint).Port)[1] == 0, "tcp-connect");
                    using (System.Net.Sockets.TcpClient remote = target.AcceptTcpClient())
                    {
                        remote.ReceiveTimeout = 3000; remote.SendTimeout = 3000;
                        Write(client.GetStream(), new byte[] { 21, 22, 23 }); Check(Read(remote.GetStream(), 3)[2] == 23, "tcp-forward");
                        Write(remote.GetStream(), new byte[] { 31, 32 }); Check(Read(client.GetStream(), 2)[0] == 31, "tcp-return");
                        Check(binding.Binds == 1, "tcp-uses-bound-socket");
                        binding.Ready = false; relay.CheckBindingsForTest(); Check(Closed(client), "nic-down-closes-existing-tcp");
                    }
                }
            }
            finally { target.Stop(); }
            foreach (int mode in new int[] { 0, 1, 2 })
            {
                FakeBinding failed = new FakeBinding { FailBind = mode == 0, DropAfterBind = mode == 1, DropAfterConnect = mode == 2 };
                System.Net.Sockets.TcpListener remote = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0); remote.Start();
                try
                {
                    using (Relay relay = Start(failed))
                    using (System.Net.Sockets.TcpClient client = Client(relay))
                    {
                        Authenticate(client, true);
                        Check(Request(client, 1, System.Net.IPAddress.Loopback, ((System.Net.IPEndPoint)remote.LocalEndpoint).Port)[1] == 3,
                            mode == 0 ? "bind-failure-no-fallback" : mode == 1 ? "nic-change-before-connect" : "nic-change-after-connect");
                        if (mode < 2) Check(!remote.Pending(), "failed-binding-never-connected");
                    }
                }
                finally { remote.Stop(); }
            }
        }
        private static void UdpProtocol()
        {
            FakeBinding binding = new FakeBinding();
            using (Relay relay = Start(binding))
            using (System.Net.Sockets.TcpClient client = Client(relay))
            using (System.Net.Sockets.Socket local = Udp())
            using (System.Net.Sockets.Socket otherLocal = Udp())
            using (System.Net.Sockets.Socket remote = Udp())
            using (System.Net.Sockets.Socket rogueRemote = Udp())
            {
                Authenticate(client, true); byte[] response = Request(client, 3, System.Net.IPAddress.Any, 0);
                Check(response[1] == 0, "udp-associate");
                System.Net.IPEndPoint relayAddress = new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, response[8] * 256 + response[9]);
                System.Net.IPEndPoint destination = (System.Net.IPEndPoint)remote.LocalEndPoint;
                local.SendTo(UdpPacket(destination, 1), relayAddress); Check(Timeout(remote), "udp-fragment-rejected");
                byte[] domain = UdpPacket(destination, 0); domain[3] = 3; local.SendTo(domain, relayAddress); Check(Timeout(remote), "udp-domain-rejected");
                local.SendTo(UdpPacket(destination, 0), relayAddress);
                byte[] bytes = new byte[64]; System.Net.EndPoint sender = new System.Net.IPEndPoint(System.Net.IPAddress.Any, 0);
                int n = remote.ReceiveFrom(bytes, ref sender); Check(n == 3 && bytes[2] == 19, "udp-payload-forwarded");
                remote.SendTo(new byte[] { 41, 42 }, sender);
                n = local.Receive(bytes); Check(n == 12 && bytes[0] == 0 && bytes[2] == 0 && bytes[3] == 1 && bytes[10] == 41 &&
                    bytes[8] * 256 + bytes[9] == destination.Port, "udp-legal-return-header");
                otherLocal.SendTo(UdpPacket(destination, 0), relayAddress); Check(Timeout(remote), "udp-first-source-port-locked");
                rogueRemote.SendTo(new byte[] { 51 }, sender); Check(Timeout(local), "udp-unsolicited-remote-rejected");
                Check(binding.Binds == 1, "udp-bound-once-no-default-socket");
                binding.Ready = false; relay.CheckBindingsForTest(); Check(Closed(client), "nic-down-closes-udp-control");
            }
        }
        private static void LimitsAndPrivacy()
        {
            DestinationWindow window = new DestinationWindow(); System.DateTime now = System.DateTime.UtcNow;
            for (int i = 1; i <= 256; i++) window.Sent(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, i), now);
            Check(!window.CanSend(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 257), now), "udp-destination-limit-256");
            Check(window.CanSend(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 1), now), "known-destination-refresh-allowed");
            Check(!window.Allows(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 1), now.AddSeconds(60)), "udp-permission-expires-60s");
            Check(window.CanSend(new System.Net.IPEndPoint(System.Net.IPAddress.Loopback, 257), now.AddSeconds(60)), "expired-destination-capacity-released");
            FakeBinding binding = new FakeBinding();
            using (Relay relay = Start(binding))
            {
                System.Reflection.FieldInfo countField = typeof(Relay).GetField("connectionCount", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                countField.SetValue(relay, 128);
                using (System.Net.Sockets.TcpClient overflow = Client(relay)) Check(Closed(overflow), "tcp-limit-128");
                countField.SetValue(relay, 0);
                System.Reflection.FieldInfo assocField = typeof(Relay).GetField("associationCount", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                assocField.SetValue(relay, 64);
                using (System.Net.Sockets.TcpClient overflow = Client(relay))
                { Authenticate(overflow, true); Check(Request(overflow, 3, System.Net.IPAddress.Any, 0)[1] == 1, "udp-association-limit-64"); }
                assocField.SetValue(relay, 0);
            }
            System.Collections.Generic.Dictionary<string, object> last = null;
            using (Relay relay = new Relay(Config(), binding, delegate(System.Collections.Generic.Dictionary<string, object> status) { last = status; }, true))
            {
                relay.Start(); relay.CheckBindingsForTest();
                for (int i = 0; last == null && i < 100; i++) System.Threading.Thread.Sleep(10);
                Check(last != null && (string)last["state"] == "running" && (bool)last["supports_ipv6"] == false, "heartbeat-schema");
                string json = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(last);
                Check(!json.Contains("mock-user") && !json.Contains("mock-password"), "heartbeat-no-credentials");
                binding.Ready = false; relay.CheckBindingsForTest();
                Check((string)last["state"] == "error" && (string)last["error"] == "interface-unavailable", "generic-interface-error");
            }
        }
        public static string Run()
        { count = 0; Protocol(); Tcp(); UdpProtocol(); LimitsAndPrivacy(); return "PASS " + count + " relay checks; loopback and mock binding only."; }
    }
}
