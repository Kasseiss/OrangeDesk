using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32;

namespace FastOrangeCombined {
    public class RawProxyValue {
        public string Path;
        public string Name;
        public bool Exists;
        public string Kind;
        public string Data;
    }
    public class ProxyProfile {
        public uint Flags;
        public string Server;
        public string Bypass;
        public string AutoUrl;
        public RawProxyValue[] Raw;
    }
    public static class ProxySettings {
        [StructLayout(LayoutKind.Explicit, Size=8)] private struct OptionValue {
            [FieldOffset(0)] public uint Number;
            [FieldOffset(0)] public IntPtr Text;
        }
        [StructLayout(LayoutKind.Sequential)] private struct Option { public uint Code; public OptionValue Value; }
        [StructLayout(LayoutKind.Sequential)] private struct OptionList {
            public uint Size; public IntPtr Connection; public uint Count; public uint Error; public IntPtr Options;
        }
        [DllImport("wininet.dll", EntryPoint="InternetQueryOptionW", SetLastError=true)]
        private static extern bool Query(IntPtr handle,uint option,ref OptionList list,ref uint size);
        [DllImport("wininet.dll", EntryPoint="InternetSetOptionW", SetLastError=true)]
        private static extern bool Set(IntPtr handle,uint option,ref OptionList list,uint size);
        [DllImport("wininet.dll", EntryPoint="InternetSetOptionW", SetLastError=true)]
        private static extern bool Notify(IntPtr handle,uint option,IntPtr buffer,uint size);
        [DllImport("kernel32.dll")] private static extern IntPtr GlobalFree(IntPtr memory);
        private const string SettingsPath="Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";
        public static RawProxyValue[] CaptureRaw() {
            List<RawProxyValue> result=new List<RawProxyValue>();
            string[][] names=new string[][] {
                new string[] {"ProxyEnable","ProxyServer","ProxyOverride","AutoConfigURL","AutoDetect"},
                new string[] {"DefaultConnectionSettings","SavedLegacySettings"}
            };
            for(int section=0;section<2;section++) {
                string path=SettingsPath+(section==1?"\\Connections":"");
                using(RegistryKey key=Registry.CurrentUser.OpenSubKey(path)) {
                    foreach(string name in names[section]) {
                        RawProxyValue value=new RawProxyValue {Path=path,Name=name,Exists=false,Kind="",Data=""};
                        if(key!=null) {
                            object data=key.GetValue(name,null,RegistryValueOptions.DoNotExpandEnvironmentNames);
                            if(data!=null) {
                                value.Exists=true; value.Kind=key.GetValueKind(name).ToString();
                                value.Data=data is byte[]?Convert.ToBase64String((byte[])data):Convert.ToString(data,CultureInfo.InvariantCulture);
                            }
                        }
                        result.Add(value);
                    }
                }
            }
            return result.ToArray();
        }
        private static string Text(IntPtr value) { return value==IntPtr.Zero?"":Marshal.PtrToStringUni(value); }
        public static ProxyProfile Read() {
            int stride=Marshal.SizeOf(typeof(Option));
            IntPtr memory=Marshal.AllocHGlobal(stride*4);
            OptionList list=new OptionList {Size=(uint)Marshal.SizeOf(typeof(OptionList)),Count=4,Options=memory};
            uint size=list.Size;
            try {
                for(int i=0;i<4;i++) { Option item=new Option();item.Code=(uint)(i+1);Marshal.StructureToPtr(item,IntPtr.Add(memory,i*stride),false); }
                if(!Query(IntPtr.Zero,75,ref list,ref size)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(),"Reading system proxy settings failed.");
                Option flags=(Option)Marshal.PtrToStructure(memory,typeof(Option));
                ProxyProfile profile=new ProxyProfile {Flags=flags.Value.Number};
                for(int i=1;i<4;i++) {
                    Option item=(Option)Marshal.PtrToStructure(IntPtr.Add(memory,i*stride),typeof(Option));
                    string text=Text(item.Value.Text);
                    if(i==1)profile.Server=text; else if(i==2)profile.Bypass=text; else profile.AutoUrl=text;
                    if(item.Value.Text!=IntPtr.Zero)GlobalFree(item.Value.Text);
                }
                profile.Raw=CaptureRaw();
                return profile;
            } finally { Marshal.FreeHGlobal(memory); }
        }
        public static void Write(ProxyProfile profile) {
            int stride=Marshal.SizeOf(typeof(Option));
            IntPtr memory=Marshal.AllocHGlobal(stride*4);
            List<IntPtr> strings=new List<IntPtr>();
            OptionList list=new OptionList {Size=(uint)Marshal.SizeOf(typeof(OptionList)),Count=4,Options=memory};
            try {
                for(int i=0;i<4;i++) {
                    Option item=new Option(); item.Code=(uint)(i+1);
                    if(i==0)item.Value.Number=profile.Flags;
                    else {
                        string value=i==1?profile.Server:(i==2?profile.Bypass:profile.AutoUrl);
                        item.Value.Text=Marshal.StringToHGlobalUni(value??"");strings.Add(item.Value.Text);
                    }
                    Marshal.StructureToPtr(item,IntPtr.Add(memory,i*stride),false);
                }
                if(!Set(IntPtr.Zero,75,ref list,list.Size))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(),"Updating system proxy settings failed.");
                Refresh();
            } finally { foreach(IntPtr value in strings)Marshal.FreeHGlobal(value);Marshal.FreeHGlobal(memory); }
        }
        public static void Refresh() { Notify(IntPtr.Zero,39,IntPtr.Zero,0);Notify(IntPtr.Zero,37,IntPtr.Zero,0); }
        private static bool SameRaw(RawProxyValue first,RawProxyValue second) {
            return first!=null && second!=null && first.Exists==second.Exists && first.Kind==second.Kind && first.Data==second.Data;
        }
        public static uint MergeFlags(uint before,uint applied,uint current) {
            uint externalChanges=applied^current;
            return (current&externalChanges)|(before&~externalChanges);
        }
        public static bool RestoreOwned(ProxyProfile before,ProxyProfile applied,string ownedUrl) {
            ProxyProfile current=Read();
            if(current.AutoUrl!=ownedUrl)return false;
            ProxyProfile restored=new ProxyProfile {Flags=MergeFlags(before.Flags,applied.Flags,current.Flags),
                Server=current.Server==applied.Server?before.Server:current.Server,
                Bypass=current.Bypass==applied.Bypass?before.Bypass:current.Bypass,AutoUrl=before.AutoUrl};
            Write(restored);
            // Restore original absence/value/connection bytes only when this tool's value remained unchanged.
            if(before.Raw!=null && applied.Raw!=null)foreach(RawProxyValue original in before.Raw) {
                RawProxyValue own=Array.Find(applied.Raw,delegate(RawProxyValue x){return x.Path==original.Path && x.Name==original.Name;});
                RawProxyValue live=Array.Find(current.Raw,delegate(RawProxyValue x){return x.Path==original.Path && x.Name==original.Name;});
                if(!SameRaw(own,live))continue;
                using(RegistryKey key=Registry.CurrentUser.CreateSubKey(original.Path)) {
                    if(!original.Exists)key.DeleteValue(original.Name,false);
                    else {
                        RegistryValueKind kind=(RegistryValueKind)Enum.Parse(typeof(RegistryValueKind),original.Kind);
                        object value=original.Data;
                        if(kind==RegistryValueKind.Binary)value=Convert.FromBase64String(original.Data);
                        else if(kind==RegistryValueKind.DWord)value=Int32.Parse(original.Data,CultureInfo.InvariantCulture);
                        else if(kind==RegistryValueKind.QWord)value=Int64.Parse(original.Data,CultureInfo.InvariantCulture);
                        key.SetValue(original.Name,value,kind);
                    }
                }
            }
            Refresh();return true;
        }
    }
    public class ProxyLease : IDisposable {
        private readonly ProxyProfile before;
        private ProxyProfile applied;
        private readonly string url;
        private readonly int controller;
        private readonly long ticks;
        private readonly string stopFile;
        private readonly object gate=new object();
        private Thread watcher;
        private volatile bool stopping;
        private bool completed;
        private IDisposable resource;
        public bool Restored { get; private set; }
        public bool ExternalChangePreserved { get; private set; }
        public string LastError { get; private set; }
        public ProxyProfile Applied { get {return applied;} }
        public void AttachResource(IDisposable value) { resource=value; }
        public ProxyLease(ProxyProfile original,string pacUrl,int controllerId,long controllerTicks,string stopPath) {
            before=original;url=pacUrl;controller=controllerId;ticks=controllerTicks;stopFile=stopPath;LastError="";
        }
        public void Start() {
            lock(gate) {
                ProxySettings.Write(new ProxyProfile {Flags=5,Server=before.Server,Bypass=before.Bypass,AutoUrl=url});
                applied=ProxySettings.Read();
                if(applied.AutoUrl!=url || (applied.Flags&4)==0)throw new InvalidOperationException("Temporary PAC settings did not apply.");
                watcher=new Thread(Watch);watcher.IsBackground=true;watcher.Start();
            }
        }
        public bool Restore() {
            lock(gate) {
                if(completed)return Restored;
                if(applied==null)applied=ProxySettings.Read();
                bool result=ProxySettings.RestoreOwned(before,applied,url);
                Restored=result;ExternalChangePreserved=!result;completed=true;return result;
            }
        }
        private void Watch() {
            while(!stopping) {
                bool alive=false;
                try { using(Process process=Process.GetProcessById(controller))alive=!process.HasExited && process.StartTime.ToUniversalTime().Ticks==ticks; }catch { }
                if(!alive || File.Exists(stopFile)) {
                    try {Restore();}catch(Exception error){LastError=error.Message;}
                    finally {if(resource!=null)resource.Dispose();}
                    return;
                }
                Thread.Sleep(200);
            }
        }
        public void Dispose() {
            stopping=true;
            if(watcher!=null && watcher!=Thread.CurrentThread)watcher.Join(1500);
            try {Restore();}finally{if(resource!=null)resource.Dispose();}
        }
    }
}
