using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
using Accessibility;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using System.Security.Cryptography;

public sealed class EditTarget {
 public string Identity;
 public string State;
 public int[] Region;
}
public static class RemoteDesktop {
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint SetThreadExecutionState(uint flags);
 public static uint KeepAwake(bool enabled) {
  uint previous=SetThreadExecutionState(enabled ? 0x80000003u : 0x80000000u);
  if(enabled && previous==0)throw new Exception("KEEP_AWAKE_REQUEST_FAILED");
  return previous;
 }
 [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd,uint id,ref Guid iid,[MarshalAs(UnmanagedType.Interface)] out IAccessible obj);
 // A browser exposes logical focus beneath its single native HWND. Bind this
 // identity even when the element is not eligible for scoped text comparison.
 public static string AutomationFocusIdentity() {
  try {
   var e=AutomationElement.FocusedElement;
   if(e==null)return null;
   return e.Current.ProcessId+":"+String.Join(",",e.GetRuntimeId());
  }catch{return null;}
 }
 static string Digest(string value) {
  using(var hash=SHA256.Create())return Convert.ToBase64String(hash.ComputeHash(Encoding.UTF8.GetBytes(value)));
 }
 public static EditTarget FocusedEditTarget() {
  try {
   var e=AutomationElement.FocusedElement;
   if(e==null)return null;
   var c=e.Current;
   uint pid;GetWindowThreadProcessId(GetForegroundWindow(),out pid);
   if(c.ProcessId!=pid || c.ControlType!=ControlType.Edit || !c.HasKeyboardFocus || !c.IsEnabled || c.IsOffscreen || c.IsPassword)return null;
   object vp,tp;
   if(!e.TryGetCurrentPattern(ValuePattern.Pattern,out vp)||!e.TryGetCurrentPattern(TextPattern.Pattern,out tp))return null;
   var value=(ValuePattern)vp;
   if(value.Current.IsReadOnly)return null;
   string text=value.Current.Value;
   if(text==null||text.Length>200000)return null;
   var pattern=(TextPattern)tp;var selection=pattern.GetSelection();
   if(selection==null||selection.Length!=1)return null;
   var doc=pattern.DocumentRange;
   var prefix=doc.Clone();prefix.MoveEndpointByRange(TextPatternRangeEndpoint.End,selection[0],TextPatternRangeEndpoint.Start);
   var through=doc.Clone();through.MoveEndpointByRange(TextPatternRangeEndpoint.End,selection[0],TextPatternRangeEndpoint.End);
   string before=prefix.GetText(200001),end=through.GetText(200001);
   if(before.Length>200000||end.Length>200000)return null;
   var r=c.BoundingRectangle;var b=Bounds();var w=ForegroundBounds();
   int x=(int)Math.Floor(r.Left)-b[0],y=(int)Math.Floor(r.Top)-b[1];
   int right=(int)Math.Ceiling(r.Right)-b[0],bottom=(int)Math.Ceiling(r.Bottom)-b[1];
   if(r.IsEmpty||right<=x||bottom<=y||x<w[0]||y<w[1]||right>w[0]+w[2]||bottom>w[1]+w[3])return null;
   // Do not scope an occluded target: its center must hit the same edit element.
   var hit=AutomationElement.FromPoint(new System.Windows.Point((r.Left+r.Right)/2,(r.Top+r.Bottom)/2));
   if(hit==null||!Automation.Compare(e,hit))return null;
   string identity=c.ProcessId+":"+String.Join(",",e.GetRuntimeId());
   string state=identity+"|"+r.ToString(System.Globalization.CultureInfo.InvariantCulture)+"|"+c.Name+"|"+c.AutomationId+"|"+Digest(text)+"|"+Digest(before)+"|"+Digest(end);
   // Include a small exact-pixel context margin; no caller-supplied rectangle.
   int left=Math.Max(w[0],x-24),top=Math.Max(w[1],y-24);
   right=Math.Min(w[0]+w[2],right+24);bottom=Math.Min(w[1]+w[3],bottom+24);
   return new EditTarget{Identity=identity,State=state,Region=new[]{left,top,right-left,bottom-top}};
  }catch{return null;} // Unsupported/incomplete accessibility keeps strict guard.
 }
 static int[] AccessibleCaretBounds() {
  IAccessible a=null;
  try {
   Guid iid=new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
   if(AccessibleObjectFromWindow(GetForegroundWindow(),0xFFFFFFF8,ref iid,out a)!=0||a==null)return null;
   int role=Convert.ToInt32(a.get_accRole(0)),state=Convert.ToInt32(a.get_accState(0));
   if(role!=7 || (state & (0x8000|0x10000))!=0)return null; // caret; visible, onscreen
   int x,y,width,height;a.accLocation(out x,out y,out width,out height,0);
   if(width<1||width>4||height<1||height>124)return null;
   var e=FocusedEditTarget();if(e==null)return null;
   var b=Bounds();x-=b[0];y-=b[1];
   var r=e.Region;
   if(x<r[0]+24||y<r[1]+24||x+width>r[0]+r[2]-24||y+height>r[1]+r[3]-24)return null;
   // Chromium at 150% reported x=302 while the rendered caret occupied
   // x=300..301. Allow only this 2px physical rasterization fringe.
   return new[]{x-2,y-2,width+4,height+4};
  }catch{return null;}
  finally{if(a!=null&&Marshal.IsComObject(a))Marshal.ReleaseComObject(a);}
 }

 [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] static extern int GetSystemMetrics(int n);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] static extern IntPtr OpenInputDesktop(uint f, bool inherit, uint access);
 [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint tid);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h, int n, StringBuilder s, int len, out int needed);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);
 [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] a, int size);
 [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO info);
 [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd,out RECT rect);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint process);
 [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread,ref GUIINFO info);
 [DllImport("user32.dll")] static extern int MapWindowPoints(IntPtr from,IntPtr to,ref RECT rect,uint count);
 [DllImport("user32.dll")] static extern IntPtr GetWindowDpiAwarenessContext(IntPtr hwnd);
 [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
 [DllImport("user32.dll")] static extern bool LogicalToPhysicalPointForPerMonitorDPI(IntPtr hwnd,ref POINT point);
 [StructLayout(LayoutKind.Sequential)] struct POINT {public int x,y;}
 [StructLayout(LayoutKind.Sequential)] struct RECT {public int left,top,right,bottom;}
 [StructLayout(LayoutKind.Sequential)] struct GUIINFO {public uint size,flags;public IntPtr active,focus,capture,menuOwner,moveSize,caret;public RECT caretRect;}
 [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO {public uint cbSize; public uint dwTime;}
 [StructLayout(LayoutKind.Sequential)] struct INPUT {public uint type; public UNION u;}
 [StructLayout(LayoutKind.Explicit)] struct UNION {[FieldOffset(0)] public MOUSE mouse; [FieldOffset(0)] public KEY key;}
 [StructLayout(LayoutKind.Sequential)] struct MOUSE {public int x,y; public uint data,flags,time; public UIntPtr extra;}
 [StructLayout(LayoutKind.Sequential)] struct KEY {public ushort vk,scan; public uint flags,time; public UIntPtr extra;}
 [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access,bool inherit,uint id);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
 [DllImport("advapi32.dll")] static extern bool GetTokenInformation(IntPtr token,int info,IntPtr buffer,int length,out int needed);
 [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid,uint n);
 [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
 [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
 public static int IntegrityLevel(uint id) {
  IntPtr process=OpenProcess(0x1000,false,id),token=IntPtr.Zero,buffer=IntPtr.Zero;
  try {if(process==IntPtr.Zero||!OpenProcessToken(process,8,out token))throw new Exception("INPUT_INTEGRITY_UNKNOWN");
   int needed;GetTokenInformation(token,25,IntPtr.Zero,0,out needed);if(needed<1)throw new Exception("INPUT_INTEGRITY_UNKNOWN");
   buffer=Marshal.AllocHGlobal(needed);if(!GetTokenInformation(token,25,buffer,needed,out needed))throw new Exception("INPUT_INTEGRITY_UNKNOWN");
   IntPtr sid=Marshal.ReadIntPtr(buffer);return Marshal.ReadInt32(GetSidSubAuthority(sid,(uint)(Marshal.ReadByte(GetSidSubAuthorityCount(sid))-1)));
  }finally{if(buffer!=IntPtr.Zero)Marshal.FreeHGlobal(buffer);if(token!=IntPtr.Zero)CloseHandle(token);if(process!=IntPtr.Zero)CloseHandle(process);}
 }
 public static int[] InputIntegrity() {uint id;GetWindowThreadProcessId(GetForegroundWindow(),out id);return new[]{IntegrityLevel((uint)System.Diagnostics.Process.GetCurrentProcess().Id),IntegrityLevel(id)};}
 public static void CheckInputAccess() {var levels=InputIntegrity();if(levels[1]>levels[0])throw new Exception("TARGET_HIGHER_INTEGRITY_INPUT_BLOCKED");}
 public static int[] CursorPosition() {POINT p;if(!GetCursorPos(out p))throw new Exception("CURSOR_POSITION_UNKNOWN");return new[]{p.x,p.y};}
 public static void Init() {SetProcessDPIAware();}
 static string Name(IntPtr h) {int n; var s=new StringBuilder(256); if(!GetUserObjectInformation(h,2,s,512,out n)) throw new Exception("DESKTOP_UNAVAILABLE"); return s.ToString();}
 public static void Check() {
   if(System.Diagnostics.Process.GetCurrentProcess().SessionId==0) throw new Exception("NOT_INTERACTIVE_SESSION");
   IntPtr h=OpenInputDesktop(0,false,1);
   if(h==IntPtr.Zero) throw new Exception("LOCKED_OR_SECURE_DESKTOP");
   try {if(Name(h)!="Default" || Name(GetThreadDesktop(GetCurrentThreadId()))!="Default") throw new Exception("LOCKED_OR_SECURE_DESKTOP");}
   finally {CloseDesktop(h);}
 }
 public static uint LastInput() {var i=new LASTINPUTINFO(); i.cbSize=(uint)Marshal.SizeOf(i); if(!GetLastInputInfo(ref i)) throw new Exception("INPUT_STATE_UNKNOWN"); return i.dwTime;}
 public static int[] Bounds() {return new int[]{GetSystemMetrics(76),GetSystemMetrics(77),GetSystemMetrics(78),GetSystemMetrics(79)};}
 public static string Title() {var s=new StringBuilder(512); GetWindowText(GetForegroundWindow(),s,512); return s.ToString();}
 // Bind keyboard actions to the focused native control as well as foreground HWND.
 public static long FocusIdentity() {
  uint process;uint thread=GetWindowThreadProcessId(GetForegroundWindow(),out process);
  var info=new GUIINFO();info.size=(uint)Marshal.SizeOf(info);
  if(thread==0 || !GetGUIThreadInfo(thread,ref info))throw new Exception("FOCUS_STATE_UNKNOWN");
  return info.focus.ToInt64();
 }
 public static int[] ForegroundBounds() {
  RECT r;if(!GetWindowRect(GetForegroundWindow(),out r))throw new Exception("WINDOW_BOUNDS_UNKNOWN");
  var b=Bounds();int x=Math.Max(0,r.left-b[0]),y=Math.Max(0,r.top-b[1]);
  int right=Math.Min(b[2],r.right-b[0]),bottom=Math.Min(b[3],r.bottom-b[1]);
  if(right<=x||bottom<=y)throw new Exception("WINDOW_NOT_VISIBLE");
  return new[]{x,y,right-x,bottom-y};
 }
 // Only an OS-reported narrow caret can be excluded; unknown/custom carets
 // retain strict comparison. Its position must remain unchanged across frames.
 public static int[] CaretBounds() {
  var native=NativeCaretBounds();return native??AccessibleCaretBounds();
 }
 static int[] NativeCaretBounds() {
  var info=new GUIINFO();info.size=(uint)Marshal.SizeOf(info);
  if(!GetGUIThreadInfo(0,ref info)||info.caret==IntPtr.Zero||info.caret!=info.focus)return null;
  var context=GetWindowDpiAwarenessContext(info.caret);
  if(context==IntPtr.Zero)return null;
  var previous=SetThreadDpiAwarenessContext(context);
  if(previous==IntPtr.Zero)return null;
  var r=info.caretRect;
  try {
   // rcCaret is in the owner's logical units, not the capturing thread's DPI.
   MapWindowPoints(info.caret,IntPtr.Zero,ref r,2);
   var a=new POINT{x=r.left,y=r.top};var z=new POINT{x=r.right,y=r.bottom};
   if(!LogicalToPhysicalPointForPerMonitorDPI(info.caret,ref a)||!LogicalToPhysicalPointForPerMonitorDPI(info.caret,ref z))return null;
   r=new RECT{left=a.x,top=a.y,right=z.x,bottom=z.y};
  }finally{SetThreadDpiAwarenessContext(previous);}
  // Fractional DPI rasterization extends the caret by one physical pixel.
  // Live 150% test: logical 1px caret changed exactly a 3px-wide pixel region.
  r.left--;r.top--;r.right++;r.bottom++;
  int width=r.right-r.left,height=r.bottom-r.top;
  if(width<1||width>8||height<1||height>128)return null;
  var b=Bounds();var w=ForegroundBounds();
  int x=r.left-b[0],y=r.top-b[1];
  if(x<w[0]||y<w[1]||x+width>w[0]+w[2]||y+height>w[1]+w[3])return null;
  return new[]{x,y,width,height};
 }
 // Exact pixel comparison within the action target. No fuzzy image threshold.
 // Keyboard/text compare the entire foreground window; clicks compare their
 // 160px target neighborhood. Other windows' clocks/animations are irrelevant.
 public static bool RegionUnchanged(byte[] before,byte[] after,int[] region) {
  return RegionUnchanged(before,after,region,null);
 }
 public static bool RegionUnchanged(byte[] before,byte[] after,int[] region,int[] caret) {
  if(region==null||region.Length!=4||region[2]<=0||region[3]<=0)throw new Exception("INVALID_GUARD_REGION");
  if(caret!=null&&(caret.Length!=4||caret[2]<1||caret[2]>8||caret[3]<1||caret[3]>128))throw new Exception("INVALID_CARET_REGION");
  using(var aStream=new MemoryStream(before))using(var bStream=new MemoryStream(after))
  using(var a=new Bitmap(aStream))using(var b=new Bitmap(bStream)){
   if(a.Size!=b.Size)return false;
   var bounds=new Rectangle(0,0,a.Width,a.Height);
   var rect=Rectangle.Intersect(bounds,new Rectangle(region[0],region[1],region[2],region[3]));
   if(rect.Width<=0||rect.Height<=0)throw new Exception("GUARD_REGION_OUTSIDE_SCREEN");
   var ad=a.LockBits(rect,ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
   try {
    var bd=b.LockBits(rect,ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
    try {
     var ar=new byte[rect.Width*4];var br=new byte[ar.Length];
     for(int y=0;y<rect.Height;y++){
      Marshal.Copy(IntPtr.Add(ad.Scan0,y*ad.Stride),ar,0,ar.Length);
      Marshal.Copy(IntPtr.Add(bd.Scan0,y*bd.Stride),br,0,br.Length);
      for(int x=0;x<ar.Length;x++){
       int px=rect.X+x/4,py=rect.Y+y;
       if(caret!=null&&px>=caret[0]&&px<caret[0]+caret[2]&&py>=caret[1]&&py<caret[1]+caret[3])continue;
       if(ar[x]!=br[x])return false;
      }
     }
     return true;
    }finally{b.UnlockBits(bd);}
   }finally{a.UnlockBits(ad);}
  }
 }
 // The caller keeps the original PNG for all frame and input checks.
 public static byte[] CompactScreenshot(byte[] png) {
  if(png.Length<65536)return png;
  using(var src=new MemoryStream(png))
  using(var bitmap=new Bitmap(src)){
   byte[] best=png;
   // Round each channel to 5 bits: all original pixels/coordinates are kept,
   // black and white stay exact, and no channel moves more than four levels.
   byte[] levels=new byte[256];
   for(int i=0;i<levels.Length;i++)levels[i]=(byte)((((i*31+127)/255)*255+15)/31);
   using(var reduced=bitmap.Clone(new Rectangle(0,0,bitmap.Width,bitmap.Height),PixelFormat.Format24bppRgb)){
    var bits=reduced.LockBits(new Rectangle(0,0,reduced.Width,reduced.Height),ImageLockMode.ReadWrite,PixelFormat.Format24bppRgb);
    try{
     byte[] row=new byte[reduced.Width*3];
     for(int y=0;y<reduced.Height;y++){
      IntPtr address=IntPtr.Add(bits.Scan0,y*bits.Stride);
      Marshal.Copy(address,row,0,row.Length);
      for(int x=0;x<row.Length;x+=3){
       int b=row[x],g=row[x+1],r=row[x+2];
       // Keep neutral text and its antialiased edges byte-for-byte.
       if(Math.Max(r,Math.Max(g,b))-Math.Min(r,Math.Min(g,b))<=16)continue;
       row[x]=levels[b];row[x+1]=levels[g];row[x+2]=levels[r];
      }
      Marshal.Copy(row,0,address,row.Length);
     }
    }finally{reduced.UnlockBits(bits);}
    using(var encoded=new MemoryStream()){
     reduced.Save(encoded,ImageFormat.Png);
     if(encoded.Length*4<=(long)png.Length*3)best=encoded.ToArray();
    }
   }
   foreach(var codec in ImageCodecInfo.GetImageEncoders()){
    if(codec.FormatID!=ImageFormat.Jpeg.Guid)continue;
    using(var encoded=new MemoryStream())
    using(var options=new EncoderParameters(1)){
     options.Param[0]=new EncoderParameter(System.Drawing.Imaging.Encoder.Quality,90L);
     bitmap.Save(encoded,codec,options);
     // Prefer the bounded color change of PNG when the two sizes are close.
     if(encoded.Length*4<=(long)png.Length*3 && encoded.Length*5<=(long)best.Length*3)best=encoded.ToArray();
    }
    break;
   }
   return best;
  }
 }
 public static byte[] Capture() {
   Check(); var b=Bounds(); if(b[2]<=0 || b[3]<=0 || (long)b[2]*b[3]>50000000) throw new Exception("DISPLAY_SIZE_UNSUPPORTED");
   using(var bitmap=new Bitmap(b[2],b[3])) {using(var g=Graphics.FromImage(bitmap)) {g.CopyFromScreen(b[0],b[1],0,0,new Size(b[2],b[3]));} using(var s=new MemoryStream()) {bitmap.Save(s,ImageFormat.Png); return s.ToArray();}}
 }
 public static string InputStopFile;
 static void Send(INPUT[] a) {if(!String.IsNullOrEmpty(InputStopFile)&&File.Exists(InputStopFile))throw new Exception("WORKER_STOPPING_BEFORE_INPUT");Check();CheckInputAccess(); if(SendInput((uint)a.Length,a,Marshal.SizeOf(typeof(INPUT)))!=a.Length) throw new Exception("INPUT_NOT_CONFIRMED_POSSIBLE_UIPI_DO_NOT_RETRY");}
 static INPUT Mouse(int x,int y,uint flags,uint data) {var i=new INPUT();i.type=0;i.u.mouse=new MOUSE{x=x,y=y,flags=flags,data=data};return i;}
 static INPUT Key(ushort vk,ushort scan,uint flags) {var i=new INPUT();i.type=1;i.u.key=new KEY{vk=vk,scan=scan,flags=flags};return i;}
 public static void Move(int x,int y) {var b=Bounds();if(x<0||y<0||x>=b[2]||y>=b[3]) throw new Exception("OUT_OF_FRAME"); Send(new[]{Mouse((int)Math.Round(x*65535.0/Math.Max(1,b[2]-1)),(int)Math.Round(y*65535.0/Math.Max(1,b[3]-1)),0xC001,0)});
  // Verify movement before button events. Never resend an uncertain click.
  System.Threading.Thread.Sleep(50);var actual=CursorPosition();
  if(Math.Abs(actual[0]-(b[0]+x))>2||Math.Abs(actual[1]-(b[1]+y))>2)throw new Exception("POINTER_MOVE_NOT_CONFIRMED_NO_CLICK_SENT");
 }
 public static void Click(int x,int y,bool right) {Move(x,y);Send(new[]{Mouse(0,0,right?8u:2u,0),Mouse(0,0,right?16u:4u,0)});}
 public static void DoubleClick(int x,int y) {Move(x,y);Send(new[]{Mouse(0,0,2,0),Mouse(0,0,4,0),Mouse(0,0,2,0),Mouse(0,0,4,0)});}
 public static void Scroll(int amount) {Send(new[]{Mouse(0,0,0x800,unchecked((uint)(amount*120)))});}
 // CRLF is one Enter, LF/CR are Enter; other UTF-16 units retain Unicode input.
 // Keep each UTF-16 key down/up pair together; pace pairs for TSF-backed editors.
 static INPUT[] TextInputs(string text) {
  if(text==null||text.Length>2000)throw new Exception("INVALID_TEXT");
  var a=new List<INPUT>();
  for(int n=0;n<text.Length;n++){
   char c=text[n];
   if(c=='\r'||c=='\n'){
    if(c=='\r'&&n+1<text.Length&&text[n+1]=='\n')n++;
    a.Add(Key(13,0,0));a.Add(Key(13,0,2));
   }else{a.Add(Key(0,c,4));a.Add(Key(0,c,6));}
  }
  return a.ToArray();
 }
 public static void Text(string text) {
  var a=TextInputs(text);var hwnd=GetForegroundWindow();var focus=FocusIdentity();
  for(int n=0;n<a.Length;n+=2){
   if(GetForegroundWindow()!=hwnd||FocusIdentity()!=focus)throw new Exception("TEXT_TARGET_CHANGED_PARTIAL_INPUT_DO_NOT_RETRY");
   Send(new[]{a[n],a[n+1]});System.Threading.Thread.Sleep(15);
  }
 }
 public static void Keys(ushort[] keys) {
  var a=new INPUT[keys.Length*2];for(int i=0;i<keys.Length;i++){a[i]=Key(keys[i],0,0);a[a.Length-1-i]=Key(keys[i],0,2);} Send(a);
 }
}
