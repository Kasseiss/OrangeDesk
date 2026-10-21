namespace OrangeDeskClaudeGuard.Tests
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Runtime.InteropServices;
    using System.Security.AccessControl;
    using System.Text;

    public static class Harness
    {
        private const string Sid = "S-1-5-21-101-202-303-1001";
        private const string OtherSid = "S-1-5-21-101-202-303-1002";
        private static int checks;
        private static void Check(bool value, string message)
        { if (!value) throw new Exception("FAIL " + message); checks++; Console.WriteLine("PASS " + message); }

        private sealed class Store
        {
            internal Dictionary<Guid, SubLayerRecord> Layers = new Dictionary<Guid, SubLayerRecord>();
            internal List<FilterRecord> Records = new List<FilterRecord>();
        }
        private sealed class FakeSession : IWfpSession
        {
            internal readonly Store Store;
            internal readonly List<string> Calls = new List<string>();
            internal bool FailCommit, CorruptReadback;
            internal bool FailAbort = false;
            internal int FailAddAt, FailDeleteAt;
            private int additions, deletions;
            private Dictionary<Guid, SubLayerRecord> oldLayers;
            private List<FilterRecord> oldRecords;
            internal FakeSession(Store store) { Store = store; }
            public void Begin(bool readOnly)
            {
                Calls.Add(readOnly ? "BeginRead" : "BeginWrite");
                oldLayers = new Dictionary<Guid, SubLayerRecord>(Store.Layers);
                oldRecords = new List<FilterRecord>(); foreach (FilterRecord record in Store.Records) oldRecords.Add(record.Clone());
            }
            public void Commit()
            { Calls.Add("Commit"); if (FailCommit) throw new NativeError("FwpmTransactionCommit0", 5); oldLayers = null; oldRecords = null; }
            public void Abort()
            {
                Calls.Add("Abort");
                if (oldLayers != null) { Store.Layers = oldLayers; Store.Records = oldRecords; }
                if (FailAbort) throw new NativeError("FwpmTransactionAbort0", 6);
            }
            public void Dispose() { Calls.Add("Close"); }
            public SubLayerRecord GetSubLayer(Guid key)
            { Calls.Add("GetSubLayer"); SubLayerRecord value; return Store.Layers.TryGetValue(key, out value) ? value : null; }
            public void AddSubLayer(Guid key, string name, string tag, string ownerSid)
            { Calls.Add("AddSubLayer"); Store.Layers.Add(key, new SubLayerRecord { Key = key, Name = name, Tag = tag, Flags = 1, NullProvider = true }); }
            public List<FilterRecord> Filters(Guid key)
            {
                Calls.Add("Enum"); List<FilterRecord> result = new List<FilterRecord>();
                foreach (FilterRecord record in Store.Records)
                    if (record.SubLayer == key) { FilterRecord copy = record.Clone(); if (CorruptReadback && additions > 0) copy.ShapeValid = false; result.Add(copy); }
                return result;
            }
            public FilterRecord GetFilter(Guid key, Guid expectedSubLayer)
            {
                Calls.Add("GetFilterByKey");
                foreach (FilterRecord record in Store.Records) if (record.Key == key) return record.Clone();
                return null;
            }
            public void AddFilter(FilterSpec spec)
            {
                Calls.Add("AddFilter"); additions++;
                if (FailAddAt == additions) throw new NativeError("FwpmFilterAdd0", 5);
                using (Arena memory = new Arena())
                {
                    FilterNative native = NativeSession.BuildFilter(spec, memory);
                    Check(native.Provider == IntPtr.Zero && native.Flags == 1 && native.Action.Type == 0x1001,
                        "built filter is persistent BLOCK without provider or PERMIT");
                    Store.Records.Add(NativeSession.DecodeFilter(native));
                }
            }
            public void DeleteFilter(Guid key)
            {
                Calls.Add("DeleteFilter"); deletions++;
                if (deletions == FailDeleteAt) throw new NativeError("FwpmFilterDeleteByKey0", 5);
                Store.Records.RemoveAll(delegate(FilterRecord record) { return record.Key == key; });
            }
            public void DeleteSubLayer(Guid key) { Calls.Add("DeleteSubLayer"); Store.Layers.Remove(key); }
            public byte[] AppId(string path)
            { Calls.Add("AppId"); return Encoding.Unicode.GetBytes("\\device\\mock" + path.ToLowerInvariant() + "\0"); }
        }

        private static string NativeFixture(string root, string folder)
        {
            string directory = Path.Combine(root, folder); Directory.CreateDirectory(directory);
            string path = Path.Combine(directory, "claude.exe"); byte[] bytes = new byte[256];
            bytes[0] = 0x4D; bytes[1] = 0x5A; bytes[0x3C] = 128;
            bytes[128] = 0x50; bytes[129] = 0x45; bytes[132] = 0x64; bytes[133] = 0x86; bytes[150] = 2;
            File.WriteAllBytes(path, bytes); return path;
        }
        private static bool Fails(System.Action action)
        { try { action(); return false; } catch (Exception) { return true; } }
        private static Snapshot Read(Store store, string sid)
        { return Policy.Read(sid, new FakeSession(store)); }
        private static void Seed(Store store, string path, ulong luid)
        { Policy.Install(Sid, new string[] { path }, luid, new FakeSession(store)); }

        public static void Run()
        {
            NativeSession.CheckLayouts();
            Check(Marshal.SizeOf(typeof(FilterNative)) == 200 && Marshal.SizeOf(typeof(Condition)) == 40 &&
                Marshal.SizeOf(typeof(SubLayerNative)) == 72 && Marshal.SizeOf(typeof(OrangeDeskClaudeGuard.Action)) == 20,
                "SDK x64 filter/condition/sublayer/action sizes match");
            Check(Marshal.OffsetOf(typeof(FilterNative), "Action").ToInt32() == 128 &&
                Marshal.OffsetOf(typeof(FilterNative), "ContextUnion").ToInt32() == 152 &&
                Marshal.OffsetOf(typeof(FilterNative), "Reserved").ToInt32() == 168 &&
                Marshal.OffsetOf(typeof(FilterNative), "EffectiveWeight").ToInt32() == 184 &&
                Marshal.OffsetOf(typeof(SubLayerNative), "Weight").ToInt32() == 64,
                "SDK union/pointer/weight offsets match");
            Check(Ids.Layer4.ToString() == "c38d57d1-05a7-4c33-904f-7fbceee60e82" &&
                Ids.Layer6.ToString() == "4a72393b-319f-44bc-84c3-ba54dcb3b6b4" &&
                Ids.LocalInterface.ToString() == "4cd62a49-59c3-4969-b7f3-bda5d32890a4" &&
                Ids.NextInterface.ToString() == "93ae8f5b-7f6f-4719-98c8-14e97429ef04" &&
                Ids.AppId.ToString() == "d78e1e87-8644-4ea5-9437-d809ecefc971" &&
                Ids.Flags.ToString() == "632ce23b-5167-435c-86d7-e903684aa80c",
                "layer and condition GUIDs match Microsoft SDK definitions");
            Check(Ids.SubLayer(Sid) == Ids.SubLayer(Sid) && Ids.SubLayer(Sid) != Ids.SubLayer(OtherSid),
                "sublayer key is stable per owner and isolated between owners");
            byte[] security = NativeSession.ObjectSecurityDescriptor(Sid);
            RawSecurityDescriptor descriptor = new RawSecurityDescriptor(security, 0);
            Check(descriptor.Owner.Value == "S-1-5-32-544" && descriptor.Group.Value == "S-1-5-32-544" &&
                (descriptor.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0 &&
                (descriptor.ControlFlags & ControlFlags.SelfRelative) != 0 && descriptor.DiscretionaryAcl.Count == 3,
                "new-object descriptor is self-relative, protected and administratively owned");
            int adminFull = 0, userRead = 0;
            foreach (GenericAce rawAce in descriptor.DiscretionaryAcl)
            {
                CommonAce ace = rawAce as CommonAce;
                Check(ace != null && ace.AceQualifier == AceQualifier.AccessAllowed && ace.AceFlags == AceFlags.None,
                    "new-object ACL contains only explicit non-inheriting allow ACEs");
                if (ace.SecurityIdentifier.Value == Sid)
                {
                    userRead++;
                    Check(ace.AccessMask == unchecked((int)0x80000000), "logical owner SID receives GENERIC_READ only, no write/delete/ownership grant");
                }
                else if (ace.SecurityIdentifier.Value == "S-1-5-18" || ace.SecurityIdentifier.Value == "S-1-5-32-544")
                { adminFull++; Check(ace.AccessMask == 0x10000000, "SYSTEM and Administrators retain GENERIC_ALL"); }
                else throw new Exception("Unexpected trustee on private WFP object.");
            }
            Check(adminFull == 2 && userRead == 1, "read-only access is limited to the requested SID, without Users/Everyone broadening");
            string root = Path.Combine(Path.GetTempPath(), "OrangeClaudeGuardTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(root);
            try
            {
                string app = NativeFixture(root, "one"), app2 = NativeFixture(root, "two");
                Store store = new Store(); FakeSession install = new FakeSession(store);
                Snapshot installed = Policy.Install(Sid, new string[] { app, app.ToUpperInvariant() }, 0x112233445566UL, install);
                Check(installed.Enabled && installed.FilterCount == 4 && installed.Apps.Length == 1 && !installed.UserScoped,
                    "one app creates exactly four verified filters and declares all-user path scope");
                Check(installed.InterfaceLuid == 0x112233445566UL && install.Calls.Contains("Commit") && !install.Calls.Contains("Abort") && install.Calls.Contains("Close"),
                    "successful install commits atomically and closes engine");
                HashSet<string> combos = new HashSet<string>();
                foreach (FilterRecord record in store.Records)
                    combos.Add(record.Layer.ToString() + "|" + record.InterfaceField.ToString());
                Check(combos.Count == 4 && Read(store, Sid).Enabled,
                    "both address families and local/next-hop conditions persist after helper close");
                Check(installed.FilterKeys.Length == 4, "installed snapshot exposes all four native filter keys");
                FakeSession knownRead = new FakeSession(store);
                Snapshot known = Policy.ReadKnown(Sid, installed.FilterKeys, knownRead);
                Check(known.Enabled && known.FilterCount == 4 && !knownRead.Calls.Contains("Enum") &&
                    !knownRead.Calls.Contains("BeginRead") && !knownRead.Calls.Contains("BeginWrite") && knownRead.Calls.Contains("GetFilterByKey"),
                    "ReadKnown verifies objects by key without global enumeration or transaction permission");
                string[] duplicatedKeys = new string[5]; Array.Copy(installed.FilterKeys, duplicatedKeys, 4); duplicatedKeys[4] = installed.FilterKeys[0].ToUpperInvariant();
                Check(Policy.ReadKnown(Sid, duplicatedKeys, new FakeSession(store)).FilterKeys.Length == 4,
                    "known GUID inputs are normalized and deduplicated");
                string[] oneMissing = new string[5]; Array.Copy(installed.FilterKeys, oneMissing, 4); oneMissing[4] = Guid.NewGuid().ToString("D");
                Snapshot missingKnown = Policy.ReadKnown(Sid, oneMissing, new FakeSession(store));
                Check(!missingKnown.Enabled && missingKnown.FilterCount == 4 && missingKnown.FilterKeys.Length == 5,
                    "any missing expected key prevents enabled and stays in expected key snapshot");
                Check(!Policy.ReadKnown(Sid, new string[0], new FakeSession(store)).Enabled,
                    "empty known key set cannot claim enabled");
                Check(Fails(delegate { Policy.ReadKnown(Sid, new string[] { Guid.Empty.ToString("D") }, new FakeSession(store)); }) &&
                    Fails(delegate { Policy.ReadKnown(Sid, new string[] { "not-a-guid" }, new FakeSession(store)); }) &&
                    Fails(delegate { Policy.ReadKnown(Sid, new string[129], new FakeSession(store)); }),
                    "invalid, zero and oversized known key sets reject before querying");

                using (Arena memory = new Arena())
                {
                    FilterSpec spec = new FilterSpec { Key = Guid.NewGuid(), SubLayer = Ids.SubLayer(Sid), Layer = Ids.Layer4,
                        InterfaceField = Ids.LocalInterface, OwnerSid = Sid, AppPath = app,
                        AppId = Encoding.Unicode.GetBytes("mock-app-id"), Luid = UInt64.MaxValue - 5 };
                    FilterNative native = NativeSession.BuildFilter(spec, memory);
                    Condition appCondition = (Condition)Marshal.PtrToStructure(native.Conditions, typeof(Condition));
                    Condition interfaceCondition = (Condition)Marshal.PtrToStructure(IntPtr.Add(native.Conditions, 40), typeof(Condition));
                    Condition flagsCondition = (Condition)Marshal.PtrToStructure(IntPtr.Add(native.Conditions, 80), typeof(Condition));
                    Check(appCondition.Field == Ids.AppId && appCondition.Match == 0 && appCondition.Data.Type == 12 &&
                        interfaceCondition.Match == 10 && interfaceCondition.Data.Type == 4 && unchecked((ulong)Marshal.ReadInt64(interfaceCondition.Data.Pointer)) == spec.Luid &&
                        flagsCondition.Field == Ids.Flags && flagsCondition.Match == 8 && flagsCondition.Data.Type == 3 && flagsCondition.Data.Number == 1,
                        "native conditions encode app equality, 64-bit LUID inequality and loopback exclusion");
                    Check(NativeSession.DecodeFilter(native).ShapeValid, "native construction round-trips through strict readback");
                    native.Flags = Ids.Persistent | Ids.Indexed;
                    Check(NativeSession.DecodeFilter(native).ShapeValid, "BFE INDEXED 0x40 normalization preserves valid persistent BLOCK");
                    native.Flags |= 0x20;
                    Check(!NativeSession.DecodeFilter(native).ShapeValid, "INDEXED does not conceal DISABLED 0x20");
                    native.Flags = Ids.Persistent | Ids.Indexed | 0x08;
                    Check(!NativeSession.DecodeFilter(native).ShapeValid, "INDEXED does not conceal other filter flags");
                    native.Flags = Ids.Indexed;
                    Check(!NativeSession.DecodeFilter(native).ShapeValid, "INDEXED without PERSISTENT is rejected");
                    native.Flags = Ids.Persistent;
                    native.Action = new OrangeDeskClaudeGuard.Action { Type = 0x1002 };
                    Check(!NativeSession.DecodeFilter(native).ShapeValid, "readback rejects PERMIT action");
                }

                FakeSession failed = new FakeSession(store) { FailAddAt = 3 };
                bool nativeCode = false;
                try { Policy.Install(Sid, new string[] { app2 }, 99, failed); }
                catch (NativeError error) { nativeCode = error.NativeCode == 5 && error.Message.Contains("0x00000005"); }
                Snapshot afterFailure = Read(store, Sid);
                Check(nativeCode && failed.Calls.Contains("Abort") && failed.Calls.Contains("Close") &&
                    afterFailure.Enabled && afterFailure.InterfaceLuid == 0x112233445566UL && afterFailure.Apps[0] == app,
                    "partial native add failure aborts and preserves old policy with native code");
                FakeSession commitFailure = new FakeSession(store) { FailCommit = true };
                Check(Fails(delegate { Policy.Install(Sid, new string[] { app2 }, 88, commitFailure); }) &&
                    commitFailure.Calls.Contains("Abort") && Read(store, Sid).InterfaceLuid == 0x112233445566UL,
                    "commit failure aborts replacement");
                FakeSession corrupt = new FakeSession(store) { CorruptReadback = true };
                Check(Fails(delegate { Policy.Install(Sid, new string[] { app2 }, 77, corrupt); }) &&
                    corrupt.Calls.Contains("Abort") && Read(store, Sid).Enabled,
                    "incomplete native readback cannot commit");

                Snapshot replaced = Policy.Install(Sid, new string[] { app, app2 }, 1234, new FakeSession(store));
                Check(replaced.Enabled && replaced.FilterCount == 8 && replaced.Apps.Length == 2 && replaced.InterfaceLuid == 1234,
                    "atomic replacement updates both families for every selected app");
                FakeSession readOnly = new FakeSession(store); Policy.Read(Sid, readOnly);
                Check(!readOnly.Calls.Contains("BeginRead") && !readOnly.Calls.Contains("BeginWrite") && !readOnly.Calls.Contains("AddFilter") && !readOnly.Calls.Contains("DeleteFilter"),
                    "Read uses object queries without privileged transaction rights or policy mutation");
                FakeSession abortFailure = new FakeSession(store) { FailAddAt = 1, FailAbort = true };
                bool bothErrors = false;
                try { Policy.Install(Sid, new string[] { app }, 55, abortFailure); }
                catch (AggregateException error)
                {
                    bothErrors = error.InnerExceptions.Count == 2 && ((NativeError)error.InnerExceptions[0]).NativeCode == 5 &&
                        ((NativeError)error.InnerExceptions[1]).NativeCode == 6;
                }
                Check(bothErrors && abortFailure.Calls.Contains("Close"), "rollback failure preserves both native errors and still closes engine");
                FilterRecord removed = store.Records[0]; store.Records.RemoveAt(0);
                Check(!Read(store, Sid).Enabled && Read(store, Sid).FilterCount == 7, "partial policy is never reported enabled");
                store.Records.Add(removed);
                store.Records[0].ShapeValid = false;
                Check(!Read(store, Sid).Enabled, "disabled or malformed native conditions cannot claim enabled");
                store.Records[0].ShapeValid = true;

                Policy.Install(OtherSid, new string[] { app }, 456, new FakeSession(store));
                int otherCount = Read(store, OtherSid).FilterCount;
                Check(Fails(delegate { Policy.ReadKnown(Sid, Read(store, OtherSid).FilterKeys, new FakeSession(store)); }),
                    "known keys in another owner's sublayer are rejected");
                FakeSession removeFailure = new FakeSession(store) { FailDeleteAt = 2 };
                Check(Fails(delegate { Policy.Remove(Sid, removeFailure); }) && removeFailure.Calls.Contains("Abort") && Read(store, Sid).FilterCount == 8,
                    "partial removal failure atomically restores owned filters");
                Snapshot off = Policy.Remove(Sid, new FakeSession(store));
                Check(!off.Enabled && off.FilterCount == 0 && Read(store, OtherSid).FilterCount == otherCount,
                    "Remove deletes only matching owner sublayer and preserves another owner");

                Seed(store, app, 789);
                Guid ownKey = Ids.SubLayer(Sid); SubLayerRecord layer = store.Layers[ownKey];
                layer.Tag = Ids.SubTag(OtherSid);
                FakeSession collision = new FakeSession(store);
                Check(Fails(delegate { Policy.Remove(Sid, collision); }) && !collision.Calls.Contains("DeleteFilter") && collision.Calls.Contains("Abort"),
                    "foreign sublayer label prevents all deletions");
                layer.Tag = Ids.SubTag(Sid);
                FilterRecord foreign = null;
                foreach (FilterRecord record in store.Records) if (record.SubLayer == ownKey) { foreign = record; break; }
                string oldTag = foreign.Tag; foreign.Tag = Ids.OwnerPrefix(OtherSid) + "app=foreign";
                FakeSession foreignFilter = new FakeSession(store);
                Check(Fails(delegate { Policy.Install(Sid, new string[] { app }, 42, foreignFilter); }) &&
                    !foreignFilter.Calls.Contains("DeleteFilter") && foreignFilter.Calls.Contains("Abort"),
                    "foreign filter label in owned sublayer aborts without deleting it");
                foreign.Tag = oldTag;

                FakeSession invalid = new FakeSession(new Store());
                Check(Fails(delegate { Policy.Install(Sid, new string[] { app }, 0, invalid); }) && !invalid.Calls.Contains("BeginWrite"),
                    "zero VPN LUID is rejected before transaction");
                FakeSession badSid = new FakeSession(new Store());
                Check(Fails(delegate { Policy.Install("not-a-sid", new string[] { app }, 1, badSid); }) && !badSid.Calls.Contains("AppId"),
                    "invalid owner SID is rejected before native identity resolution");
                string wrapper = Path.Combine(root, "claude.cmd"); File.WriteAllText(wrapper, "echo mock");
                Check(Fails(delegate { Policy.Install(Sid, new string[] { wrapper }, 1, new FakeSession(new Store())); }),
                    "script wrappers cannot be treated as native Claude executables");
                Console.WriteLine("ALL " + checks + " CLAUDE WFP MOCK/LAYOUT CHECKS PASSED; NO WFP ENGINE WAS OPENED.");
            }
            finally
            {
                string resolved = Path.GetFullPath(root);
                string boundary = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                if (resolved.StartsWith(boundary, StringComparison.OrdinalIgnoreCase) && Path.GetFileName(resolved).StartsWith("OrangeClaudeGuardTests-", StringComparison.Ordinal))
                    Directory.Delete(resolved, true);
            }
        }
    }
}
