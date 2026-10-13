using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using FastOrangeCombined;

// All transport/caching tests are socket-free. The certificate rejection test
// uses an ephemeral, untrusted certificate and a loopback-only TLS listener.
class DoHRegressionHarness
{
    static readonly BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;
    static readonly Type ResolverType = typeof(BridgeServer).Assembly.GetType("FastOrangeCombined.BridgeServer+DirectDns");
    static int checks;
    static void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; Console.WriteLine("PASS " + name); }
    static object Invoke(object target, string method, params object[] args) { return target.GetType().GetMethod(method, Flags).Invoke(target, args); }
    static object Field(object target, string name) { return target.GetType().GetField(name, Flags).GetValue(target); }
    static void Resolve(object dns, string host, int index, Action<IPAddress[], string> complete) { Invoke(dns, "Resolve", host, index, complete); }
    sealed class Probe { internal Action<IPAddress[], uint, string> Complete; internal string Server; internal int Timeout, Cancelled; }
    sealed class Mock
    {
        internal readonly List<Probe> Probes = new List<Probe>();
        internal object Resolver;
        internal Mock()
        {
            Func<string, int, string, int, Action<IPAddress[], uint, string>, Action> start = delegate(string host, int index, string server, int timeout, Action<IPAddress[], uint, string> complete)
            {
                Probe probe = new Probe { Complete = complete, Server = server, Timeout = timeout };
                lock (Probes) Probes.Add(probe);
                return delegate { Interlocked.Increment(ref probe.Cancelled); };
            };
            Resolver = Activator.CreateInstance(ResolverType, Flags, null, new object[] { start }, null);
        }
        internal Probe At(int index) { lock (Probes) return Probes[index]; }
        internal int Count { get { lock (Probes) return Probes.Count; } }
    }
    static IPAddress[] Address(string value) { return new IPAddress[] { IPAddress.Parse(value) }; }
    static void CacheAndFlights()
    {
        Mock mock = new Mock(); int callbacks = 0; string cacheSource = null;
        Task[] tasks = new Task[64];
        for (int i = 0; i < tasks.Length; i++) tasks[i] = Task.Factory.StartNew(delegate
        { Resolve(mock.Resolver, "www.example.com", 5, delegate(IPAddress[] addresses, string source) { if (addresses != null) Interlocked.Increment(ref callbacks); }); });
        Task.WaitAll(tasks);
        Check(mock.Count == 1, "64 same-domain requests share one DoH flight");
        mock.At(0).Complete(Address("42.56.67.170"), 900, "mock");
        Check(callbacks == 64, "single flight delivers every waiter once");
        Resolve(mock.Resolver, "www.example.com", 5, delegate(IPAddress[] addresses, string source) { cacheSource = source; });
        Check(mock.Count == 1 && cacheSource == "physicalDoH/cache", "warm DNS cache creates no transport");
        IDictionary cache = (IDictionary)Field(mock.Resolver, "cache"); object entry = cache["5/www.example.com"];
        DateTime expires = (DateTime)Field(entry, "Expires");
        Check((expires - DateTime.UtcNow).TotalSeconds > 299 && (expires - DateTime.UtcNow).TotalSeconds <= 300, "positive DNS cache TTL capped at 300 seconds");
        entry.GetType().GetField("Expires", Flags).SetValue(entry, DateTime.UtcNow.AddSeconds(-1));
        Resolve(mock.Resolver, "www.example.com", 5, delegate { });
        Check(mock.Count == 2, "expired cache starts a fresh DoH request");
        mock.At(1).Complete(Address("42.56.67.171"), 0, "mock");
        Resolve(mock.Resolver, "www.example.com", 5, delegate { });
        Check(mock.Count == 3, "zero TTL is never cached");
        Invoke(mock.Resolver, "Clear");

        mock = new Mock(); int oldReplies = 0, newReplies = 0;
        Resolve(mock.Resolver, "old.example.com", 5, delegate { oldReplies++; });
        Probe old = mock.At(0); Invoke(mock.Resolver, "ChangeInterface");
        Resolve(mock.Resolver, "new.example.com", 6, delegate { newReplies++; });
        old.Complete(Address("1.2.3.4"), 60, "late");
        Check(old.Cancelled == 1 && oldReplies == 0 && ((IDictionary)Field(mock.Resolver, "cache")).Count == 0, "interface switch cancels old flight and discards late cache writes");
        mock.At(1).Complete(Address("42.56.67.170"), 60, "new");
        Check(newReplies == 1, "new-interface resolver remains usable after cancellation");
        Resolve(mock.Resolver, "late.example.com", 6, delegate { oldReplies++; }); Probe stopProbe = mock.At(2);
        Invoke(mock.Resolver, "Clear"); Invoke(mock.Resolver, "Restart");
        stopProbe.Complete(Address("1.2.3.4"), 60, "late");
        Check(oldReplies == 0 && stopProbe.Cancelled == 1 && ((IDictionary)Field(mock.Resolver, "cache")).Count == 0, "Stop and Restart reject previous-generation callbacks");
        Invoke(mock.Resolver, "Clear");

        mock = new Mock(); string resultSource = null;
        Resolve(mock.Resolver, "www.example.com", 5, delegate(IPAddress[] addresses, string source) { resultSource = source; });
        mock.At(0).Complete(null, 0, "timeout");
        Check(mock.Count == 2 && mock.At(0).Server == "223.5.5.5" && mock.At(1).Server == "223.6.6.6" && mock.At(0).Timeout <= 900 && mock.At(1).Timeout <= 900, "backup provider is bounded by the shared DoH deadline");
        mock.At(1).Complete(Address("42.56.67.170"), 60, "ok");
        Check(resultSource == "physicalDoH/cold", "backup-provider answer remains physical DoH");
        Resolve(mock.Resolver, "missing.example.com", 5, delegate(IPAddress[] addresses, string source) { resultSource = source; });
        mock.At(2).Complete(new IPAddress[0], 0, "negative");
        Check(resultSource == "physicalDoH/no-A" && ((IDictionary)Field(mock.Resolver, "failureCooldown")).Count == 0, "valid negative DNS answer does not degrade the whole interface");
        Invoke(mock.Resolver, "Clear");
    }
    static void LimitsAndFallback()
    {
        Mock mock = new Mock(); int completed = 0, degraded = 0;
        Task[] requestTasks = new Task[32];
        for (int i = 0; i < requestTasks.Length; i++)
        {
            int number = i;
            requestTasks[i] = Task.Factory.StartNew(delegate
            { Resolve(mock.Resolver, "load" + number + ".example.com", 5, delegate(IPAddress[] addresses, string source) { Interlocked.Increment(ref completed); if (source != "physicalDoH/cold") Interlocked.Increment(ref degraded); }); });
        }
        Task.WaitAll(requestTasks);
        Check(mock.Count == 16 && completed == 0, "32 different hosts queue behind a 16-flight DoH concurrency cap");
        for (int i = 0; i < 16; i++) mock.At(i).Complete(Address("42.56.67.170"), 60, "ok");
        Check(mock.Count == 32, "releasing DoH slots starts every queued hostname");
        for (int i = 16; i < 32; i++) mock.At(i).Complete(Address("42.56.67.170"), 60, "ok");
        Check(completed == 32 && degraded == 0, "32 fast different-host replies all remain physical DoH with no system fallback");
        Invoke(mock.Resolver, "Clear");
        mock = new Mock();
        for (int i = 0; i < 32; i++) Resolve(mock.Resolver, "cancel" + i + ".example.com", 5, delegate { });
        Invoke(mock.Resolver, "Clear"); bool allCancelled = mock.Count == 16;
        foreach (Probe probe in mock.Probes) if (probe.Cancelled != 1) allCancelled = false;
        Check(allCancelled && ((IDictionary)Field(mock.Resolver, "flights")).Count == 0, "Stop cancels active transports and discards queued hosts");

        mock = new Mock(); Invoke(mock.Resolver, "MarkFailure", 5); string fallbackSource = null;
        using (ManualResetEvent done = new ManualResetEvent(false))
        {
            Resolve(mock.Resolver, "localhost", 5, delegate(IPAddress[] addresses, string value) { fallbackSource = value; done.Set(); });
            Check(done.WaitOne(1000) && mock.Count == 0 && fallbackSource == "systemDNS-fallback/degraded/cooldown", "failure cooldown goes directly to an explicitly degraded system resolver");
        }
        Check(((IDictionary)Field(mock.Resolver, "cache")).Count == 0, "unknown-TTL system fallback is not cached");
        Invoke(mock.Resolver, "Clear");

        mock = new Mock(); Stopwatch watch = Stopwatch.StartNew();
        using (ManualResetEvent done = new ManualResetEvent(false))
        {
            Resolve(mock.Resolver, "never.example.com", 5, delegate(IPAddress[] addresses, string value) { fallbackSource = value; done.Set(); });
            Check(done.WaitOne(3100) && watch.ElapsedMilliseconds < 3000 && fallbackSource.Contains("degraded/timeout"), "whole DNS operation has a finite deadline even if a transport never calls back");
        }
        Check(mock.At(0).Cancelled == 1 && ((IDictionary)Field(mock.Resolver, "flights")).Count == 0, "deadline removes the flight and closes its transport");
        Invoke(mock.Resolver, "Clear");
    }
    static void Put16(List<byte> bytes, int value) { bytes.Add((byte)(value >> 8)); bytes.Add((byte)value); }
    static void Put32(List<byte> bytes, uint value) { Put16(bytes, (int)(value >> 16)); Put16(bytes, (int)value); }
    static byte[] Name(string name) { List<byte> bytes = new List<byte>(); foreach (string label in name.Split('.')) { bytes.Add((byte)label.Length); bytes.AddRange(Encoding.ASCII.GetBytes(label)); } bytes.Add(0); return bytes.ToArray(); }
    static void Record(List<byte> bytes, string owner, int type, uint ttl, byte[] body) { bytes.AddRange(Name(owner)); Put16(bytes, type); Put16(bytes, 1); Put32(bytes, ttl); Put16(bytes, body.Length); bytes.AddRange(body); }
    static byte[] Packet(bool cname)
    {
        byte[] query = (byte[])ResolverType.GetMethod("MakeQuery", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { "www.example.com" });
        query[2] = 0x81; query[3] = 0x80; query[7] = (byte)(cname ? 2 : 1); List<byte> bytes = new List<byte>(query);
        if (cname) Record(bytes, "www.example.com", 5, 60, Name("cdn.example.com"));
        Record(bytes, cname ? "cdn.example.com" : "www.example.com", 1, 240, new byte[] { 42, 56, 67, 170 }); return bytes.ToArray();
    }
    static byte[] ParseDns(byte[] packet, out uint ttl)
    {
        object[] args = new object[] { packet, packet.Length, (ushort)0, "www.example.com", (uint)0 };
        IPAddress[] answer = (IPAddress[])ResolverType.GetMethod("Parse", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, args);
        ttl = (uint)args[4]; return answer == null ? null : answer[0].GetAddressBytes();
    }
    static object Reply() { return Activator.CreateInstance(ResolverType.GetNestedType("DnsHttpReply", BindingFlags.NonPublic), true); }
    static void Add(object reply, byte[] bytes) { Invoke(reply, "Add", bytes, bytes.Length); }
    static bool Body(object reply, bool eof, out byte[] body) { object[] args = new object[] { eof, null }; bool complete = (bool)Invoke(reply, "TryBody", args); body = (byte[])args[1]; return complete; }
    static byte[] Join(byte[] first, byte[] second) { byte[] result = new byte[first.Length + second.Length]; Buffer.BlockCopy(first, 0, result, 0, first.Length); Buffer.BlockCopy(second, 0, result, first.Length, second.Length); return result; }
    static void HttpAndDns()
    {
        byte[] packet = Packet(false), body; uint ttl; object reply = Reply();
        byte[] response = Join(Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\nContent-Length: " + packet.Length + "\r\nAge: 7\r\n\r\n"), packet);
        byte[] head = new byte[13], rest = new byte[response.Length - 13]; Buffer.BlockCopy(response, 0, head, 0, head.Length); Buffer.BlockCopy(response, 13, rest, 0, rest.Length); Add(reply, head);
        Check(!Body(reply, false, out body), "fragmented HTTP header waits for completion"); Add(reply, rest);
        Check(Body(reply, false, out body) && BitConverter.ToString(body) == BitConverter.ToString(packet) && (uint)reply.GetType().GetProperty("Age", Flags).GetValue(reply, null) == 7, "DNS binary content-length body and HTTP Age parse");
        reply = Reply(); Add(reply, Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\nTransfer-Encoding: chunked\r\n\r\n"));
        Add(reply, Encoding.ASCII.GetBytes(packet.Length.ToString("X") + ";mock=yes\r\n")); Add(reply, packet); Add(reply, Encoding.ASCII.GetBytes("\r\n0\r\nX-Mock: done\r\n\r\n"));
        Check(Body(reply, false, out body) && BitConverter.ToString(body) == BitConverter.ToString(packet), "chunked DoH binary body and trailers parse");
        reply = Reply(); Add(reply, Encoding.ASCII.GetBytes("HTTP/1.1 302 Found\r\nContent-Type: application/dns-message\r\nContent-Length: 12\r\n\r\n")); bool rejected = false;
        try { Body(reply, false, out body); } catch (TargetInvocationException ex) { rejected = ex.InnerException is FormatException; }
        Check(rejected, "DoH redirects are rejected rather than changing trusted endpoint");
        reply = Reply(); Add(reply, Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 12\r\n\r\n")); rejected = false;
        try { Body(reply, false, out body); } catch (TargetInvocationException ex) { rejected = ex.InnerException is FormatException; }
        Check(rejected, "wrong DoH MIME type is rejected");
        ParseDns(Packet(true), out ttl); Check(ttl == 60, "A and CNAME chain use the minimum TTL");
        byte[] negative = (byte[])ResolverType.GetMethod("MakeQuery", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { "www.example.com" }); negative[2] = 0x81; negative[3] = 0x83;
        Check(ParseDns(negative, out ttl) == null, "authenticated NXDOMAIN is a valid negative DNS reply");
    }
    static X509Certificate2 Certificate()
    {
        using (RSA rsa = RSA.Create())
        {
            rsa.KeySize = 2048;
            CertificateRequest request = new CertificateRequest("CN=dns.alidns.com", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
            using (X509Certificate2 generated = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-5), DateTimeOffset.UtcNow.AddHours(1)))
                // DefaultKeySet creates only a transient private-key handle for
                // Schannel; disposing this certificate removes it. No cert store.
                return new X509Certificate2(generated.Export(X509ContentType.Pfx), "", X509KeyStorageFlags.DefaultKeySet);
        }
    }
    static void CertificateRejection()
    {
        using (X509Certificate2 certificate = Certificate())
        using (ManualResetEvent completed = new ManualResetEvent(false))
        {
            TcpListener listener = new TcpListener(IPAddress.Loopback, 0); listener.Start(); int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            SslStream serverStream = null; TcpClient accepted = null;
            listener.BeginAcceptTcpClient(delegate(IAsyncResult result)
            {
                try
                {
                    accepted = listener.EndAcceptTcpClient(result); serverStream = new SslStream(accepted.GetStream(), false);
                    serverStream.BeginAuthenticateAsServer(certificate, false, SslProtocols.Tls12, false, delegate(IAsyncResult authenticated)
                    { try { serverStream.EndAuthenticateAsServer(authenticated); } catch (Exception error) { Console.WriteLine("TLS mock server: " + error.Message); if (accepted != null) accepted.Close(); } }, null);
                }
                catch (Exception error) { Console.WriteLine("TLS mock setup: " + error.Message); if (accepted != null) accepted.Close(); }
            }, null);
            string reason = null;
            Action<IPAddress[], uint, string> done = delegate(IPAddress[] addresses, uint value, string error) { reason = error; completed.Set(); };
            Type query = ResolverType.GetNestedType("DnsQuery", BindingFlags.NonPublic);
            object probe = Activator.CreateInstance(query, Flags, null, new object[] { "www.example.com", 0, "127.0.0.1", 1500, done, port }, null);
            try
            {
                Invoke(probe, "Start");
                bool finished = completed.WaitOne(2100);
                Check(finished && reason == "TLS-validation", "default TLS validator rejects an untrusted certificate without a trust-store change (" + reason + ")");
            }
            finally { Invoke(probe, "Cancel"); listener.Stop(); if (accepted != null) accepted.Close(); if (serverStream != null) serverStream.Dispose(); }
        }
    }
    static int Main()
    {
        try { ThreadPool.SetMinThreads(80, 80); CacheAndFlights(); LimitsAndFallback(); HttpAndDns(); CertificateRejection(); Console.WriteLine("ALL " + checks + " DOH CHECKS PASSED"); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
