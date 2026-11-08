using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
class Launcher {
 [STAThread] static void Main(string[] args) {
  string root=AppDomain.CurrentDomain.BaseDirectory;
  var p=new ProcessStartInfo("powershell.exe", "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \""+Path.Combine(root,"LanPilot.ps1")+"\""+(Array.IndexOf(args,"--hidden")>=0 ? " -StartHidden" : ""));
  p.UseShellExecute=false;p.CreateNoWindow=true;p.WorkingDirectory=root;
  try {Process.Start(p);} catch(Exception){MessageBox.Show("Unable to start LanPilot. Please reinstall.","LanPilot");}
 }
}
