// Windows x64 / .NET Framework 4.x. This file does not install anything on load.
// Native definitions verified against Microsoft's win32metadata SDK headers:
// generation/WinSDK/RecompiledIdlHeaders/{um/fwpmu.h,shared/fwpmtypes.h,shared/fwptypes.h}.
// Protection is for these executable paths across all users (not ALE_USER_ID scoped).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

namespace OrangeDeskClaudeGuard
{
    public sealed class Snapshot
    {
        public bool Enabled { get; internal set; }
        public int FilterCount { get; internal set; }
        public string[] Apps { get; internal set; }
        public ulong InterfaceLuid { get; internal set; }
        public string Message { get; internal set; }
        public bool UserScoped { get { return false; } }
        public string OwnerSid { get; internal set; }
        public string SubLayerKey { get; internal set; }
        public string[] ValidationErrors { get; internal set; }
        public string[] FilterKeys { get; internal set; }
    }

    public sealed class NativeError : InvalidOperationException
    {
        public uint NativeCode { get; private set; }
        internal NativeError(string operation, uint code)
            : base(operation + " failed: native code 0x" + code.ToString("X8", CultureInfo.InvariantCulture) +
                   (code == 5 || code == 0x80070005 ? "; WFP access denied. Administrator permission may be required; state was not assumed." : ""))
        { NativeCode = code; }
    }

    internal static class Ids
    {
        // fwpmtypes.h: BFE may add FWPM_FILTER_FLAG_INDEXED (0x40) on readback.
        // No other filter flag is normalized away, especially DISABLED (0x20).
        internal const uint Persistent = 1, Indexed = 0x40, Block = 0x1001;
        internal const uint UInt8 = 1, UInt32 = 3, UInt64 = 4, ByteBlob = 12;
        internal const uint Equal = 0, FlagsNoneSet = 8, NotEqual = 10, Loopback = 1;
        internal static readonly Guid Layer4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
        internal static readonly Guid Layer6 = new Guid("4a72393b-319f-44bc-84c3-ba54dcb3b6b4");
        internal static readonly Guid AppId = new Guid("d78e1e87-8644-4ea5-9437-d809ecefc971");
        internal static readonly Guid LocalInterface = new Guid("4cd62a49-59c3-4969-b7f3-bda5d32890a4");
        internal static readonly Guid NextInterface = new Guid("93ae8f5b-7f6f-4719-98c8-14e97429ef04");
        internal static readonly Guid Flags = new Guid("632ce23b-5167-435c-86d7-e903684aa80c");
        internal const string Namespace = "OrangeDeskClaudeGuard/5d57fb72-8278-4b49-a2ba-93458d0b0c77/v1/";
        internal static Guid SubLayer(string sid)
        {
            byte[] hash;
            using (SHA256 sha = SHA256.Create()) hash = sha.ComputeHash(Encoding.UTF8.GetBytes(Namespace + sid));
            byte[] bytes = new byte[16]; Array.Copy(hash, bytes, 16);
            bytes[7] = (byte)((bytes[7] & 15) | 0x50); bytes[8] = (byte)((bytes[8] & 63) | 0x80);
            return new Guid(bytes);
        }
        internal static string OwnerPrefix(string sid) { return "OrangeDeskClaudeGuard|v1|owner=" + sid + "|"; }
        internal static string SubTag(string sid) { return OwnerPrefix(sid) + "sublayer"; }
        internal static string Hash(byte[] bytes)
        { using (SHA256 sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", ""); }
    }

    // Explicit x64 layouts avoid accidental pointer/union alignment changes.
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    internal struct Blob { [FieldOffset(0)] internal uint Size; [FieldOffset(8)] internal IntPtr Data; }
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    internal struct Value
    {
        [FieldOffset(0)] internal uint Type;
        [FieldOffset(8)] internal IntPtr Pointer;
        [FieldOffset(8)] internal uint Number;
    }
    [StructLayout(LayoutKind.Explicit, Size = 40)]
    internal struct Condition
    { [FieldOffset(0)] internal Guid Field; [FieldOffset(16)] internal uint Match; [FieldOffset(24)] internal Value Data; }
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    internal struct Display { [FieldOffset(0)] internal IntPtr Name; [FieldOffset(8)] internal IntPtr Description; }
    [StructLayout(LayoutKind.Explicit, Size = 20)]
    internal struct Action { [FieldOffset(0)] internal uint Type; [FieldOffset(4)] internal Guid Key; }
    [StructLayout(LayoutKind.Explicit, Size = 72)]
    internal struct SubLayerNative
    {
        [FieldOffset(0)] internal Guid Key; [FieldOffset(16)] internal Display Display;
        [FieldOffset(32)] internal uint Flags; [FieldOffset(40)] internal IntPtr Provider;
        [FieldOffset(48)] internal Blob Data; [FieldOffset(64)] internal ushort Weight;
    }
    [StructLayout(LayoutKind.Explicit, Size = 200)]
    internal struct FilterNative
    {
        [FieldOffset(0)] internal Guid Key; [FieldOffset(16)] internal Display Display;
        [FieldOffset(32)] internal uint Flags; [FieldOffset(40)] internal IntPtr Provider;
        [FieldOffset(48)] internal Blob Data; [FieldOffset(64)] internal Guid Layer;
        [FieldOffset(80)] internal Guid SubLayer; [FieldOffset(96)] internal Value Weight;
        [FieldOffset(112)] internal uint ConditionCount; [FieldOffset(120)] internal IntPtr Conditions;
        [FieldOffset(128)] internal Action Action; [FieldOffset(152)] internal Guid ContextUnion;
        [FieldOffset(168)] internal IntPtr Reserved; [FieldOffset(176)] internal ulong Id;
        [FieldOffset(184)] internal Value EffectiveWeight;
    }

    internal sealed class SubLayerRecord
    { internal Guid Key; internal string Name, Tag; internal uint Flags; internal bool NullProvider; }
    internal sealed class FilterRecord
    {
        internal Guid Key, SubLayer, Layer, InterfaceField;
        internal string Tag, AppPath, ActualAppIdHash;
        internal string ShapeDetails;
        internal ulong Luid;
        internal bool ShapeValid;
        internal FilterRecord Clone() { return (FilterRecord)MemberwiseClone(); }
    }
    internal sealed class FilterSpec
    {
        internal Guid Key, SubLayer, Layer, InterfaceField;
        internal string OwnerSid, AppPath;
        internal byte[] AppId;
        internal ulong Luid;
        internal string Tag
        {
            get { return Ids.OwnerPrefix(OwnerSid) + "app=" + Convert.ToBase64String(Encoding.UTF8.GetBytes(AppPath)) +
                ";luid=" + Luid.ToString(CultureInfo.InvariantCulture) + ";family=" + (Layer == Ids.Layer4 ? "4" : "6") +
                ";interface=" + (InterfaceField == Ids.LocalInterface ? "local" : "next") + ";hash=" + Ids.Hash(AppId); }
        }
        internal FilterRecord Record()
        { return new FilterRecord { Key = Key, SubLayer = SubLayer, Layer = Layer, InterfaceField = InterfaceField,
            Tag = Tag, AppPath = AppPath, ActualAppIdHash = Ids.Hash(AppId), Luid = Luid, ShapeValid = true }; }
    }

    internal interface IWfpSession : IDisposable
    {
        void Begin(bool readOnly); void Commit(); void Abort();
        SubLayerRecord GetSubLayer(Guid key); void AddSubLayer(Guid key, string name, string tag, string ownerSid);
        List<FilterRecord> Filters(Guid subLayer); FilterRecord GetFilter(Guid key, Guid expectedSubLayer); void AddFilter(FilterSpec filter);
        void DeleteFilter(Guid key); void DeleteSubLayer(Guid key); byte[] AppId(string path);
    }

    public static class Policy
    {
        public static Snapshot Install(string ownerSid, string[] appPaths, ulong vpnLuid)
        {
            string sid = ValidateSid(ownerSid); string[] apps = NormalizeApps(appPaths);
            if (vpnLuid == 0) throw new ArgumentException("VPN NET_LUID must be nonzero; no rules were installed.", "vpnLuid");
            return InstallPrepared(sid, apps, vpnLuid, new NativeSession());
        }
        public static Snapshot Remove(string ownerSid) { return RemoveCore(ValidateSid(ownerSid), new NativeSession()); }
        public static Snapshot Read(string ownerSid) { return ReadCore(ValidateSid(ownerSid), new NativeSession()); }
        public static Snapshot ReadKnown(string ownerSid, string[] keys)
        {
            string sid = ValidateSid(ownerSid); Guid[] known = NormalizeKeys(keys);
            return ReadKnownCore(sid, known, new NativeSession());
        }

        internal static Snapshot Install(string ownerSid, string[] appPaths, ulong vpnLuid, IWfpSession session)
        {
            string sid; string[] apps;
            try
            {
                sid = ValidateSid(ownerSid); apps = NormalizeApps(appPaths);
                if (vpnLuid == 0) throw new ArgumentException("VPN NET_LUID must be nonzero.", "vpnLuid");
            }
            catch { session.Dispose(); throw; }
            return InstallPrepared(sid, apps, vpnLuid, session);
        }
        internal static Snapshot Remove(string sid, IWfpSession session) { return RemoveCore(ValidateSid(sid), session); }
        internal static Snapshot Read(string sid, IWfpSession session) { return ReadCore(ValidateSid(sid), session); }
        internal static Snapshot ReadKnown(string sid, string[] keys, IWfpSession session)
        { return ReadKnownCore(ValidateSid(sid), NormalizeKeys(keys), session); }

        private static Guid[] NormalizeKeys(string[] keys)
        {
            if (keys == null || keys.Length > 128) throw new ArgumentException("Provide at most 128 known filter GUID keys.", "keys");
            SortedSet<Guid> result = new SortedSet<Guid>();
            foreach (string text in keys)
            {
                Guid key;
                if (!Guid.TryParse(text, out key) || key == Guid.Empty) throw new ArgumentException("Known filter keys must be nonempty GUIDs.", "keys");
                result.Add(key);
            }
            Guid[] normalized = new Guid[result.Count]; result.CopyTo(normalized); return normalized;
        }

        private static string ValidateSid(string sid)
        {
            if (String.IsNullOrWhiteSpace(sid)) throw new ArgumentException("Owner SID is required.", "ownerSid");
            try { return new SecurityIdentifier(sid).Value; }
            catch (ArgumentException) { throw new ArgumentException("Owner SID is invalid.", "ownerSid"); }
        }
        private static string[] NormalizeApps(string[] paths)
        {
            if (paths == null || paths.Length == 0 || paths.Length > 32) throw new ArgumentException("Provide 1-32 native claude.exe paths.", "appPaths");
            SortedSet<string> apps = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string path in paths)
            {
                if (String.IsNullOrWhiteSpace(path) || !Path.IsPathRooted(path)) throw new ArgumentException("Application paths must be absolute.");
                string full = Path.GetFullPath(path);
                if (!String.Equals(Path.GetFileName(full), "claude.exe", StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Only native claude.exe paths can be protected.");
                using (FileStream file = File.Open(full, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (BinaryReader reader = new BinaryReader(file))
                {
                    if (file.Length < 88 || reader.ReadUInt16() != 0x5A4D) throw new ArgumentException("Application is not a native PE executable.");
                    file.Position = 0x3C; int offset = reader.ReadInt32();
                    if (offset < 64 || offset > file.Length - 24) throw new ArgumentException("Invalid PE header offset.");
                    file.Position = offset;
                    if (reader.ReadUInt32() != 0x00004550) throw new ArgumentException("Application is not a native PE executable.");
                    ushort machine = reader.ReadUInt16();
                    file.Position = offset + 22;
                    if ((machine != 0x8664 && machine != 0x014C && machine != 0xAA64) || (reader.ReadUInt16() & 2) == 0)
                        throw new ArgumentException("Application is not a supported native executable image.");
                }
                apps.Add(full);
            }
            string[] result = new string[apps.Count]; apps.CopyTo(result); return result;
        }

        private static void AssertSubLayer(SubLayerRecord layer, Guid key, string sid)
        {
            if (layer == null) return;
            if (layer.Key != key || layer.Tag != Ids.SubTag(sid) || layer.Name != "OrangeDesk Claude guard / " + sid ||
                layer.Flags != Ids.Persistent || !layer.NullProvider)
                throw new InvalidOperationException("Sublayer ownership or persistence does not match; no foreign rules will be changed.");
        }
        private static void AssertFilterOwners(List<FilterRecord> filters, Guid key, string sid)
        {
            foreach (FilterRecord filter in filters)
                if (filter.SubLayer != key || filter.Tag == null || !filter.Tag.StartsWith(Ids.OwnerPrefix(sid) + "app=", StringComparison.Ordinal))
                    throw new InvalidOperationException("A filter in the owned sublayer has a foreign owner tag; refusing any deletion.");
        }
        private static Snapshot Inspect(string sid, Guid key, List<FilterRecord> filters)
        {
            AssertFilterOwners(filters, key, sid);
            SortedSet<string> apps = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> combinations = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            ulong luid = 0; bool valid = filters.Count > 0;
            List<string> errors = new List<string>();
            foreach (FilterRecord filter in filters)
            {
                string app; ulong taggedLuid; string family, kind, hash;
                bool parsed = ParseTag(filter.Tag, sid, out app, out taggedLuid, out family, out kind, out hash);
                if (!parsed) { valid = false; errors.Add("Filter " + filter.Key + ": owner tag payload cannot be parsed."); continue; }
                apps.Add(app);
                string combo = app + "|" + family + "|" + kind;
                List<string> mismatch = new List<string>();
                if (!combinations.Add(combo)) mismatch.Add("duplicate app/family/interface combination");
                if (!filter.ShapeValid) mismatch.Add("native shape invalid: " + (filter.ShapeDetails ?? "no native shape detail"));
                if (filter.AppPath != app) mismatch.Add("AppPath differs from owner tag: decoded=" + filter.AppPath + ", tagged=" + app);
                if (filter.Luid == 0 || filter.Luid != taggedLuid) mismatch.Add("LUID actual=" + filter.Luid + ", tagged=" + taggedLuid);
                if (filter.ActualAppIdHash != hash) mismatch.Add("AppId hash actual=" + filter.ActualAppIdHash + ", tagged=" + hash);
                if (filter.Layer != (family == "4" ? Ids.Layer4 : Ids.Layer6)) mismatch.Add("Layer actual=" + filter.Layer + ", family=" + family);
                if (filter.InterfaceField != (kind == "local" ? Ids.LocalInterface : Ids.NextInterface)) mismatch.Add("InterfaceField actual=" + filter.InterfaceField + ", tagged=" + kind);
                if (mismatch.Count > 0) { valid = false; errors.Add("Filter " + filter.Key + ": " + String.Join("; ", mismatch.ToArray())); }
                if (luid == 0) luid = filter.Luid;
                else if (luid != filter.Luid) { valid = false; errors.Add("Filters disagree on LUID: " + luid + " vs " + filter.Luid); }
            }
            if (apps.Count == 0 || filters.Count != apps.Count * 4)
            { valid = false; errors.Add("FilterCount=" + filters.Count + ", parsedApps=" + apps.Count + ", expectedFilters=" + (apps.Count * 4)); }
            foreach (string app in apps)
                foreach (string family in new string[] { "4", "6" })
                    foreach (string kind in new string[] { "local", "next" })
                        if (!combinations.Contains(app + "|" + family + "|" + kind))
                        { valid = false; errors.Add("Missing combination: " + app + " / IPv" + family + " / " + kind); }
            string[] paths = new string[apps.Count]; apps.CopyTo(paths);
            string[] keys = new string[filters.Count];
            for (int i = 0; i < filters.Count; i++) keys[i] = filters[i].Key.ToString("D");
            Array.Sort(keys, StringComparer.Ordinal);
            return new Snapshot { Enabled = valid, FilterCount = filters.Count, Apps = paths,
                InterfaceLuid = luid, OwnerSid = sid, SubLayerKey = key.ToString("D"), ValidationErrors = errors.ToArray(), FilterKeys = keys,
                Message = valid ? "Persistent IPv4/IPv6 BLOCK policy verified. Scope: all users of these native executable paths; loopback excluded. No country verification."
                    : filters.Count == 0 ? "No owned Claude network protection filters are installed."
                    : "Owned filters are incomplete or inconsistent; protection is not reported as enabled." };
        }
        internal static bool ParseTag(string tag, string sid, out string app, out ulong luid, out string family, out string kind, out string hash)
        {
            app = family = kind = hash = ""; luid = 0;
            string prefix = Ids.OwnerPrefix(sid);
            if (tag == null || !tag.StartsWith(prefix, StringComparison.Ordinal)) return false;
            string[] parts = tag.Substring(prefix.Length).Split(';');
            if (parts.Length != 5 || !parts[0].StartsWith("app=", StringComparison.Ordinal) || !parts[1].StartsWith("luid=", StringComparison.Ordinal) ||
                !parts[2].StartsWith("family=", StringComparison.Ordinal) || !parts[3].StartsWith("interface=", StringComparison.Ordinal) || !parts[4].StartsWith("hash=", StringComparison.Ordinal)) return false;
            try { app = new UTF8Encoding(false, true).GetString(Convert.FromBase64String(parts[0].Substring(4))); }
            catch (Exception) { return false; }
            family = parts[2].Substring(7); kind = parts[3].Substring(10); hash = parts[4].Substring(5);
            return Path.IsPathRooted(app) && String.Equals(Path.GetFileName(app), "claude.exe", StringComparison.OrdinalIgnoreCase) &&
                UInt64.TryParse(parts[1].Substring(5), NumberStyles.None, CultureInfo.InvariantCulture, out luid) && luid != 0 &&
                (family == "4" || family == "6") && (kind == "local" || kind == "next") && hash.Length == 64;
        }

        private static T Transaction<T>(IWfpSession session, bool readOnly, Func<T> body)
        {
            session.Begin(readOnly);
            try { T result = body(); session.Commit(); return result; }
            catch (Exception failure)
            {
                try { session.Abort(); }
                catch (Exception abortFailure)
                { throw new AggregateException("WFP transaction and rollback both failed; original and rollback native errors are preserved.", failure, abortFailure); }
                throw;
            }
        }

        private static Snapshot InstallPrepared(string sid, string[] apps, ulong luid, IWfpSession session)
        {
            using (session)
            {
                Guid key = Ids.SubLayer(sid); List<FilterSpec> planned = new List<FilterSpec>();
                foreach (string app in apps)
                {
                    byte[] appId = session.AppId(app);
                    if (appId == null || appId.Length == 0) throw new InvalidOperationException("Native application ID is empty.");
                    foreach (Guid layer in new Guid[] { Ids.Layer4, Ids.Layer6 })
                        foreach (Guid field in new Guid[] { Ids.LocalInterface, Ids.NextInterface })
                            planned.Add(new FilterSpec { Key = Guid.NewGuid(), SubLayer = key, Layer = layer,
                                InterfaceField = field, OwnerSid = sid, AppPath = app, AppId = appId, Luid = luid });
                }
                return Transaction(session, false, delegate
                {
                    SubLayerRecord existing = session.GetSubLayer(key); AssertSubLayer(existing, key, sid);
                    if (existing == null) session.AddSubLayer(key, "OrangeDesk Claude guard / " + sid, Ids.SubTag(sid), sid);
                    List<FilterRecord> old = session.Filters(key); AssertFilterOwners(old, key, sid);
                    foreach (FilterRecord filter in old) session.DeleteFilter(filter.Key);
                    foreach (FilterSpec filter in planned) session.AddFilter(filter);
                    Snapshot result = Inspect(sid, key, session.Filters(key));
                    if (!result.Enabled || result.FilterCount != planned.Count)
                        throw new InvalidOperationException("Native readback did not verify the complete new policy; transaction will be aborted. Planned=" + planned.Count +
                            ", observed=" + result.FilterCount + ". " + String.Join(" || ", result.ValidationErrors));
                    return result;
                });
            }
        }
        private static Snapshot RemoveCore(string sid, IWfpSession session)
        {
            using (session)
            {
                Guid key = Ids.SubLayer(sid);
                return Transaction(session, false, delegate
                {
                    SubLayerRecord existing = session.GetSubLayer(key); AssertSubLayer(existing, key, sid);
                    if (existing != null)
                    {
                        List<FilterRecord> filters = session.Filters(key); AssertFilterOwners(filters, key, sid);
                        foreach (FilterRecord filter in filters) session.DeleteFilter(filter.Key);
                        session.DeleteSubLayer(key);
                    }
                    Snapshot result = Inspect(sid, key, new List<FilterRecord>());
                    result.Message = "Owned Claude protection rules removed atomically; no other sublayer was changed."; return result;
                });
            }
        }
        private static Snapshot ReadCore(string sid, IWfpSession session)
        {
            using (session)
            {
                Guid key = Ids.SubLayer(sid);
                // BEGIN_READ_TXN has a separate WFP ACL and can be denied to a
                // standard user who can enumerate objects. Enumeration creates
                // its own BFE snapshot; all mutations remain atomic transactions.
                SubLayerRecord existing = session.GetSubLayer(key); AssertSubLayer(existing, key, sid);
                return Inspect(sid, key, existing == null ? new List<FilterRecord>() : session.Filters(key));
            }
        }

        private static Snapshot ReadKnownCore(string sid, Guid[] keys, IWfpSession session)
        {
            using (session)
            {
                Guid subLayer = Ids.SubLayer(sid);
                SubLayerRecord existing = session.GetSubLayer(subLayer); AssertSubLayer(existing, subLayer, sid);
                List<FilterRecord> filters = new List<FilterRecord>(); List<string> missing = new List<string>();
                foreach (Guid key in keys)
                {
                    FilterRecord filter = session.GetFilter(key, subLayer);
                    if (filter == null) missing.Add("Known filter is missing: " + key.ToString("D"));
                    else
                    {
                        if (filter.Key != key || filter.SubLayer != subLayer)
                            throw new InvalidOperationException("Known filter key belongs to another sublayer or identity; no foreign object will be trusted.");
                        filters.Add(filter);
                    }
                }
                Snapshot result = Inspect(sid, subLayer, filters);
                // Preserve the expected keys, including missing keys, so a UI
                // cache cannot accidentally shrink the expected policy to the
                // surviving filters and declare a partial policy complete.
                result.FilterKeys = new string[keys.Length];
                for (int i = 0; i < keys.Length; i++) result.FilterKeys[i] = keys[i].ToString("D");
                if (missing.Count > 0 || existing == null || keys.Length == 0)
                {
                    result.Enabled = false;
                    List<string> errors = new List<string>(result.ValidationErrors); errors.AddRange(missing);
                    if (existing == null) errors.Add("The owned sublayer is not present.");
                    if (keys.Length == 0) errors.Add("No known filter keys were supplied.");
                    result.ValidationErrors = errors.ToArray();
                    result.Message = "Known policy keys are missing or unavailable; protection cannot be reported as enabled. " + String.Join("; ", missing.ToArray());
                }
                return result;
            }
        }
    }

    internal sealed class Arena : IDisposable
    {
        private readonly List<IntPtr> blocks = new List<IntPtr>();
        internal IntPtr Allocate(int size) { IntPtr block = Marshal.AllocHGlobal(size); blocks.Add(block); return block; }
        internal IntPtr Text(string text) { IntPtr block = Marshal.StringToHGlobalUni(text); blocks.Add(block); return block; }
        internal IntPtr Structure<T>(T value) where T : struct
        { IntPtr block = Allocate(Marshal.SizeOf(typeof(T))); Marshal.StructureToPtr(value, block, false); return block; }
        internal Blob Bytes(byte[] bytes)
        { IntPtr block = Allocate(Math.Max(bytes.Length, 1)); if (bytes.Length > 0) Marshal.Copy(bytes, 0, block, bytes.Length); return new Blob { Size = (uint)bytes.Length, Data = block }; }
        public void Dispose() { for (int i = blocks.Count - 1; i >= 0; i--) Marshal.FreeHGlobal(blocks[i]); blocks.Clear(); }
    }

    internal sealed class NativeSession : IWfpSession
    {
        private IntPtr engine;
        private const uint SubLayerNotFound = 0x80320007;
        private const uint FilterNotFound = 0x80320003;
        [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)] private static extern uint FwpmEngineOpen0(string server, uint authn, IntPtr identity, IntPtr session, out IntPtr handle);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmEngineClose0(IntPtr handle);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmTransactionBegin0(IntPtr handle, uint flags);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmTransactionCommit0(IntPtr handle);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmTransactionAbort0(IntPtr handle);
        [DllImport("fwpuclnt.dll")] private static extern void FwpmFreeMemory0(ref IntPtr memory);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmSubLayerGetByKey0(IntPtr handle, ref Guid key, out IntPtr layer);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmSubLayerAdd0(IntPtr handle, ref SubLayerNative layer, IntPtr security);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmSubLayerDeleteByKey0(IntPtr handle, ref Guid key);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterCreateEnumHandle0(IntPtr handle, IntPtr template, out IntPtr enumeration);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterEnum0(IntPtr handle, IntPtr enumeration, uint requested, out IntPtr entries, out uint count);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterDestroyEnumHandle0(IntPtr handle, IntPtr enumeration);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterAdd0(IntPtr handle, ref FilterNative filter, IntPtr security, out ulong id);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterGetByKey0(IntPtr handle, ref Guid key, out IntPtr filter);
        [DllImport("fwpuclnt.dll")] private static extern uint FwpmFilterDeleteByKey0(IntPtr handle, ref Guid key);
        [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)] private static extern uint FwpmGetAppIdFromFileName0(string path, out IntPtr appId);

        internal static void CheckLayouts()
        {
            if (IntPtr.Size != 8) throw new PlatformNotSupportedException("Claude network guard requires a 64-bit PowerShell process.");
            if (Marshal.SizeOf(typeof(FilterNative)) != 200 || Marshal.SizeOf(typeof(Condition)) != 40 || Marshal.SizeOf(typeof(SubLayerNative)) != 72 ||
                Marshal.OffsetOf(typeof(FilterNative), "Conditions").ToInt32() != 120 || Marshal.OffsetOf(typeof(FilterNative), "Id").ToInt32() != 176 ||
                Marshal.OffsetOf(typeof(Condition), "Data").ToInt32() != 24 || Marshal.OffsetOf(typeof(Value), "Pointer").ToInt32() != 8)
                throw new InvalidOperationException("WFP x64 native structure layout mismatch.");
        }
        internal NativeSession()
        {
            CheckLayouts();
            // Null session => ordinary non-dynamic engine. Persistent objects
            // deliberately survive helper/engine exit and BFE restart.
            uint code = FwpmEngineOpen0(null, 10, IntPtr.Zero, IntPtr.Zero, out engine);
            if (code != 0 && engine != IntPtr.Zero) { FwpmEngineClose0(engine); engine = IntPtr.Zero; }
            Check("FwpmEngineOpen0", code);
        }
        private static void Check(string operation, uint code) { if (code != 0) throw new NativeError(operation, code); }
        public void Begin(bool readOnly) { Check("FwpmTransactionBegin0", FwpmTransactionBegin0(engine, readOnly ? 1u : 0u)); }
        public void Commit() { Check("FwpmTransactionCommit0", FwpmTransactionCommit0(engine)); }
        public void Abort() { Check("FwpmTransactionAbort0", FwpmTransactionAbort0(engine)); }
        public void Dispose() { if (engine != IntPtr.Zero) { FwpmEngineClose0(engine); engine = IntPtr.Zero; } }
        private static byte[] CopyBlob(Blob blob, int maximum)
        {
            if (blob.Size > maximum || (blob.Size > 0 && blob.Data == IntPtr.Zero)) throw new InvalidOperationException("Unexpected native blob size/pointer.");
            byte[] bytes = new byte[(int)blob.Size]; if (bytes.Length > 0) Marshal.Copy(blob.Data, bytes, 0, bytes.Length); return bytes;
        }
        private static string Tag(Blob blob) { return new UTF8Encoding(false, true).GetString(CopyBlob(blob, 65536)); }
        public SubLayerRecord GetSubLayer(Guid key)
        {
            IntPtr pointer = IntPtr.Zero;
            try
            {
                uint code = FwpmSubLayerGetByKey0(engine, ref key, out pointer);
                if (code == SubLayerNotFound) return null; Check("FwpmSubLayerGetByKey0", code);
                SubLayerNative value = (SubLayerNative)Marshal.PtrToStructure(pointer, typeof(SubLayerNative));
                return new SubLayerRecord { Key = value.Key, Name = Marshal.PtrToStringUni(value.Display.Name), Tag = Tag(value.Data),
                    Flags = value.Flags, NullProvider = value.Provider == IntPtr.Zero };
            }
            finally { if (pointer != IntPtr.Zero) FwpmFreeMemory0(ref pointer); }
        }
        internal static byte[] ObjectSecurityDescriptor(string ownerSid)
        {
            string sid = new SecurityIdentifier(ownerSid).Value;
            // BFE maps GR using FWPM_GENERIC_READ; it excludes write, delete,
            // WRITE_DAC and WRITE_OWNER. Protected DACL applies only to this
            // newly created object. BA, not the logical ownerSID, owns the
            // object so the standard user does not inherit implicit WRITE_DAC.
            // https://learn.microsoft.com/windows/win32/fwp/access-control
            // https://learn.microsoft.com/windows/win32/fwp/access-right-identifiers
            RawSecurityDescriptor descriptor = new RawSecurityDescriptor("O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;" + sid + ")");
            byte[] bytes = new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes, 0); return bytes;
        }
        public void AddSubLayer(Guid key, string name, string tag, string ownerSid)
        {
            using (Arena memory = new Arena())
            {
                SubLayerNative layer = new SubLayerNative { Key = key, Display = new Display { Name = memory.Text(name) },
                    Flags = Ids.Persistent, Provider = IntPtr.Zero, Data = memory.Bytes(Encoding.UTF8.GetBytes(tag)), Weight = 0x7FFF };
                IntPtr security = memory.Bytes(ObjectSecurityDescriptor(ownerSid)).Data;
                Check("FwpmSubLayerAdd0", FwpmSubLayerAdd0(engine, ref layer, security));
            }
        }
        public byte[] AppId(string path)
        {
            IntPtr pointer = IntPtr.Zero;
            try
            {
                Check("FwpmGetAppIdFromFileName0", FwpmGetAppIdFromFileName0(path, out pointer));
                return CopyBlob((Blob)Marshal.PtrToStructure(pointer, typeof(Blob)), 65536);
            }
            finally { if (pointer != IntPtr.Zero) FwpmFreeMemory0(ref pointer); }
        }
        internal static FilterNative BuildFilter(FilterSpec spec, Arena memory)
        {
            IntPtr appBlob = memory.Structure(memory.Bytes(spec.AppId));
            IntPtr luid = memory.Allocate(8); Marshal.WriteInt64(luid, unchecked((long)spec.Luid));
            Condition[] conditions = new Condition[] {
                new Condition { Field = Ids.AppId, Match = Ids.Equal, Data = new Value { Type = Ids.ByteBlob, Pointer = appBlob } },
                new Condition { Field = spec.InterfaceField, Match = Ids.NotEqual, Data = new Value { Type = Ids.UInt64, Pointer = luid } },
                new Condition { Field = Ids.Flags, Match = Ids.FlagsNoneSet, Data = new Value { Type = Ids.UInt32, Number = Ids.Loopback } }
            };
            IntPtr array = memory.Allocate(conditions.Length * 40);
            for (int i = 0; i < conditions.Length; i++) Marshal.StructureToPtr(conditions[i], IntPtr.Add(array, i * 40), false);
            return new FilterNative { Key = spec.Key, Display = new Display { Name = memory.Text("OrangeDesk Claude guard / " + spec.OwnerSid),
                Description = memory.Text("BLOCK native Claude outside selected TAP; all users of this executable path; loopback excluded.") },
                Flags = Ids.Persistent, Provider = IntPtr.Zero, Data = memory.Bytes(Encoding.UTF8.GetBytes(spec.Tag)), Layer = spec.Layer,
                SubLayer = spec.SubLayer, Weight = new Value { Type = Ids.UInt8, Number = 15 }, ConditionCount = 3,
                Conditions = array, Action = new Action { Type = Ids.Block } };
        }
        public void AddFilter(FilterSpec spec)
        {
            using (Arena memory = new Arena())
            {
                FilterNative filter = BuildFilter(spec, memory); ulong id;
                IntPtr security = memory.Bytes(ObjectSecurityDescriptor(spec.OwnerSid)).Data;
                Check("FwpmFilterAdd0", FwpmFilterAdd0(engine, ref filter, security, out id));
            }
        }
        internal static FilterRecord DecodeFilter(FilterNative value)
        {
            FilterRecord record = new FilterRecord { Key = value.Key, SubLayer = value.SubLayer, Layer = value.Layer, Tag = Tag(value.Data) };
            StringBuilder details = new StringBuilder();
            details.Append("flags=0x").Append(value.Flags.ToString("X8", CultureInfo.InvariantCulture)).Append(" action=0x")
                .Append(value.Action.Type.ToString("X8", CultureInfo.InvariantCulture)).Append(" providerNull=").Append(value.Provider == IntPtr.Zero)
                .Append(" conditionCount=").Append(value.ConditionCount).Append(" conditionsNull=").Append(value.Conditions == IntPtr.Zero);
            bool valid = value.Action.Type == Ids.Block && value.Provider == IntPtr.Zero &&
                (value.Flags & ~Ids.Indexed) == Ids.Persistent && value.ConditionCount == 3 && value.Conditions != IntPtr.Zero;
            int apps = 0, interfaces = 0, flags = 0;
            if (value.ConditionCount > 32) throw new InvalidOperationException("Unexpected owned filter condition count.");
            for (int i = 0; i < value.ConditionCount && value.Conditions != IntPtr.Zero; i++)
            {
                Condition condition = (Condition)Marshal.PtrToStructure(IntPtr.Add(value.Conditions, i * 40), typeof(Condition));
                details.Append(" [condition ").Append(i).Append(" field=").Append(condition.Field).Append(" match=").Append(condition.Match)
                    .Append(" type=").Append(condition.Data.Type);
                if (condition.Data.Type == Ids.UInt32 || condition.Data.Type == Ids.UInt8) details.Append(" number=").Append(condition.Data.Number);
                if (condition.Data.Type == Ids.UInt64 && condition.Data.Pointer != IntPtr.Zero)
                    details.Append(" uint64=").Append(unchecked((ulong)Marshal.ReadInt64(condition.Data.Pointer)));
                details.Append(']');
                if (condition.Field == Ids.AppId && condition.Match == Ids.Equal && condition.Data.Type == Ids.ByteBlob && condition.Data.Pointer != IntPtr.Zero)
                { apps++; record.ActualAppIdHash = Ids.Hash(CopyBlob((Blob)Marshal.PtrToStructure(condition.Data.Pointer, typeof(Blob)), 65536)); }
                else if ((condition.Field == Ids.LocalInterface || condition.Field == Ids.NextInterface) && condition.Match == Ids.NotEqual && condition.Data.Type == Ids.UInt64 && condition.Data.Pointer != IntPtr.Zero)
                { interfaces++; record.InterfaceField = condition.Field; record.Luid = unchecked((ulong)Marshal.ReadInt64(condition.Data.Pointer)); }
                else if (condition.Field == Ids.Flags && condition.Match == Ids.FlagsNoneSet && condition.Data.Type == Ids.UInt32 && condition.Data.Number == Ids.Loopback) flags++;
                else valid = false;
            }
            record.ShapeValid = valid && apps == 1 && interfaces == 1 && flags == 1;
            record.ShapeDetails = details.Append(" parsedApp=").Append(apps).Append(" parsedInterface=").Append(interfaces).Append(" parsedFlags=").Append(flags).ToString();
            // Extract only the path; owner/combinations/tag hash are checked by Policy.
            int start = record.Tag.IndexOf("|app=", StringComparison.Ordinal);
            int end = start < 0 ? -1 : record.Tag.IndexOf(';', start);
            if (start >= 0 && end > start)
                try { record.AppPath = new UTF8Encoding(false, true).GetString(Convert.FromBase64String(record.Tag.Substring(start + 5, end - start - 5))); }
                catch (Exception) { record.ShapeValid = false; }
            return record;
        }
        public List<FilterRecord> Filters(Guid subLayer)
        {
            List<FilterRecord> result = new List<FilterRecord>(); IntPtr enumeration = IntPtr.Zero;
            try
            {
                Check("FwpmFilterCreateEnumHandle0", FwpmFilterCreateEnumHandle0(engine, IntPtr.Zero, out enumeration));
                while (true)
                {
                    IntPtr entries = IntPtr.Zero; uint count;
                    try
                    {
                        Check("FwpmFilterEnum0", FwpmFilterEnum0(engine, enumeration, 256, out entries, out count));
                        if (count > 256) throw new InvalidOperationException("Unexpected WFP enumeration batch size.");
                        if (count > 0 && entries == IntPtr.Zero) throw new InvalidOperationException("Missing native enumeration array.");
                        for (int i = 0; i < count; i++)
                        {
                            IntPtr pointer = Marshal.ReadIntPtr(entries, i * IntPtr.Size);
                            if (pointer == IntPtr.Zero) throw new InvalidOperationException("Missing native filter pointer.");
                            FilterNative value = (FilterNative)Marshal.PtrToStructure(pointer, typeof(FilterNative));
                            if (value.SubLayer == subLayer) result.Add(DecodeFilter(value));
                        }
                    }
                    finally { if (entries != IntPtr.Zero) FwpmFreeMemory0(ref entries); }
                    if (count < 256) break;
                }
            }
            finally { if (enumeration != IntPtr.Zero) FwpmFilterDestroyEnumHandle0(engine, enumeration); }
            return result;
        }
        public FilterRecord GetFilter(Guid key, Guid expectedSubLayer)
        {
            IntPtr pointer = IntPtr.Zero;
            try
            {
                uint code = FwpmFilterGetByKey0(engine, ref key, out pointer);
                if (code == FilterNotFound) return null;
                Check("FwpmFilterGetByKey0", code);
                if (pointer == IntPtr.Zero) throw new InvalidOperationException("Missing native filter pointer.");
                FilterNative value = (FilterNative)Marshal.PtrToStructure(pointer, typeof(FilterNative));
                // Do not decode metadata belonging to another sublayer.
                if (value.SubLayer != expectedSubLayer) return new FilterRecord { Key = value.Key, SubLayer = value.SubLayer };
                return DecodeFilter(value);
            }
            finally { if (pointer != IntPtr.Zero) FwpmFreeMemory0(ref pointer); }
        }
        public void DeleteFilter(Guid key) { Check("FwpmFilterDeleteByKey0", FwpmFilterDeleteByKey0(engine, ref key)); }
        public void DeleteSubLayer(Guid key) { Check("FwpmSubLayerDeleteByKey0", FwpmSubLayerDeleteByKey0(engine, ref key)); }
    }
}
