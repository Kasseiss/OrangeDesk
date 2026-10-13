using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class WinHttpPacProbe {
    [StructLayout(LayoutKind.Sequential)] private struct IeConfig { public int AutoDetect;public IntPtr AutoUrl;public IntPtr Proxy;public IntPtr Bypass; }
    [StructLayout(LayoutKind.Sequential)] private struct Options { public uint Flags;public uint AutoDetectFlags;[MarshalAs(UnmanagedType.LPWStr)] public string AutoUrl;public IntPtr Reserved;public uint ReservedNumber;public int AutoLogon; }
    [StructLayout(LayoutKind.Sequential)] private struct ProxyInfo {public uint AccessType;public IntPtr Proxy;public IntPtr Bypass;}
    [DllImport("winhttp.dll",SetLastError=true)] private static extern bool WinHttpGetIEProxyConfigForCurrentUser(out IeConfig config);
    [DllImport("winhttp.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern IntPtr WinHttpOpen(string agent,uint access,string proxy,string bypass,uint flags);
    [DllImport("winhttp.dll",SetLastError=true)] private static extern bool WinHttpSetTimeouts(IntPtr session,int resolve,int connect,int send,int receive);
    [DllImport("winhttp.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern bool WinHttpGetProxyForUrl(IntPtr session,string url,ref Options options,out ProxyInfo result);
    [DllImport("winhttp.dll")] private static extern bool WinHttpCloseHandle(IntPtr session);
    [DllImport("kernel32.dll")] private static extern IntPtr GlobalFree(IntPtr address);
    private static void Free(IntPtr value) {if(value!=IntPtr.Zero)GlobalFree(value);}
    public static string ReadSystemPac() {
        IeConfig config;if(!WinHttpGetIEProxyConfigForCurrentUser(out config))throw new Win32Exception(Marshal.GetLastWin32Error());
        try {return config.AutoUrl==IntPtr.Zero?"":Marshal.PtrToStringUni(config.AutoUrl);}finally {Free(config.AutoUrl);Free(config.Proxy);Free(config.Bypass);}
    }
    public static string Resolve(string url) {
        string pac=ReadSystemPac();
        if(String.IsNullOrEmpty(pac))throw new InvalidOperationException("System PAC is not active.");
        IntPtr session=WinHttpOpen("FastOrangeTemporarySplitTest",1,null,null,0);
        if(session==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
        ProxyInfo result=new ProxyInfo();
        try {
            WinHttpSetTimeouts(session,2000,2000,3000,3000);
            Options options=new Options {Flags=2,AutoUrl=pac,AutoLogon=0};
            if(!WinHttpGetProxyForUrl(session,url,ref options,out result))throw new Win32Exception(Marshal.GetLastWin32Error(),"System PAC download or evaluation failed.");
            return result.Proxy==IntPtr.Zero?"DIRECT":Marshal.PtrToStringUni(result.Proxy);
        }finally {Free(result.Proxy);Free(result.Bypass);WinHttpCloseHandle(session);}
    }
}
