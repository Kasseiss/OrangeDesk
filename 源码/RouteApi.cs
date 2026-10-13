using System;
using System.Collections.Generic;
using System.Net;
using System.Runtime.InteropServices;

namespace FastOrangeCombined {
    [StructLayout(LayoutKind.Explicit, Size = 28)]
    public struct SockAddrInet {
        [FieldOffset(0)] public ushort Family;
        [FieldOffset(2)] public ushort Port;
        [FieldOffset(4)] public uint Address;
        [FieldOffset(8)] public uint Extra1;
        [FieldOffset(12)] public uint Extra2;
        [FieldOffset(16)] public uint Extra3;
        [FieldOffset(20)] public uint Extra4;
        [FieldOffset(24)] public uint Scope;
    }
    [StructLayout(LayoutKind.Explicit, Size = 32)]
    public struct AddressPrefix {
        [FieldOffset(0)] public SockAddrInet Address;
        [FieldOffset(28)] public byte Length;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct RouteRow {
        public ulong Luid;
        public uint Index;
        public AddressPrefix Prefix;
        public SockAddrInet NextHop;
        public byte SitePrefixLength;
        public uint ValidLifetime;
        public uint PreferredLifetime;
        public uint Metric;
        public uint Protocol;
        public byte Loopback;
        public byte AutoconfigureAddress;
        public byte Publish;
        public byte Immortal;
        public uint Age;
        public uint Origin;
    }
    public class RouteSpec {
        public string Prefix;
        public uint Index;
        public ulong Luid;
        public string Gateway;
        public uint Metric;
        public uint Protocol;
        public uint ValidLifetime;
        public byte Immortal;
        public bool CreationLeaseInvalid;
        public string Key { get { return Prefix + "|" + Index + "|" + Luid + "|" + Gateway + "|" + Metric + "|" + Protocol; } }
    }
    public static class RouteApi {
        [DllImport("iphlpapi.dll")] private static extern uint GetIpForwardTable2(ushort family, out IntPtr table);
        [DllImport("iphlpapi.dll")] private static extern uint GetIpForwardEntry2(ref RouteRow row);
        [DllImport("iphlpapi.dll")] private static extern void FreeMibTable(IntPtr table);
        [DllImport("iphlpapi.dll")] private static extern void InitializeIpForwardEntry(ref RouteRow row);
        [DllImport("iphlpapi.dll")] private static extern uint CreateIpForwardEntry2(ref RouteRow row);
        [DllImport("iphlpapi.dll")] private static extern uint SetIpForwardEntry2(ref RouteRow row);
        [DllImport("iphlpapi.dll")] private static extern uint DeleteIpForwardEntry2(ref RouteRow row);
        public static void CheckLayout() {
            if (Marshal.SizeOf(typeof(RouteRow)) != 104 || Marshal.OffsetOf(typeof(RouteRow), "ValidLifetime").ToInt32() != 76 ||
                Marshal.OffsetOf(typeof(RouteRow), "NextHop").ToInt32() != 44)
                throw new InvalidOperationException("Unexpected Windows route structure layout.");
        }
        private static SockAddrInet Address(string text) {
            byte[] bytes = IPAddress.Parse(text).GetAddressBytes();
            if (bytes.Length != 4) throw new ArgumentException("IPv4 required.");
            SockAddrInet address = new SockAddrInet();
            address.Family = 2;
            address.Address = BitConverter.ToUInt32(bytes, 0);
            return address;
        }
        private static string Text(SockAddrInet address) { return new IPAddress(BitConverter.GetBytes(address.Address)).ToString(); }
        private static RouteSpec Spec(RouteRow row) {
            return new RouteSpec { Prefix = Text(row.Prefix.Address) + "/" + row.Prefix.Length, Index = row.Index,
                Luid = row.Luid, Gateway = Text(row.NextHop), Metric = row.Metric, Protocol = row.Protocol,
                ValidLifetime = row.ValidLifetime, Immortal = row.Immortal };
        }
        private static RouteRow Make(RouteSpec spec, uint lease) {
            RouteRow row = new RouteRow();
            InitializeIpForwardEntry(ref row);
            string[] parts = spec.Prefix.Split('/');
            row.Luid = spec.Luid; row.Index = spec.Index;
            row.Prefix.Address = Address(parts[0]); row.Prefix.Length = Byte.Parse(parts[1]);
            row.NextHop = Address(spec.Gateway); row.SitePrefixLength = 0;
            row.Metric = spec.Metric; row.Protocol = 3;
            row.ValidLifetime = lease; row.PreferredLifetime = lease;
            row.Immortal = 0; row.Publish = 0; row.Loopback = 0; row.AutoconfigureAddress = 0;
            return row;
        }
        private static RouteRow[] Rows() {
            CheckLayout();
            IntPtr table;
            uint code = GetIpForwardTable2(2, out table);
            if (code != 0) throw new InvalidOperationException("GetIpForwardTable2: " + code);
            try {
                int count = Marshal.ReadInt32(table);
                if (count < 0 || count > 100000) throw new InvalidOperationException("Unexpected route count.");
                RouteRow[] rows = new RouteRow[count];
                for (int i = 0; i < count; i++)
                    rows[i] = (RouteRow)Marshal.PtrToStructure(IntPtr.Add(table, 8 + i * 104), typeof(RouteRow));
                return rows;
            } finally { FreeMibTable(table); }
        }
        public static RouteSpec[] Read() {
            List<RouteSpec> result = new List<RouteSpec>();
            foreach (RouteRow row in Rows()) if (row.Prefix.Address.Family == 2) result.Add(Spec(row));
            return result.ToArray();
        }
        public const uint OwnershipChanged = 1306;
        public static uint Create(RouteSpec spec, uint lease) {
            RouteRow row = Make(spec, lease);
            uint code = CreateIpForwardEntry2(ref row);
            if (code != 0) return code;
            RouteRow check = Make(spec, lease);
            if (GetIpForwardEntry2(ref check) == 0 && Spec(check).Key == spec.Key &&
                (check.Immortal != 0 || check.ValidLifetime > lease)) {
                // Creation succeeded: the supervisor must retain ownership even if later cleanup fails.
                spec.CreationLeaseInvalid = true;
            }
            return 0;
        }
        private static uint FreshOwned(RouteSpec expected, out RouteRow row) {
            row = Make(expected, 0);
            uint code = GetIpForwardEntry2(ref row);
            if (code != 0) return code;
            // Metric/protocol are not native delete selectors. Check them just before touching the row.
            if (Spec(row).Key != expected.Key || (!expected.CreationLeaseInvalid && (row.Immortal != 0 || row.ValidLifetime > 90)))
                return OwnershipChanged;
            return 0;
        }
        public static uint CheckOwnership(RouteSpec expected) {
            RouteRow row;
            return FreshOwned(expected, out row);
        }
        public static RouteRow Preview(RouteSpec spec, uint lease) { return Make(spec, lease); }
        public static uint Renew(RouteSpec live, uint lease) {
            RouteRow row;
            uint code = FreshOwned(live, out row);
            if (code != 0) return code;
            row.ValidLifetime = lease; row.PreferredLifetime = lease;
            return SetIpForwardEntry2(ref row);
        }
        public static uint Remove(RouteSpec live) {
            RouteRow row;
            uint code = FreshOwned(live, out row);
            return code == 0 ? DeleteIpForwardEntry2(ref row) : code;
        }
    }
}
