// IPv4-only authenticated SOCKS5 relay. No DNS, payload logging, service or driver installation.
// Compile with .NET Framework 4 / System.Web.Extensions.dll. Test seams are internal.
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace OrangeDeskWeChatRelay
{
    internal sealed class Configuration
    {
        internal int Port;
        internal Guid InterfaceGuid;
        internal byte[] Username, Password;
        internal string StatusPath;

        internal static Configuration Load(string path)
        {
            if (!Path.IsPathRooted(path) || new FileInfo(path).Length > 16384) throw new ArgumentException();
            JavaScriptSerializer serializer = new JavaScriptSerializer { MaxJsonLength = 16384, RecursionLimit = 4 };
            Dictionary<string, object> data = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
            if (data == null || data.Count != 5) throw new ArgumentException();
            string[] names = { "listen_port", "interface_guid", "username", "password", "status_path" };
            foreach (string name in names) if (!data.ContainsKey(name)) throw new ArgumentException();
            Configuration value = new Configuration();
            if (!(data["listen_port"] is int)) throw new ArgumentException();
            value.Port = (int)data["listen_port"];
            if (value.Port < 1024 || value.Port > 65535 || !(data["interface_guid"] is string) ||
                !Guid.TryParse((string)data["interface_guid"], out value.InterfaceGuid) || value.InterfaceGuid == Guid.Empty)
                throw new ArgumentException();
            if (!(data["username"] is string) || !(data["password"] is string) || !(data["status_path"] is string))
                throw new ArgumentException();
            value.Username = new UTF8Encoding(false, true).GetBytes((string)data["username"]);
            value.Password = new UTF8Encoding(false, true).GetBytes((string)data["password"]);
            if (value.Username.Length < 1 || value.Username.Length > 255 || value.Password.Length < 1 || value.Password.Length > 255)
                throw new ArgumentException();
            string status = (string)data["status_path"];
            if (!Path.IsPathRooted(status) || !String.Equals(Path.GetExtension(status), ".json", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException();
            status = Path.GetFullPath(status);
            string programData = Path.GetFullPath(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData)).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!status.StartsWith(programData, StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(Path.GetDirectoryName(status), Path.GetDirectoryName(Path.GetFullPath(path)), StringComparison.OrdinalIgnoreCase) ||
                String.Equals(status, Path.GetFullPath(path), StringComparison.OrdinalIgnoreCase)) throw new ArgumentException();
            value.StatusPath = status;
            return value;
        }
    }

    internal sealed class Binding
    {
        internal Guid Guid;
        internal string Name;
        internal IPAddress Address;
        internal int Index;
        internal bool Same(Binding other)
        { return other != null && Guid == other.Guid && Index == other.Index && Address.Equals(other.Address); }
    }

    internal interface IBindingProvider
    {
        Binding Current();
        void Bind(Socket socket, Binding binding);
    }

    internal sealed class NativeBindingProvider : IBindingProvider
    {
        private readonly Guid guid;
        internal NativeBindingProvider(Guid value) { guid = value; }
        public Binding Current()
        {
            foreach (NetworkInterface nic in NetworkInterface.GetAllNetworkInterfaces())
            {
                Guid candidate;
                if (!Guid.TryParse(nic.Id, out candidate) || candidate != guid) continue;
                if (nic.OperationalStatus != OperationalStatus.Up || nic.NetworkInterfaceType == NetworkInterfaceType.Loopback ||
                    nic.NetworkInterfaceType == NetworkInterfaceType.Tunnel) throw new InvalidOperationException("interface-unavailable");
                IPInterfaceProperties properties = nic.GetIPProperties();
                IPv4InterfaceProperties ipv4 = properties.GetIPv4Properties();
                if (ipv4 == null || ipv4.Index <= 0) throw new InvalidOperationException("interface-unavailable");
                List<IPAddress> addresses = new List<IPAddress>();
                foreach (UnicastIPAddressInformation address in properties.UnicastAddresses)
                {
                    if (address.Address.AddressFamily != AddressFamily.InterNetwork || IPAddress.IsLoopback(address.Address)) continue;
                    byte[] bytes = address.Address.GetAddressBytes();
                    if (bytes[0] == 0 || (bytes[0] == 169 && bytes[1] == 254)) continue;
                    addresses.Add(address.Address);
                }
                if (addresses.Count == 0) throw new InvalidOperationException("interface-unavailable");
                addresses.Sort(delegate(IPAddress a, IPAddress b) { return String.CompareOrdinal(a.ToString(), b.ToString()); });
                return new Binding { Guid = guid, Name = nic.Name, Address = addresses[0], Index = ipv4.Index };
            }
            throw new InvalidOperationException("interface-unavailable");
        }
        public void Bind(Socket socket, Binding binding)
        {
            if (socket.AddressFamily != AddressFamily.InterNetwork || !binding.Same(Current()))
                throw new InvalidOperationException("interface-changed");
            // Windows IPv4 IP_UNICAST_IF takes an interface index in network byte order.
            socket.SetSocketOption(SocketOptionLevel.IP, (SocketOptionName)31, IPAddress.HostToNetworkOrder(binding.Index));
            socket.Bind(new IPEndPoint(binding.Address, 0));
            if (!binding.Same(Current())) throw new InvalidOperationException("interface-changed");
        }
    }

    internal sealed class Lease : IDisposable
    {
        internal readonly Socket Socket;
        internal readonly Binding Binding;
        private readonly Action closeOwner;
        private int closed;
        internal Lease(Socket socket, Binding binding, Action close) { Socket = socket; Binding = binding; closeOwner = close; }
        public void Dispose()
        {
            if (Interlocked.Exchange(ref closed, 1) != 0) return;
            try { Socket.Close(); } catch (SocketException) { }
            try { closeOwner(); } catch (SocketException) { }
        }
    }

    internal sealed class DestinationWindow
    {
        private readonly Dictionary<string, DateTime> entries = new Dictionary<string, DateTime>();
        private void Expire(DateTime now)
        {
            List<string> expired = new List<string>();
            foreach (KeyValuePair<string, DateTime> entry in entries)
                if ((now - entry.Value).TotalSeconds >= 60) expired.Add(entry.Key);
            foreach (string key in expired) entries.Remove(key);
        }
        internal bool CanSend(IPEndPoint endpoint, DateTime now)
        { Expire(now); return entries.Count < 256 || entries.ContainsKey(endpoint.ToString()); }
        internal void Sent(IPEndPoint endpoint, DateTime now)
        { Expire(now); if (entries.Count < 256 || entries.ContainsKey(endpoint.ToString())) entries[endpoint.ToString()] = now; }
        internal bool Allows(IPEndPoint endpoint, DateTime now)
        { Expire(now); return entries.ContainsKey(endpoint.ToString()); }
    }

    internal sealed class Relay : IDisposable
    {
        private readonly Configuration config;
        private readonly IBindingProvider provider;
        private readonly Action<Dictionary<string, object>> statusWriter;
        private readonly bool allowLoopbackTargets;
        private readonly ConcurrentDictionary<int, TcpClient> clients = new ConcurrentDictionary<int, TcpClient>();
        private readonly ConcurrentDictionary<int, Lease> leases = new ConcurrentDictionary<int, Lease>();
        private readonly ManualResetEvent stopped = new ManualResetEvent(false);
        private TcpListener listener;
        private Timer heartbeat;
        private volatile bool stopping;
        private int serial, connectionCount, associationCount, writing, boundPort;
        internal const int MaxConnections = 128, MaxAssociations = 64;
        internal int BoundPort { get { return boundPort; } }
        internal int ConnectionCount { get { return Interlocked.CompareExchange(ref connectionCount, 0, 0); } }
        internal int AssociationCount { get { return Interlocked.CompareExchange(ref associationCount, 0, 0); } }

        internal Relay(Configuration value, IBindingProvider bindingProvider,
                       Action<Dictionary<string, object>> writer, bool testLoopbackTargets)
        { config = value; provider = bindingProvider; statusWriter = writer; allowLoopbackTargets = testLoopbackTargets; }

        internal void Start()
        {
            listener = new TcpListener(IPAddress.Loopback, config.Port);
            listener.Server.ExclusiveAddressUse = true;
            listener.Start(64);
            boundPort = ((IPEndPoint)listener.LocalEndpoint).Port;
            heartbeat = new Timer(delegate(object unused) { Tick(); }, null, 0, 1000);
            Thread accept = new Thread(AcceptLoop); accept.IsBackground = true; accept.Name = "WeChatRelayAccept"; accept.Start();
        }
        internal void Wait() { stopped.WaitOne(); }
        internal void Stop()
        {
            if (stopping) return;
            stopping = true;
            if (heartbeat != null) heartbeat.Dispose();
            if (listener != null) listener.Stop();
            foreach (Lease lease in leases.Values) lease.Dispose();
            foreach (TcpClient client in clients.Values) client.Close();
            Tick();
            stopped.Set();
        }
        public void Dispose() { Stop(); }

        private void AcceptLoop()
        {
            while (!stopping)
            {
                TcpClient client;
                try { client = listener.AcceptTcpClient(); }
                catch (SocketException) { if (stopping) break; Thread.Sleep(50); continue; }
                catch (ObjectDisposedException) { break; }
                if (stopping) { client.Close(); break; }
                if (Interlocked.Increment(ref connectionCount) > MaxConnections)
                { Interlocked.Decrement(ref connectionCount); client.Close(); continue; }
                int id = Interlocked.Increment(ref serial);
                clients[id] = client;
                try { Background(delegate { Serve(id, client); }); }
                catch (Exception)
                { TcpClient unused; clients.TryRemove(id, out unused); client.Close(); Interlocked.Decrement(ref connectionCount); }
            }
        }
        private static void Background(ThreadStart action)
        {
            // Dedicated workers are bounded by the admitted sessions. Blocking
            // socket pumps cannot starve authentication or the heartbeat pool.
            Thread worker = new Thread(action); worker.IsBackground = true; worker.Start();
        }
        private void Tick()
        {
            if (Interlocked.Exchange(ref writing, 1) != 0) return;
            try
            {
                Binding current = null; string error = "";
                try { current = provider.Current(); } catch (Exception) { error = "interface-unavailable"; }
                foreach (Lease lease in leases.Values)
                    if (!lease.Binding.Same(current)) lease.Dispose();
                Dictionary<string, object> status = new Dictionary<string, object>();
                status["state"] = stopping ? "stopped" : current == null ? "error" : "running";
                status["interface_guid"] = config.InterfaceGuid.ToString("D");
                status["interface_name"] = current == null ? "" : current.Name;
                status["physical_ipv4"] = current == null ? "" : current.Address.ToString();
                status["listen_port"] = BoundPort;
                status["pid"] = System.Diagnostics.Process.GetCurrentProcess().Id;
                status["updated_at"] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                status["active_connections"] = ConnectionCount;
                status["udp_associations"] = AssociationCount;
                status["error"] = error;
                status["supports_ipv6"] = false;
                statusWriter(status);
            }
            catch (Exception) { /* Never serialize native exception strings or config secrets. */ }
            finally { Interlocked.Exchange(ref writing, 0); }
        }
        internal void CheckBindingsForTest() { Tick(); }
        private bool Current(Binding binding)
        { try { return !stopping && binding.Same(provider.Current()); } catch (Exception) { return false; } }
        private Lease CreateExternal(SocketType type, ProtocolType protocol, TcpClient owner)
        {
            Binding binding = provider.Current();
            Socket socket = new Socket(AddressFamily.InterNetwork, type, protocol);
            try
            {
                provider.Bind(socket, binding);
                if (!Current(binding)) throw new IOException("interface-changed");
                socket.ReceiveTimeout = 1000; socket.SendTimeout = 5000;
                return new Lease(socket, binding, owner.Close);
            }
            catch { socket.Close(); throw; }
        }
        private static byte[] Read(NetworkStream stream, int count)
        {
            byte[] bytes = new byte[count]; int read = 0;
            while (read < count)
            { int n = stream.Read(bytes, read, count - read); if (n == 0) throw new EndOfStreamException(); read += n; }
            return bytes;
        }
        private static bool EqualSecret(byte[] a, byte[] b)
        {
            int difference = a.Length ^ b.Length;
            for (int i = 0; i < Math.Max(a.Length, b.Length); i++)
                difference |= (i < a.Length ? a[i] : 0) ^ (i < b.Length ? b[i] : 0);
            return difference == 0;
        }
        private bool Authenticate(NetworkStream stream)
        {
            byte[] hello = Read(stream, 2);
            if (hello[0] != 5 || hello[1] < 1 || hello[1] > 16) return false;
            byte[] methods = Read(stream, hello[1]); bool supported = false;
            foreach (byte method in methods) if (method == 2) supported = true;
            stream.Write(new byte[] { 5, supported ? (byte)2 : (byte)255 }, 0, 2);
            if (!supported) return false;
            byte[] auth = Read(stream, 2);
            if (auth[0] != 1 || auth[1] == 0) return false;
            byte[] username = Read(stream, auth[1]); byte passwordLength = Read(stream, 1)[0];
            if (passwordLength == 0) return false;
            byte[] password = Read(stream, passwordLength);
            bool ok = EqualSecret(username, config.Username) & EqualSecret(password, config.Password);
            Array.Clear(username, 0, username.Length); Array.Clear(password, 0, password.Length);
            stream.Write(new byte[] { 1, ok ? (byte)0 : (byte)1 }, 0, 2);
            return ok;
        }
        private static void Reply(NetworkStream stream, byte result, IPEndPoint endpoint)
        {
            if (endpoint == null) endpoint = new IPEndPoint(IPAddress.Any, 0);
            byte[] bytes = new byte[10]; bytes[0] = 5; bytes[1] = result; bytes[3] = 1;
            Buffer.BlockCopy(endpoint.Address.GetAddressBytes(), 0, bytes, 4, 4);
            bytes[8] = (byte)(endpoint.Port >> 8); bytes[9] = (byte)endpoint.Port;
            stream.Write(bytes, 0, bytes.Length);
        }
        private bool AllowedDestination(IPAddress ip, int port)
        {
            if (ip.AddressFamily != AddressFamily.InterNetwork || port < 1 || port > 65535) return false;
            byte[] bytes = ip.GetAddressBytes();
            return bytes[0] != 0 && bytes[0] < 224 && !ip.Equals(IPAddress.Broadcast) &&
                (allowLoopbackTargets || !IPAddress.IsLoopback(ip));
        }
        private void Serve(int id, TcpClient client)
        {
            Lease lease = null;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = 5000; client.SendTimeout = 5000;
                if (!IPAddress.IsLoopback(((IPEndPoint)client.Client.RemoteEndPoint).Address)) return;
                NetworkStream stream = client.GetStream();
                if (!Authenticate(stream)) return;
                byte[] header = Read(stream, 4);
                if (header[0] != 5 || header[2] != 0) return;
                // Domain names and IPv6 are deliberately unsupported, never sent to system DNS.
                if (header[3] != 1) { Reply(stream, 8, null); return; }
                byte[] address = Read(stream, 6);
                IPAddress target = new IPAddress(new byte[] { address[0], address[1], address[2], address[3] });
                int port = address[4] * 256 + address[5];
                if (header[1] == 3)
                {
                    if (!target.Equals(IPAddress.Any) && !target.Equals(((IPEndPoint)client.Client.RemoteEndPoint).Address))
                    { Reply(stream, 2, null); return; }
                    Associate(id, client, stream, port); return;
                }
                if (header[1] != 1) { Reply(stream, 7, null); return; }
                if (!AllowedDestination(target, port)) { Reply(stream, 2, null); return; }
                try
                {
                    lease = CreateExternal(SocketType.Stream, ProtocolType.Tcp, client);
                    leases[id] = lease;
                    IAsyncResult connecting = lease.Socket.BeginConnect(new IPEndPoint(target, port), null, null);
                    using (WaitHandle wait = connecting.AsyncWaitHandle)
                    { if (!wait.WaitOne(5000)) throw new SocketException((int)SocketError.TimedOut); }
                    lease.Socket.EndConnect(connecting);
                    if (!Current(lease.Binding)) throw new IOException("interface-changed");
                }
                catch (Exception) { Reply(stream, 3, null); return; }
                Reply(stream, 0, (IPEndPoint)lease.Socket.LocalEndPoint);
                client.ReceiveTimeout = 1000;
                Lease connected = lease;
                Background(delegate { Pump(connected.Socket, client.Client, connected); });
                Pump(client.Client, connected.Socket, connected);
            }
            catch (Exception) { /* Protocol and socket failures close only this bounded session. */ }
            finally
            {
                Lease removed; leases.TryRemove(id, out removed);
                if (lease != null) lease.Dispose();
                client.Close(); TcpClient unused; clients.TryRemove(id, out unused);
                Interlocked.Decrement(ref connectionCount);
            }
        }
        private void Pump(Socket source, Socket target, Lease lease)
        {
            byte[] bytes = new byte[16384];
            try
            {
                while (Current(lease.Binding))
                {
                    int count;
                    try { count = source.Receive(bytes); }
                    catch (SocketException ex) { if (ex.SocketErrorCode == SocketError.TimedOut) continue; throw; }
                    if (count == 0 || !Current(lease.Binding)) break;
                    int sent = 0;
                    while (sent < count)
                    { int n = target.Send(bytes, sent, count - sent, SocketFlags.None); if (n == 0) throw new EndOfStreamException(); sent += n; }
                }
            }
            catch (Exception) { }
            finally { Array.Clear(bytes, 0, bytes.Length); lease.Dispose(); }
        }
        private void Associate(int id, TcpClient client, NetworkStream stream, int clientPort)
        {
            if (Interlocked.Increment(ref associationCount) > MaxAssociations)
            { Interlocked.Decrement(ref associationCount); Reply(stream, 1, null); return; }
            Socket local = null; Lease external = null;
            try
            {
                local = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
                local.Bind(new IPEndPoint(IPAddress.Loopback, 0)); local.ReceiveTimeout = 1000; local.SendTimeout = 1000;
                try { external = CreateExternal(SocketType.Dgram, ProtocolType.Udp, client); }
                catch (Exception) { Reply(stream, 3, null); return; }
                leases[id] = external;
                UdpAssociation association = new UdpAssociation(this, local, external,
                    ((IPEndPoint)client.Client.RemoteEndPoint).Address, clientPort);
                Reply(stream, 0, (IPEndPoint)local.LocalEndPoint);
                Background(association.FromClient);
                Background(association.FromRemote);
                client.ReceiveTimeout = 1000;
                byte[] one = new byte[1];
                while (Current(external.Binding))
                {
                    try { if (stream.Read(one, 0, 1) == 0) break; }
                    catch (IOException ex)
                    {
                        SocketException inner = ex.InnerException as SocketException;
                        if (inner != null && inner.SocketErrorCode == SocketError.TimedOut) continue;
                        throw;
                    }
                }
            }
            finally
            {
                Lease removed; leases.TryRemove(id, out removed);
                if (local != null) local.Close(); if (external != null) external.Dispose();
                Interlocked.Decrement(ref associationCount);
            }
        }

        private sealed class UdpAssociation
        {
            private readonly Relay relay;
            private readonly Socket local;
            private readonly Lease external;
            private readonly IPAddress clientAddress;
            private readonly int requestedPort;
            private readonly object gate = new object();
            private IPEndPoint peer;
            private readonly DestinationWindow destinations = new DestinationWindow();
            internal UdpAssociation(Relay owner, Socket localSocket, Lease bound, IPAddress clientIp, int clientPort)
            { relay = owner; local = localSocket; external = bound; clientAddress = clientIp; requestedPort = clientPort; }
            internal void FromClient()
            {
                byte[] bytes = new byte[65535];
                try
                {
                    while (relay.Current(external.Binding))
                    {
                        EndPoint sender = new IPEndPoint(IPAddress.Any, 0); int count;
                        try { count = local.ReceiveFrom(bytes, ref sender); }
                        catch (SocketException ex) { if (ex.SocketErrorCode == SocketError.TimedOut) continue; throw; }
                        IPEndPoint source = (IPEndPoint)sender;
                        if (!source.Address.Equals(clientAddress) || !IPAddress.IsLoopback(source.Address) ||
                            (requestedPort != 0 && source.Port != requestedPort)) continue;
                        if (count < 10 || count > 65507 || bytes[0] != 0 || bytes[1] != 0 || bytes[2] != 0 || bytes[3] != 1) continue;
                        IPAddress address = new IPAddress(new byte[] { bytes[4], bytes[5], bytes[6], bytes[7] });
                        int port = bytes[8] * 256 + bytes[9];
                        if (!relay.AllowedDestination(address, port)) continue;
                        IPEndPoint destination = new IPEndPoint(address, port);
                        lock (gate)
                        {
                            if (peer == null) peer = source;
                            if (!peer.Equals(source)) continue;
                            if (!destinations.CanSend(destination, DateTime.UtcNow)) continue;
                        }
                        if (!relay.Current(external.Binding)) break;
                        // Hold the same gate as the receive permission check,
                        // avoiding a fast reply racing the successful SendTo.
                        lock (gate)
                        {
                            int sent = external.Socket.SendTo(bytes, 10, count - 10, SocketFlags.None, destination);
                            if (sent == count - 10) destinations.Sent(destination, DateTime.UtcNow);
                        }
                    }
                }
                catch (Exception) { }
                finally { Array.Clear(bytes, 0, bytes.Length); external.Dispose(); try { local.Close(); } catch (SocketException) { } }
            }
            internal void FromRemote()
            {
                byte[] bytes = new byte[65535];
                try
                {
                    while (relay.Current(external.Binding))
                    {
                        EndPoint sender = new IPEndPoint(IPAddress.Any, 0); int count;
                        try { count = external.Socket.ReceiveFrom(bytes, ref sender); }
                        catch (SocketException ex) { if (ex.SocketErrorCode == SocketError.TimedOut) continue; throw; }
                        IPEndPoint remote = (IPEndPoint)sender; IPEndPoint receiver;
                        lock (gate)
                        {
                            if (peer == null || !destinations.Allows(remote, DateTime.UtcNow)) continue;
                            receiver = peer;
                        }
                        if (count > 65497 || !relay.Current(external.Binding)) continue;
                        byte[] packet = new byte[count + 10]; packet[3] = 1;
                        Buffer.BlockCopy(remote.Address.GetAddressBytes(), 0, packet, 4, 4);
                        packet[8] = (byte)(remote.Port >> 8); packet[9] = (byte)remote.Port;
                        Buffer.BlockCopy(bytes, 0, packet, 10, count);
                        local.SendTo(packet, receiver);
                        Array.Clear(packet, 0, packet.Length);
                    }
                }
                catch (Exception) { }
                finally { Array.Clear(bytes, 0, bytes.Length); external.Dispose(); try { local.Close(); } catch (SocketException) { } }
            }
        }
    }

    public static class Program
    {
        private static void WriteStatus(string path, Dictionary<string, object> status)
        {
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, new JavaScriptSerializer().Serialize(status), new UTF8Encoding(false));
                if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
        public static int Main(string[] args)
        {
            if (args == null || args.Length != 1) { Console.Error.WriteLine("relay-config-invalid"); return 2; }
            Configuration config;
            try { config = Configuration.Load(args[0]); }
            catch (Exception) { Console.Error.WriteLine("relay-config-invalid"); return 2; }
            try
            {
                using (Relay relay = new Relay(config, new NativeBindingProvider(config.InterfaceGuid),
                    delegate(Dictionary<string, object> status) { WriteStatus(config.StatusPath, status); }, false))
                {
                    Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; relay.Stop(); };
                    relay.Start(); relay.Wait();
                }
                return 0;
            }
            catch (Exception) { Console.Error.WriteLine("relay-start-failed"); return 1; }
            finally { Array.Clear(config.Username, 0, config.Username.Length); Array.Clear(config.Password, 0, config.Password.Length); }
        }
    }
}
