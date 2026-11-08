using System;
using System.IO;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using System.Net;
using System.Text;
using System.Security.Cryptography;
public static class DesktopRelay {
 // The paired SHA-256 pin identifies the broker independently of its route.
 public static string NormalizeEndpoint(string value) {
  Uri u;
  if(value==null || !Regex.IsMatch(value,@"^https://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?/?$",RegexOptions.IgnoreCase)
    || !Uri.TryCreate(value,UriKind.Absolute,out u) || u.Port<1
    || (u.HostNameType!=UriHostNameType.Dns && u.HostNameType!=UriHostNameType.IPv4 && u.HostNameType!=UriHostNameType.IPv6))
   throw new ArgumentException("INVALID_RELAY_ENDPOINT");
  return u.GetLeftPart(UriPartial.Authority);
 }
 public static string NextEndpoint(string paired,string route,string discovered,string failed) {
  var candidates=new List<string>();
  foreach(string item in new string[]{discovered,route,paired}) {
   if(String.IsNullOrEmpty(item))continue;
   string url=NormalizeEndpoint(item);
   if(!candidates.Contains(url))candidates.Add(url);
  }
  if(candidates.Count==0)throw new ArgumentException("NO_RELAY_ENDPOINT");
  int current=candidates.IndexOf(failed ?? "");
  return candidates[(current+1)%candidates.Count];
 }
 // Called only around a network exchange. A security or protocol failure always
 // wins over a nested socket error; unknown failures are not automatically retried.
 public static bool IsTransient(Exception ex) {
  bool retry=false;
  for(var e=ex;e!=null;e=e.InnerException) {
   if(e is System.Security.Authentication.AuthenticationException || e is System.Security.Cryptography.CryptographicException || e is UnauthorizedAccessException)return false;
   var we=e as WebException;
   if(we!=null) {
    switch(we.Status) {
     case WebExceptionStatus.ProtocolError:
      var response=we.Response as HttpWebResponse;
      if(response==null)return false;
      int code=(int)response.StatusCode;
      return code==408 || code==429 || code==502 || code==503 || code==504;
     case WebExceptionStatus.TrustFailure: case WebExceptionStatus.SecureChannelFailure: return false;
     case WebExceptionStatus.Timeout: case WebExceptionStatus.ConnectFailure: case WebExceptionStatus.ReceiveFailure:
     case WebExceptionStatus.SendFailure: case WebExceptionStatus.ConnectionClosed: case WebExceptionStatus.KeepAliveFailure:
     case WebExceptionStatus.NameResolutionFailure: retry=true;break;
     default:return false;
    }
   }
   var se=e as System.Net.Sockets.SocketException;
   if(se!=null) {
    switch(se.SocketErrorCode) {
     case System.Net.Sockets.SocketError.TimedOut: case System.Net.Sockets.SocketError.ConnectionReset:
     case System.Net.Sockets.SocketError.ConnectionAborted: case System.Net.Sockets.SocketError.ConnectionRefused:
     case System.Net.Sockets.SocketError.NetworkDown: case System.Net.Sockets.SocketError.NetworkUnreachable:
     case System.Net.Sockets.SocketError.HostNotFound: case System.Net.Sockets.SocketError.HostUnreachable:
     case System.Net.Sockets.SocketError.TryAgain: retry=true;break;
     default:return false;
    }
   } else if(e is IOException || e is TimeoutException)retry=true;
  }
  return retry;
 }
 public static string Exchange(string url,string token,string pin,string method,string body) {
  var req=(HttpWebRequest)WebRequest.Create(url);
  if(req.RequestUri.Scheme!="https")throw new Exception("HTTPS_REQUIRED");
  req.Proxy=null;req.Method=method;req.Timeout=method=="POST"?90000:25000;req.ReadWriteTimeout=req.Timeout;req.AllowAutoRedirect=false;
  // Avoid the .NET Framework 350ms 100-continue wait on small pinned-TLS replies.
  req.ServicePoint.Expect100Continue=false;
  req.Headers["Authorization"]="Bearer "+token;
  req.ServerCertificateValidationCallback=(sender,cert,chain,errors)=> {
   using(var sha=SHA256.Create()){return BitConverter.ToString(sha.ComputeHash(cert.GetRawCertData())).Replace("-", "").ToLowerInvariant()==pin;}
  };
  if(method=="POST"){if(body==null)throw new Exception("POST_BODY_REQUIRED");var b=Encoding.UTF8.GetBytes(body);req.ContentType="application/json; charset=utf-8";req.ContentLength=b.Length;using(var s=req.GetRequestStream()){s.Write(b,0,b.Length);}}
  using(var response=(HttpWebResponse)req.GetResponse()){if(response.StatusCode!=HttpStatusCode.OK)throw new Exception("UNEXPECTED_STATUS"); using(var reader=new StreamReader(response.GetResponseStream(),Encoding.UTF8)){return reader.ReadToEnd();}}
 }
}
