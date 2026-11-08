using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using System.Collections.Generic;
public static class LanPilotDiscovery {
 static string Hex(byte[] b){return BitConverter.ToString(b).Replace("-","").ToLowerInvariant();}
 static string Mac(string token,string text){using(var h=new HMACSHA256(Encoding.UTF8.GetBytes(token)))return Hex(h.ComputeHash(Encoding.UTF8.GetBytes(text)));}
 static bool Equal(string a,string b){if(a==null||b==null||a.Length!=b.Length)return false;int n=0;for(int i=0;i<a.Length;i++)n|=a[i]^b[i];return n==0;}
 public static string Find(string token){
  string nonce=Guid.NewGuid().ToString("N"),id;using(var h=SHA256.Create())id=Hex(h.ComputeHash(Encoding.UTF8.GetBytes(token))).Substring(0,16);
  var json=new JavaScriptSerializer();var req=new Dictionary<string,object>{{"v",1},{"device",id},{"nonce",nonce},{"mac",Mac(token,"discover:"+nonce)}};
  using(var udp=new UdpClient()){
   udp.EnableBroadcast=true;udp.Client.ReceiveTimeout=1500;
   byte[] bytes=Encoding.UTF8.GetBytes(json.Serialize(req));udp.Send(bytes,bytes.Length,new IPEndPoint(IPAddress.Broadcast,18770));
   var deadline=DateTime.UtcNow.AddSeconds(2);
   while(DateTime.UtcNow<deadline){
    try{
     IPEndPoint peer=new IPEndPoint(IPAddress.Any,0);byte[] raw=udp.Receive(ref peer);if(raw.Length>4096)continue;
     var r=json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(raw));
     string url=r["url"] as string,n=r["nonce"] as string,mac=r["mac"] as string;
     Uri u;if(n!=nonce||!Uri.TryCreate(url,UriKind.Absolute,out u)||u.Scheme!="https"||u.Host!=peer.Address.ToString()||u.Port<1024||u.Port>65535||u.AbsolutePath!="/"||u.Query!=""||u.UserInfo!="")continue;
     if(Equal(mac,Mac(token,"endpoint:"+nonce+":"+url)))return url;
    }catch(SocketException){break;}catch{continue;}
   }
  }
  return null;
 }
}
