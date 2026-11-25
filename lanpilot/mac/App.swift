import Cocoa
import UniformTypeIdentifiers

final class App: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
 var window:NSWindow!;var status:NSStatusItem!;var timer:Timer?;var busy=false
 var table=NSTableView();var search=NSSearchField();var summary=NSTextField(labelWithString:"")
 var localLabel=NSTextField(labelWithString:"");var pairName=NSTextField();var codeLabel=NSTextField(labelWithString:"••••  ••••  ••••")
 var codeHint=NSTextField(labelWithString:"生成后，在 Windows 邻控中输入即可连接")
 var generateButton:NSButton!;var copyCodeButton:NSButton!;var expires:TimeInterval=0;var currentCode="";var clipboardCode=""
 var devices=[[String:Any]]();var sshItems=[[String:Any]]();var rows=[[String:Any]]()
 var localName:String {get{UserDefaults.standard.string(forKey:"computerName") ?? "这台电脑"} set{UserDefaults.standard.set(newValue,forKey:"computerName")}}
 let executable=Bundle.main.path(forResource:"lanpilot",ofType:nil)!
 func label(_ text:String,_ size:CGFloat,_ bold:Bool,_ rect:NSRect,_ parent:NSView)->NSTextField {
  let l=NSTextField(labelWithString:text);l.font = .systemFont(ofSize:size,weight:bold ? .semibold : .regular);l.frame=rect;parent.addSubview(l);return l
 }
 @discardableResult func button(_ text:String,_ action:Selector,_ rect:NSRect,_ parent:NSView)->NSButton {
  let b=NSButton(title:text,target:self,action:action);b.bezelStyle = .rounded;b.frame=rect;parent.addSubview(b);return b
 }
 func card(_ rect:NSRect,_ parent:NSView)->NSView {
  let v=NSView(frame:rect);v.wantsLayer=true;v.layer?.backgroundColor=NSColor.controlBackgroundColor.cgColor;v.layer?.cornerRadius=14;v.layer?.borderWidth=1;v.layer?.borderColor=NSColor.separatorColor.withAlphaComponent(0.35).cgColor;parent.addSubview(v);return v
 }
 func content()->NSView {
  let v=NSView(frame:NSRect(x:0,y:0,width:880,height:710));v.wantsLayer=true;v.layer?.backgroundColor=NSColor.windowBackgroundColor.cgColor
  _=label("邻控",30,true,NSRect(x:28,y:650,width:130,height:42),v)
  let sub=label("让远方的电脑，就在手边",13,false,NSRect(x:30,y:626,width:450,height:22),v);sub.textColor = .secondaryLabelColor
  button("SSH 连接…",#selector(addSSH),NSRect(x:610,y:653,width:110,height:32),v)
  button("更多接入…",#selector(addDevice),NSRect(x:730,y:653,width:122,height:32),v)
  let local=card(NSRect(x:28,y:409,width:280,height:198),v)
  let cap=label("这台电脑 · 控制端",12,false,NSRect(x:20,y:156,width:240,height:24),local);cap.textColor = .secondaryLabelColor
  localLabel=label(localName,21,true,NSRect(x:20,y:111,width:240,height:32),local)
  button("编辑名称",#selector(renameLocal),NSRect(x:14,y:69,width:100,height:30),local)
  let desc=label("macOS · 后台连接 Windows\n不同 Wi-Fi 也可连接",12,false,NSRect(x:20,y:17,width:240,height:43),local);desc.maximumNumberOfLines=2;desc.textColor = .secondaryLabelColor
  let pairing=card(NSRect(x:324,y:409,width:528,height:198),v)
  _=label("用验证码添加电脑",17,true,NSRect(x:20,y:155,width:310,height:26),pairing)
  pairName=NSTextField(frame:NSRect(x:20,y:116,width:324,height:27));pairName.placeholderString="给对方取个名字，例如：办公室电脑";pairing.addSubview(pairName)
  generateButton=button("生成验证码",#selector(generateCode),NSRect(x:356,y:113,width:150,height:32),pairing)
  codeLabel=label("••••  ••••  ••••",27,true,NSRect(x:20,y:64,width:355,height:39),pairing);codeLabel.font = .monospacedDigitSystemFont(ofSize:27,weight:.semibold);codeLabel.isSelectable=true
  copyCodeButton=button("复制连接码",#selector(copyCode),NSRect(x:377,y:67,width:127,height:30),pairing);copyCodeButton.isEnabled=false
  codeHint=label("生成后，在 Windows 邻控中输入即可连接",12,false,NSRect(x:20,y:23,width:490,height:32),pairing);codeHint.maximumNumberOfLines=2;codeHint.textColor = .secondaryLabelColor
  _=label("我的电脑",20,true,NSRect(x:28,y:360,width:180,height:30),v)
  search=NSSearchField(frame:NSRect(x:552,y:360,width:200,height:29));search.placeholderString="搜索电脑或 SSH";search.delegate=self;v.addSubview(search)
  button("刷新",#selector(check),NSRect(x:766,y:357,width:88,height:32),v)
  let scroll=NSScrollView(frame:NSRect(x:28,y:117,width:824,height:230));scroll.hasVerticalScroller=true;scroll.borderType = .noBorder;scroll.wantsLayer=true;scroll.layer?.cornerRadius=10
  table=NSTableView();let column=NSTableColumn(identifier:NSUserInterfaceItemIdentifier("computer"));column.width=804;table.addTableColumn(column);table.headerView=nil;table.rowHeight=67;table.delegate=self;table.dataSource=self;table.target=self;table.doubleAction=#selector(openSelected);table.allowsEmptySelection=true;scroll.documentView=table;v.addSubview(scroll)
  button("编辑电脑名称",#selector(renameDevice),NSRect(x:22,y:74,width:142,height:32),v)
  button("打开所选电脑",#selector(openSelected),NSRect(x:174,y:74,width:144,height:32),v)
  button("复制给 AI 的指令",#selector(copyPrompt),NSRect(x:328,y:74,width:165,height:32),v)
  button("导出 Windows 客户端",#selector(exportClient),NSRect(x:643,y:74,width:215,height:32),v)
  summary=label("正在读取设备…",12,false,NSRect(x:30,y:29,width:822,height:28),v);summary.textColor = .secondaryLabelColor
  return v
 }
 func applicationDidFinishLaunching(_ n:Notification){
  NSApp.setActivationPolicy(.regular)
  window=NSWindow(contentRect:NSRect(x:0,y:0,width:880,height:710),styleMask:[.titled,.closable,.miniaturizable],backing:.buffered,defer:false);window.title="邻控 · Linko Remote";window.center();window.contentView=content()
  status=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength);status.button?.title="邻控"
  let menu=NSMenu();menu.addItem(withTitle:"打开邻控",action:#selector(show),keyEquivalent:"").target=self;menu.addItem(withTitle:"刷新设备",action:#selector(check),keyEquivalent:"").target=self;menu.addItem(withTitle:"断开后台连接",action:#selector(disconnect),keyEquivalent:"").target=self;menu.addItem(.separator());menu.addItem(withTitle:"退出面板（后台保持在线）",action:#selector(quit),keyEquivalent:"q").target=self;status.menu=menu
  if !ProcessInfo.processInfo.arguments.contains("--background"){window.makeKeyAndOrderFront(nil)}
  run(["service-start"]){_ in self.check()}
  timer=Timer.scheduledTimer(withTimeInterval:5,repeats:true){[weak self] _ in self?.check();self?.updateCodeHint()}
 }
 func run(_ args:[String],_ completion:@escaping (Result<Data,Error>)->Void){
  DispatchQueue.global().async{
   let p=Process(),pipe=Pipe();p.executableURL=URL(fileURLWithPath:self.executable);p.arguments=args;p.standardOutput=pipe;p.standardError=pipe
   do{try p.run();let data=pipe.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit()
    let result:Result<Data,Error> = p.terminationStatus==0 ? .success(data) : .failure(NSError(domain:"LanPilot",code:Int(p.terminationStatus),userInfo:[NSLocalizedDescriptionKey:String(decoding:data,as:UTF8.self)]))
    DispatchQueue.main.async{completion(result)}
   }catch{DispatchQueue.main.async{completion(.failure(error))}}
  }
 }
 func notice(_ title:String,_ text:String){let a=NSAlert();a.messageText=title;a.informativeText=text;a.runModal()}
 func askName(_ title:String,_ value:String)->String? {
  let a=NSAlert();a.messageText=title;let f=NSTextField(frame:NSRect(x:0,y:0,width:300,height:26));f.stringValue=value;a.accessoryView=f;a.addButton(withTitle:"保存");a.addButton(withTitle:"取消")
  guard a.runModal() == .alertFirstButtonReturn else{return nil}
  let name=f.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
  if name.isEmpty || name.count>100 || name.unicodeScalars.contains(where:{$0.value<32 || $0.value==127}){notice("名称无效","请输入 1–100 个字符。");return nil};return name
 }
 @objc func renameLocal(){if let name=askName("编辑这台电脑的名称",localName){localName=name;localLabel.stringValue=name}}
 @objc func renameDevice(){
  guard table.selectedRow>=0 else{notice("先选择一台电脑","点击列表中的电脑，再编辑名称。");return}
  let d=rows[table.selectedRow]
  guard let name=askName("编辑电脑名称",d["name"] as? String ?? "") else{return}
  run([d["ssh"] == nil ? "rename" : "ssh-rename",d["id"] as! String,name]){r in if case .failure(let e)=r{self.notice("保存失败",e.localizedDescription)};self.check()}
 }
 @objc func generateCode(){
  let name=pairName.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
  guard !name.isEmpty else{notice("为对方电脑取个名字","例如：办公室电脑。以后也可以修改。");return}
  generateButton.isEnabled=false;generateButton.title="正在生成…";currentCode="";copyCodeButton.isEnabled=false;codeLabel.stringValue="正在准备…"
  run(["pair-code",name]){r in
   self.generateButton.isEnabled=true;self.generateButton.title="刷新验证码"
   switch r {
   case .success(let data):
    if let j=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],let code=j["code"] as? String,let end=j["expires"] as? Double{self.currentCode=code;self.clipboardCode=j["connection"] as? String ?? code;self.expires=end;self.codeLabel.stringValue=code;self.copyCodeButton.isEnabled=true;self.updateCodeHint()}
   case .failure(let e):self.codeLabel.stringValue="暂时无法生成";self.notice("生成失败",e.localizedDescription)
   }
  }
 }
 func updateCodeHint(){
  guard !currentCode.isEmpty else{return};let remain=Int(expires-Date().timeIntervalSince1970)
  if remain<=0{codeHint.stringValue="验证码已过期，请重新生成";copyCodeButton.isEnabled=false;currentCode="";codeLabel.stringValue="••••  ••••  ••••"}
  else{codeHint.stringValue=String(format:"%02d:%02d 后失效 · 复制连接码，粘贴到 Windows 邻控",remain/60,remain%60)}
 }
 @objc func copyCode(){guard !currentCode.isEmpty,expires>Date().timeIntervalSince1970 else{return};copy(clipboardCode)}
 func copy(_ text:String){NSPasteboard.general.clearContents();NSPasteboard.general.setString(text,forType:.string)}
 @objc func exportClient(){
  let panel=NSSavePanel();panel.nameFieldStringValue="邻控-Windows-验证码接入.zip";guard panel.runModal() == .OK,let path=panel.url else{return}
  run(["windows-client",path.path,"--controller",localName]){r in switch r{case .success:self.notice("Windows 客户端已导出","这份客户端可以重复使用，不含验证码或私钥。Windows 解压后双击「打开邻控.cmd」，输入本面板的验证码即可接入。请通过自己的可信下载渠道提供此包。")
   case .failure(let e):self.notice("导出失败",e.localizedDescription)}}
 }
 @objc func addSSH(){
  let a=NSAlert();a.messageText="添加 SSH 连接";a.informativeText="连接已开启 SSH 的电脑或服务器。可使用 ~/.ssh/config 中的主机别名。"
  let box=NSView(frame:NSRect(x:0,y:0,width:340,height:164));var fields=[NSTextField]()
  for (i,text) in ["名称，例如：示例服务器","主机 IP / 域名 / SSH 别名","用户名，留空沿用 SSH 配置","端口，留空沿用 SSH 配置"].enumerated(){let f=NSTextField(frame:NSRect(x:0,y:CGFloat(126-i*40),width:340,height:27));f.placeholderString=text;box.addSubview(f);fields.append(f)}
  a.accessoryView=box;a.addButton(withTitle:"保存连接");a.addButton(withTitle:"取消");guard a.runModal() == .alertFirstButtonReturn else{return}
  let portArgs = fields[3].stringValue.isEmpty ? [] : ["--port",fields[3].stringValue]
  run(["ssh-add",fields[0].stringValue,fields[1].stringValue,"--user",fields[2].stringValue] + portArgs){r in if case .failure(let e)=r{self.notice("保存失败",e.localizedDescription)};self.check()}
 }
 @objc func openSelected(){
  guard table.selectedRow>=0 else{return};let row=rows[table.selectedRow]
  if row["ssh"] == nil {
   self.summary.stringValue="正在读取远程画面…"
   run(["observe",row["id"] as! String,"--image-format","compact"]){r in
    guard case .success(let data)=r,let j=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],j["ok"] as? Bool==true,let result=j["result"] as? [String:Any],let path=result["screenshot"] as? String,let image=NSImage(contentsOfFile:path) else{self.notice("尚未取得画面","请检查对方邻控与网络。请求结果未确认时，不要重复操作。");self.check();return}
    self.showScreen(image,row["name"] as? String ?? "电脑");self.check()
   };return
  }
  run(["ssh-open",row["id"] as! String]){r in if case .failure(let e)=r{self.notice("打开失败",e.localizedDescription)}}
 }
 var screenWindow:NSWindow?
 func showScreen(_ image:NSImage,_ name:String){
  let w=NSWindow(contentRect:NSRect(x:0,y:0,width:1000,height:680),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false);w.title=name+" · 画面快照";w.isReleasedWhenClosed=false
  let view=NSImageView(frame:NSRect(x:0,y:0,width:1000,height:680));view.image=image;view.imageScaling = .scaleProportionallyUpOrDown;view.autoresizingMask=[.width,.height];w.contentView=view;w.center();w.makeKeyAndOrderFront(nil);screenWindow=w
 }
 @objc func copyPrompt(){
  let selected=table.selectedRow>=0 ? rows[table.selectedRow] : nil
  if let row=selected,row["ssh"] != nil{copy("通过 SSH 连接我保存的主机「\(row["host"] as? String ?? "")」，先核对连接和当前状态，再按我的任务操作。");return}
  let target=selected?["id"] as? String ?? "已配对的电脑"
  copy("用邻控连接设备 \(target)，先查看当前画面，再按我的任务操作。结果不明时查询原请求，不重复执行动作。")
 }
 @objc func check(){
  if busy{return};busy=true
  run(["devices"]){r in
   if case .success(let data)=r,let list=(try? JSONSerialization.jsonObject(with:data)) as? [[String:Any]]{self.devices=list
    if !self.currentCode.isEmpty && list.contains(where:{$0["name"] as? String==self.pairName.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)}){self.currentCode="";self.copyCodeButton.isEnabled=false;self.codeLabel.stringValue="已完成配对";self.codeHint.stringValue="此验证码已使用。新的电脑请重新生成验证码。"}
   }
   else{self.summary.stringValue="暂时无法读取状态，请刷新。";self.busy=false;return}
   self.run(["ssh-list"]){r in
    if case .success(let data)=r,let items=(try? JSONSerialization.jsonObject(with:data)) as? [[String:Any]]{self.sshItems=items}
    self.filterRows();let count=self.devices.filter{$0["online"] as? Bool == true}.count
    self.status?.button?.title="邻控 \(count)";self.summary.stringValue=self.devices.isEmpty && self.sshItems.isEmpty ? "还没有电脑：生成验证码，或添加 SSH 连接。" : "\(count) 台心跳在线 · \(self.devices.count) 台已配对 · \(self.sshItems.count) 个 SSH 连接 · 在线状态不代表桌面已核验"
    self.busy=false
   }
  }
 }
 func filterRows(){
  let selectedID=table.selectedRow>=0 && table.selectedRow<rows.count ? rows[table.selectedRow]["id"] as? String : nil
  let needle=search.stringValue.lowercased();rows=(devices+sshItems.map{var d=$0;d["ssh"]=true;return d}).filter{needle.isEmpty || (($0["name"] as? String ?? "")+" "+($0["id"] as? String ?? "")+" "+($0["host"] as? String ?? "")).lowercased().contains(needle)}
  table.reloadData();if let id=selectedID,let i=rows.firstIndex(where:{$0["id"] as? String==id}){table.selectRowIndexes(IndexSet(integer:i),byExtendingSelection:false)}
 }
 func controlTextDidChange(_ obj:Notification){filterRows()}
 func numberOfRows(in tableView:NSTableView)->Int{return rows.count}
 func tableView(_ tableView:NSTableView,viewFor tableColumn:NSTableColumn?,row:Int)->NSView? {
  let d=rows[row];let v=NSView(frame:NSRect(x:0,y:0,width:804,height:67));let ssh=d["ssh"] != nil;let online=d["online"] as? Bool ?? false
  let icon=NSImageView(frame:NSRect(x:14,y:17,width:31,height:31));icon.image=NSImage(systemSymbolName:ssh ? "terminal" : "desktopcomputer",accessibilityDescription:ssh ? "SSH" : "Windows");icon.contentTintColor = ssh ? .systemIndigo : .systemBlue;v.addSubview(icon)
  _=label(d["name"] as? String ?? "电脑",15,true,NSRect(x:62,y:34,width:490,height:24),v)
  let subtitle=ssh ? "\(d["user"] as? String ?? "")@\(d["host"] as? String ?? "") · SSH" : "设备 \(d["id"] as? String ?? "") · Windows"
  let detail=label(subtitle,11,false,NSRect(x:62,y:11,width:490,height:20),v);detail.textColor = .secondaryLabelColor
  let state=label(ssh ? "双击打开终端" : (online ? "● 心跳在线" : "○ 离线 / 等待连接"),12,false,NSRect(x:590,y:24,width:200,height:24),v);state.textColor=ssh ? .secondaryLabelColor : (online ? .systemGreen : .secondaryLabelColor)
  return v
 }
 @objc func disconnect(){run(["service-stop"]){_ in self.check()}}
 @objc func show(){window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)}
 @objc func quit(){NSApp.terminate(nil)}
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{if !flag{show()};return true}
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return false}
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
      if p.terminationStatus==0 {self.notice("安装包已生成","将压缩包拷到目标 Windows，解压后双击 Install.cmd。异地连接可在 Windows 面板填写可达的远程地址。");self.check()}
      else{self.notice("生成失败",message)}
     }
    }catch{DispatchQueue.main.async{self.notice("生成失败",error.localizedDescription)}}
   }
  default:break
  }
 }
}
let app=NSApplication.shared;let delegate=App()
if let i=CommandLine.arguments.firstIndex(of:"--preview"),CommandLine.arguments.count>i+1 {
 app.setActivationPolicy(.prohibited);let view=delegate.content()
 delegate.devices=[["id":"example001","name":"办公室电脑","online":true],["id":"example002","name":"家里的电脑","online":false]]
 delegate.sshItems=[["id":"example003","name":"示例服务器","host":"server.example.com","user":"admin","port":22]];delegate.filterRows()
 delegate.pairName.stringValue="办公室电脑";delegate.codeLabel.stringValue="1234  5678  9012";delegate.codeHint.stringValue="示例验证码 · 10 分钟有效";delegate.summary.stringValue="界面示例 · 设备和验证码均为演示数据"
 view.layoutSubtreeIfNeeded();let rep=view.bitmapImageRepForCachingDisplay(in:view.bounds)!;view.cacheDisplay(in:view.bounds,to:rep)
 try rep.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[i+1]))
}else{app.delegate=delegate;app.run()}
