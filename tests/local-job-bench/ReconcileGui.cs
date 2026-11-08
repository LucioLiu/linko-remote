// Synthetic fixture only. Uses the same LedgerReconcile engine as the scoped CLI.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public sealed class ReconcileGui : Form {
 [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] static extern void SwitchToThisWindow(IntPtr h,bool altTab);
 readonly string home=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CodexRemoteDesktop");
 readonly LedgerBatch[] results=new LedgerBatch[6];
 readonly Button[] buttons=new Button[6];
 readonly JavaScriptSerializer json=new JavaScriptSerializer();
 readonly Label info=new Label(), status=new Label(), digest=new Label();
 readonly PictureBox art=new PictureBox();
 readonly Timer timer=new Timer();
 readonly Button export=new Button(),switcher=new Button();
 string route,shownB="";
 string Inbox(string scope){return Path.Combine(home,"job-inbox",scope);}
 Label LabelAt(string text,int x,int y,int w,int h,int size){var l=new Label {Text=text,Location=new Point(x,y),Size=new Size(w,h),Font=new Font("Segoe UI",size),ForeColor=Color.FromArgb(30,48,62)};Controls.Add(l);return l;}
 void LabelSetup(Label l,int x,int y,int w,int h,int size){l.SetBounds(x,y,w,h);l.Font=new Font("Segoe UI",size);Controls.Add(l);}
 Button MakeButton(string text,int x,int y,int w,int h){var b=new Button();ButtonSetup(b,text,x,y,w,h);return b;}
 void ButtonSetup(Button b,string text,int x,int y,int w,int h){b.Text=text;b.SetBounds(x,y,w,h);b.Font=new Font("Segoe UI",13);Controls.Add(b);}
 public ReconcileGui(string start){
  route=start;AutoScaleMode=AutoScaleMode.None;Text="LanPilot synthetic reconciliation - "+Environment.MachineName;
  TopMost=true;BackColor=Color.White;StartPosition=FormStartPosition.Manual;FormBorderStyle=FormBorderStyle.None;Bounds=Screen.PrimaryScreen.Bounds;
  Font=new Font("Segoe UI",14);LabelAt("Ledger reconciliation  |  "+Environment.MachineName,45,30,1150,50,28);
  LabelSetup(info,48,92,1150,55,17);LabelSetup(status,48,625,1280,80,18);LabelSetup(digest,48,724,1500,75,14);
  for(int i=0;i<6;i++){int index=i;var b=MakeButton("Run batch "+(i+1).ToString("00"),50,165+i*57,245,48);b.Click+=delegate{RunBatch(index);};buttons[i]=b;}
  ButtonSetup(export,"Export result CSV",50,524,245,61);export.Click+=delegate{Export();};
  ButtonSetup(switcher,"Switch route",355,520,220,60);switcher.Click+=delegate{SetMode(route=="A"?"B":"A");};
  var close=MakeButton("Close fixture",1380,34,185,45);close.Click+=delegate{Close();};
  art.SetBounds(655,155,560,450);art.SizeMode=PictureBoxSizeMode.Normal;Controls.Add(art);
  timer.Interval=600;timer.Tick+=delegate{if(route=="B")ReadReceipt();};timer.Start();
  Shown+=delegate{Activate();SetMode(start);var activate=new Timer{Interval=13000};activate.Tick+=delegate{activate.Stop();SwitchToThisWindow(Handle,true);activate.Dispose();};activate.Start();};
 }
 protected override void Dispose(bool disposing){if(disposing){timer.Dispose();if(art.Image!=null)art.Image.Dispose();}base.Dispose(disposing);}
 void ClearReceipt(){if(art.Image!=null){var old=art.Image;art.Image=null;old.Dispose();}digest.Text="";}
 void SetMode(string mode){
  route=mode;ClearReceipt();info.Text=route=="A"?"A  |  Run six batches, then export the CSV.":"B  |  Waiting for the local reconciliation receipt.";
  switcher.Text=route=="A"?"Show local job (B)":"Show desktop route (A)";
  for(int i=0;i<6;i++){buttons[i].Text=results[i]==null?"Run batch "+(i+1).ToString("00"):"Done "+(i+1).ToString("00");buttons[i].Enabled=route=="A"&&results[i]==null;}
  export.Enabled=route=="A"&&results.All(x=>x!=null);status.Text="Ready";
  if(route=="B"){shownB="";ReadReceipt();}
 }
 void RunBatch(int i){
  if(route!="A"||results[i]!=null)return;
  try{var x=LedgerReconcile.ProcessBatch(Inbox("bench1109"),(i+1).ToString("00"));results[i]=x;buttons[i].Enabled=false;
   buttons[i].Text="Done "+x.Batch+"  |  "+x.DiscrepancyCount+" differences";status.Text="Completed "+results.Count(v=>v!=null)+" / 6; latest delta: "+x.TotalDeltaCents+" cents.";
   export.Enabled=results.All(b=>b!=null);
  }catch(Exception e){status.Text="Batch failed: "+e.GetType().Name+" / "+e.Message;}
 }
 void Export(){if(route!="A"||results.Any(b=>b==null))return;
  try{string outDir=Inbox("bench1109-a");Directory.CreateDirectory(outDir);LedgerReconcile.Export(outDir,results);
   Present(Path.Combine(outDir,"result.csv"),null,"A");export.Enabled=false;
  }catch(Exception e){status.Text="Export failed: "+e.Message;}
 }
 void ReadReceipt(){
  string receipt=Path.Combine(Inbox("bench1109-b"),"receipt.json");if(!File.Exists(receipt))return;
  try{var m=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(receipt));var id=(string)m["request_id"];
   if(!Regex.IsMatch(id,"^[0-9a-f]{32}$")||id==shownB)return;
   string sha=(string)m["csv_sha256"];if(!Regex.IsMatch(sha,"^[a-fA-F0-9]{64}$"))throw new Exception("Invalid checksum format");
   Present(Path.Combine(home,"jobs",id,"result.csv"),sha,"B");shownB=id;
  }catch(IOException){}catch(Exception e){status.Text="Local result not verified: "+e.Message;}
 }
 void Present(string path,string expected,string mode){
  byte[] data=File.ReadAllBytes(path);if(data.Length>8192)throw new Exception("Fixture receipt exceeds 8 KiB");
  byte[] sum;using(var h=SHA256.Create()){sum=h.ComputeHash(data);}
  string sha=BitConverter.ToString(sum).Replace("-","").ToLowerInvariant();if(expected!=null&&!sha.Equals(expected,StringComparison.OrdinalIgnoreCase))throw new Exception("Result does not match CLI receipt");
  int rows=Encoding.UTF8.GetString(data).TrimEnd('\r','\n').Split('\n').Length-1;
  ClearReceipt();art.Image=Encode(data,sum);status.Text=mode+" COMPLETE   |   6 batches   |   "+rows+" discrepancies   |   "+data.Length+" bytes";
  digest.Text="SHA-256: "+sha;info.Text=mode+"  |  Verified output CSV. The black-and-white receipt contains its exact bytes.";
 }
 static Bitmap Encode(byte[] data,byte[] hash){
  var payload=new List<byte>();payload.AddRange(Encoding.ASCII.GetBytes("LPQA1"));payload.Add((byte)(data.Length>>8));payload.Add((byte)data.Length);payload.AddRange(hash);payload.AddRange(data);
  const int cols=160,unit=3;int rows=(payload.Count*8+cols-1)/cols;var img=new Bitmap(cols*unit+12,rows*unit+12);
  using(var g=Graphics.FromImage(img)){g.Clear(Color.White);g.FillRectangle(Brushes.Magenta,0,0,6,6);g.FillRectangle(Brushes.Cyan,cols*unit+6,0,6,6);
   for(int n=0;n<payload.Count*8;n++)if((payload[n/8]&(128>>(n%8)))!=0)g.FillRectangle(Brushes.Black,6+(n%cols)*unit,6+(n/cols)*unit,unit,unit);}
  return img;
 }
 [STAThread]public static void Main(string[] args){SetProcessDPIAware();Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new ReconcileGui(args.Length>0&&args[0]=="B"?"B":"A"));}
}
