using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Collections.Generic;
using FastOrangeCombined;
class ExtraHarness {
 static string Call(int port,string authority) { using(var c=new TcpClient("127.0.0.1",port)){c.ReceiveTimeout=5000;var s=c.GetStream();byte[] request=Encoding.ASCII.GetBytes("CONNECT "+authority+" HTTP/1.1\r\n\r\n");s.Write(request,0,request.Length);var m=new MemoryStream();byte[] buf=new byte[1024];int n;while((n=s.Read(buf,0,buf.Length))>0)m.Write(buf,0,n);return Encoding.ASCII.GetString(m.ToArray());} }
 static void Check(bool value,string name){if(!value)throw new Exception(name);Console.WriteLine("PASS "+name);}
 static void Main(){var policy=new DomainPolicy("example.com","");using(var b=new BridgeServer(policy,"extrasession123")){int port=b.Start();b.Configure(-1,false);Check(Call(port,"127.0.0.1:1").Contains("503"),"Configure(-1,false) refuses direct even loopback");b.Configure(0,true);Check(Call(port,"10.20.30.40:22").Contains("503"),"index zero refuses nonloopback private direct before connecting");Check(Call(port,"example.com:443").Contains("503"),"index zero refuses direct hostname before DNS");Check(Call(port,"127.0.0.1:1").Contains("502"),"index zero enables loopback test connector only");}
 Type type=typeof(BridgeServer).Assembly.GetType("FastOrangeCombined.BridgeServer+DirectDns");MethodInfo parse=type.GetMethod("Parse",BindingFlags.Static|BindingFlags.NonPublic);List<byte> packet=new List<byte>(new byte[]{0x12,0x34,0x81,0x80,0,1,0,1,0,0,0,0,3});packet.AddRange(Encoding.ASCII.GetBytes("www"));packet.Add(7);packet.AddRange(Encoding.ASCII.GetBytes("example"));packet.Add(3);packet.AddRange(Encoding.ASCII.GetBytes("com"));packet.AddRange(new byte[]{0,0,1,0,1,0xc0,0x0c,0,1,0,1,0,0,2,0x58,0,4,127,0,0,1});byte[] bytes=packet.ToArray();object[] args=new object[]{bytes,bytes.Length,(ushort)0x1234,"www.example.com",(uint)0};IPAddress[] addresses=(IPAddress[])parse.Invoke(null,args);Check(addresses.Length==1&&addresses[0].ToString()=="127.0.0.1","compressed DNS answer parses");Check((uint)args[4]==300,"DNS TTL capped at 300");bytes[12]=0xc0;bytes[13]=0x0c;args[0]=bytes;bool rejected=false;try{parse.Invoke(null,args);}catch(TargetInvocationException x){rejected=x.InnerException is FormatException;}Check(rejected,"cyclic DNS compression pointer rejects");Console.WriteLine("ALL 7 EXTRA CHECKS PASSED");}
}
