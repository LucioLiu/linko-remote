import Cocoa
import UniformTypeIdentifiers
final class App: NSObject, NSApplicationDelegate {
 var window:NSWindow!;var status:NSStatusItem!;var output:NSTextView!;var timer:Timer?;var busy=false
 let executable=Bundle.main.path(forResource:"lanpilot",ofType:nil)!
 func content()->NSView {
  let view=NSView(frame:NSRect(x:0,y:0,width:680,height:480));view.wantsLayer=true;view.layer?.backgroundColor=NSColor.windowBackgroundColor.cgColor
  let title=NSTextField(labelWithString:"邻控");title.font = .systemFont(ofSize:34,weight:.bold);title.frame=NSRect(x:32,y:400,width:500,height:50);view.addSubview(title)
  let sub=NSTextField(labelWithString:"已配对的 Windows 电脑");sub.font = .systemFont(ofSize:18);sub.textColor = .secondaryLabelColor;sub.frame=NSRect(x:34,y:367,width:610,height:28);view.addSubview(sub)
  let scroll=NSScrollView(frame:NSRect(x:32,y:145,width:616,height:205));scroll.hasVerticalScroller=true;scroll.borderType = .bezelBorder
  output=NSTextView(frame:scroll.bounds);output.isEditable=false;output.font = .systemFont(ofSize:18);output.textContainerInset=NSSize(width:18,height:18);scroll.documentView=output;view.addSubview(scroll)
  let refresh=NSButton(title:"刷新设备",target:self,action:#selector(connect));refresh.bezelStyle = .rounded;refresh.frame=NSRect(x:32,y:90,width:140,height:36);view.addSubview(refresh)
  let copy=NSButton(title:"复制给 Codex 的指令",target:self,action:#selector(copyPrompt));copy.bezelStyle = .rounded;copy.frame=NSRect(x:187,y:90,width:210,height:36);view.addSubview(copy)
  let add=NSButton(title:"添加电脑",target:self,action:#selector(addDevice));add.bezelStyle = .rounded;add.frame=NSRect(x:420,y:90,width:140,height:36);view.addSubview(add)
  let hint=NSTextField(wrappingLabelWithString:"Mac 后台运行，不抢本机前台；操作会作用于 Windows 桌面。\n关闭面板仍在线。电脑需保持登录、联网且未休眠。");hint.textColor = .secondaryLabelColor;hint.frame=NSRect(x:34,y:28,width:610,height:46);view.addSubview(hint)
  return view
 }
 func applicationDidFinishLaunching(_ n:Notification){
  NSApp.setActivationPolicy(.regular)
  window=NSWindow(contentRect:NSRect(x:0,y:0,width:680,height:480),styleMask:[.titled,.closable,.miniaturizable],backing:.buffered,defer:false);window.title="邻控";window.center();window.contentView=content()
  status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength);status.button?.title="邻控"
  let menu=NSMenu();menu.addItem(withTitle:"打开邻控",action:#selector(show),keyEquivalent:"").target=self;menu.addItem(withTitle:"刷新设备",action:#selector(connect),keyEquivalent:"").target=self;menu.addItem(withTitle:"断开后台连接",action:#selector(disconnect),keyEquivalent:"").target=self;menu.addItem(.separator());menu.addItem(withTitle:"退出面板（后台保持在线）",action:#selector(quit),keyEquivalent:"q").target=self;status.menu=menu
  window.makeKeyAndOrderFront(nil);connect();timer=Timer.scheduledTimer(withTimeInterval:10,repeats:true){[weak self] _ in self?.check()}
 }
 func service(_ command:String){DispatchQueue.global().async{let p=Process();p.executableURL=URL(fileURLWithPath:self.executable);p.arguments=[command];p.standardOutput=Pipe();p.standardError=Pipe();do{try p.run();p.waitUntilExit()}catch{};DispatchQueue.main.async{self.check()}}}
 @objc func connect(){service("service-start")}
 @objc func disconnect(){service("service-stop")}
 @objc func show(){window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
 func notice(_ title:String,_ text:String) {
  let alert=NSAlert();alert.messageText=title;alert.informativeText=text;alert.runModal()
 }
 @objc func addDevice(){
  let dialog=NSAlert();dialog.messageText="邀请新的 Windows 电脑";dialog.informativeText="为电脑取一个名称。把生成的私密安装文件发到这台电脑，双击后确认一次，即可接入。邀请 15 分钟有效，仅限一台电脑。"
  let field=NSTextField(frame:NSRect(x:0,y:0,width:270,height:26));field.placeholderString="例如：天津办公室";dialog.accessoryView=field
  dialog.addButton(withTitle:"生成邀请");dialog.addButton(withTitle:"手动公钥配对");dialog.addButton(withTitle:"取消")
  let choice=dialog.runModal();if choice == .alertSecondButtonReturn{manualPair();return}
  guard choice == .alertFirstButtonReturn else{return}
  let name=field.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
  guard !name.isEmpty else{notice("请填写设备名称","使用可识别的名称区分每台电脑。");return}
  let save=NSSavePanel();save.nameFieldStringValue="LanPilot-Connect.cmd";guard save.runModal() == .OK,let dest=save.url else{return}
  let command=executable;DispatchQueue.global().async{
   let p=Process(),pipe=Pipe();p.executableURL=URL(fileURLWithPath:command);p.arguments=["invite",name,dest.path];p.standardOutput=pipe;p.standardError=pipe
   do{try p.run();let data=pipe.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit();DispatchQueue.main.async{
    if p.terminationStatus==0{self.notice("邀请已生成","通过可信的私密渠道把 "+dest.lastPathComponent+" 发给目标电脑。双击并确认连接即可，无需填写 IP 或传回公钥。")}
    else{self.notice("无法生成邀请",String(decoding:data,as:UTF8.self))}
   }}catch{DispatchQueue.main.async{self.notice("无法生成邀请",error.localizedDescription)}}
  }
 }
 func manualPair(){
  let alert=NSAlert();alert.messageText="添加 Windows 电脑"
  alert.informativeText="首次接入时，将接入工具拷到 Windows，双击 Export-Public-Key.cmd，再将生成的 LanPilot-key.xml 带回这里。"
  alert.addButton(withTitle:"选择公钥");alert.addButton(withTitle:"导出接入工具");alert.addButton(withTitle:"取消")
  switch alert.runModal(){
  case .alertSecondButtonReturn:
   let panel=NSSavePanel();panel.nameFieldStringValue="邻控接入工具"
   guard panel.runModal() == .OK, let dest=panel.url,
    let source=Bundle.main.resourceURL?.appendingPathComponent("code/windows") else{return}
   do{
    // Stage the complete folder and refuse to overwrite any existing destination.
    let staged=dest.deletingLastPathComponent().appendingPathComponent(".lanpilot-"+UUID().uuidString)
    try FileManager.default.createDirectory(at:staged,withIntermediateDirectories:false)
    defer{try? FileManager.default.removeItem(at:staged)}
    for name in ["Pair-Device.ps1","Export-Public-Key.cmd"] {
     try FileManager.default.copyItem(at:source.appendingPathComponent(name),to:staged.appendingPathComponent(name))
    }
    try FileManager.default.moveItem(at:staged,to:dest)
    notice("接入工具已导出",dest.path)
   }catch{notice("导出失败",error.localizedDescription)}
  case .alertFirstButtonReturn:
   let pick=NSOpenPanel();pick.allowedContentTypes=[.xml];pick.allowsMultipleSelection=false
   guard pick.runModal() == .OK, let key=pick.url else{return}
   let form=NSAlert();form.messageText="为电脑命名"
   let field=NSTextField(frame:NSRect(x:0,y:0,width:280,height:25));field.placeholderString="例如：北京办公室"
   form.accessoryView=field;form.addButton(withTitle:"生成安装包");form.addButton(withTitle:"取消")
   guard form.runModal() == .alertFirstButtonReturn else{return}
   let name=field.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
   guard !name.isEmpty else{notice("请填写设备名称","使用可识别的名称区分每台电脑。");return}
   let save=NSSavePanel();save.nameFieldStringValue="LanPilot-Windows.zip"
   guard save.runModal() == .OK, let dest=save.url else{return}
   let executable=self.executable
   DispatchQueue.global().async{
    let p=Process();let pipe=Pipe();p.executableURL=URL(fileURLWithPath:executable)
    p.arguments=["enroll",name,key.path,dest.path];p.standardOutput=pipe;p.standardError=pipe
    do{try p.run();let data=pipe.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit()
     let message=String(decoding:data,as:UTF8.self)
     DispatchQueue.main.async{
      if p.terminationStatus==0 {self.notice("安装包已生成","将压缩包拷到目标 Windows，解压后双击 Install.cmd。异地连接可在 Windows 面板填写可达的远程地址。");self.connect()}
      else{self.notice("生成失败",message)}
     }
    }catch{DispatchQueue.main.async{self.notice("生成失败",error.localizedDescription)}}
   }
  default:break
  }
 }
 @objc func quit(){NSApp.terminate(nil)}
 @objc func copyPrompt(){NSPasteboard.general.clearContents();NSPasteboard.general.setString("用邻控查看已连接的电脑，通过后台操作完成我的任务，不要打开 UU 或占用这台 Mac 的前台。",forType:.string)}
 @objc func check(){
  if busy{return};busy=true
  DispatchQueue.global().async {
   let p=Process();p.executableURL=URL(fileURLWithPath:self.executable);p.arguments=["devices"];let pipe=Pipe();p.standardOutput=pipe;p.standardError=Pipe()
   var message="暂时无法读取状态，请点击刷新设备。";var count=0
   do{try p.run();let data=pipe.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit()
    if let devices=try JSONSerialization.jsonObject(with:data) as? [[String:Any]] {
     message=devices.isEmpty ? "尚未添加电脑。请点击「添加电脑」。" : devices.map {d in
      let online=d["online"] as? Bool ?? false;if online{count += 1}
      return "\(online ? "●" : "○")  \(d["name"] as? String ?? "电脑")\n    \(d["state"] as? String ?? "未知")\n    \(online ? "本机状态查询 \(d["latency_ms"] ?? 0) ms · 等待桌面核验" : "检查 Windows 登录状态、邻控和网络连接")"
     }.joined(separator:"\n\n")
    }
   }catch{}
   let result=message;let connected=count
   DispatchQueue.main.async{self.output.string=result;self.status.button?.title="邻控 \(connected)";self.busy=false}
  }
 }
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return false}
}
let app=NSApplication.shared;let delegate=App()
if let i=CommandLine.arguments.firstIndex(of:"--preview"),CommandLine.arguments.count>i+1 {
 app.setActivationPolicy(.prohibited)
 let view=delegate.content();delegate.output.string="●  办公电脑\n    可连接\n    本机状态查询 15 ms · 等待桌面核验"
 view.layoutSubtreeIfNeeded();let rep=view.bitmapImageRepForCachingDisplay(in:view.bounds)!;view.cacheDisplay(in:view.bounds,to:rep)
 try rep.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[i+1]))
}else{app.delegate=delegate;app.run()}
