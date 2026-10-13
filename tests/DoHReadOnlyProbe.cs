using System;
using System.Diagnostics;
using System.Net;
using System.Reflection;
using System.Threading;
using FastOrangeCombined;

// Read-only DNS/TLS probe: no system proxy, route or certificate-store changes.
class DoHReadOnlyProbe
{
    static int Main(string[] args)
    {
        if (args.Length != 1) return 2;
        int index = Int32.Parse(args[0]);
        Type query = typeof(BridgeServer).Assembly.GetType("FastOrangeCombined.BridgeServer+DirectDns+DnsQuery");
        BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        int failures = 0;
        foreach (string host in new string[] {
            "www.baidu.com", "www.zhihu.com", "pic1.zhimg.com", "www.bilibili.com", "i1.hdslb.com",
            "www.taobao.com", "www.jd.com", "weibo.com", "www.douyin.com", "www.qq.com", "www.updream.cn"
        })
        foreach (string server in new string[] { "223.5.5.5", "223.6.6.6" })
        {
            using (ManualResetEvent done = new ManualResetEvent(false))
            {
                Stopwatch watch = Stopwatch.StartNew();
                Action<IPAddress[], uint, string> callback = delegate(IPAddress[] addresses, uint ttl, string reason)
                {
                    watch.Stop();
                    Console.WriteLine(host + " server=" + server + " reason=" + reason + " ms=" + watch.ElapsedMilliseconds + " TTL=" + ttl +
                        " A=" + (addresses == null ? "none" : String.Join(",", Array.ConvertAll(addresses, delegate(IPAddress address) { return address.ToString(); }))));
                    if (addresses == null || addresses.Length == 0) failures++;
                    done.Set();
                };
                object probe = Activator.CreateInstance(query, flags, null,
                    new object[] { host, index, server, 1800, callback }, null);
                query.GetMethod("Start", flags).Invoke(probe, null);
                if (!done.WaitOne(2500)) { query.GetMethod("Cancel", flags).Invoke(probe, null); failures++; }
            }
        }
        return failures == 0 ? 0 : 1;
    }
}
