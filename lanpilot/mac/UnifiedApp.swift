import Cocoa
import WebKit

final class UnifiedApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
 var window: NSWindow!
 var view: WKWebView!
 var process: Process?
 var authority: String?
 func applicationDidFinishLaunching(_ notification: Notification) {
  NSApp.setActivationPolicy(.regular)
  window=NSWindow(contentRect:NSRect(x:0,y:0,width:1220,height:860),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
  window.title="邻控";window.minSize=NSSize(width:860,height:680);window.center();window.isReleasedWhenClosed=false
  let configuration=WKWebViewConfiguration()
  configuration.userContentController.add(self,name:"linkoUpdate")
  view=WKWebView(frame:window.contentView!.bounds,configuration:configuration);view.navigationDelegate=self;view.autoresizingMask=[.width,.height];window.contentView=view
  window.makeKeyAndOrderFront(nil)
  let menu=NSMenu(), item=NSMenuItem(), appMenu=NSMenu()
  appMenu.addItem(withTitle:"关于邻控",action:#selector(about),keyEquivalent:"").target=self
  appMenu.addItem(.separator());appMenu.addItem(withTitle:"退出邻控",action:#selector(quit),keyEquivalent:"q").target=self
  item.submenu=appMenu;menu.addItem(item)
  let editItem=NSMenuItem(), edit=NSMenu(title:"编辑")
  for (name,action,key) in [("撤销",Selector(("undo:")),"z"),("剪切",#selector(NSText.cut(_:)),"x"),("拷贝",#selector(NSText.copy(_:)),"c"),("粘贴",#selector(NSText.paste(_:)),"v"),("全选",#selector(NSText.selectAll(_:)),"a")] {edit.addItem(withTitle:name,action:action,keyEquivalent:key)}
  editItem.submenu=edit;menu.addItem(editItem);NSApp.mainMenu=menu
  let engine=Bundle.main.resourceURL!.appendingPathComponent("engine")
  DispatchQueue.global().async {
   let backend=Process(), pipe=Pipe(), errors=Pipe()
   backend.executableURL=engine.appendingPathComponent("LinkoService");backend.arguments=[]
   var environment=ProcessInfo.processInfo.environment
   environment["LINKO_APP_PATH"]=Bundle.main.bundleURL.path
   environment["LINKO_APP_PID"]=String(ProcessInfo.processInfo.processIdentifier)
   backend.environment=environment
   backend.currentDirectoryURL=engine;backend.standardOutput=pipe;backend.standardError=errors
   do {
    try backend.run();self.process=backend
    var line=Data()
    while line.count<4096 {let part=pipe.fileHandleForReading.readData(ofLength:1);if part.isEmpty||part==Data([10]){break};line.append(part)}
    guard let value=try JSONSerialization.jsonObject(with:line) as? [String:String],let address=value["url"],let url=URL(string:address),url.host=="127.0.0.1",url.scheme=="http" else {throw NSError(domain:"Linko",code:1)}
    DispatchQueue.main.async {self.authority=url.host!+":"+String(url.port!);self.view.load(URLRequest(url:url))}
   }catch {DispatchQueue.main.async{let alert=NSAlert();alert.messageText="邻控暂时无法启动";alert.informativeText="本地连接服务没有正常启动。原版配对和安装不受影响。";alert.runModal()}}
  }
 }
 func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
  guard message.name=="linkoUpdate",message.body as? String=="installing",message.frameInfo.isMainFrame,
   let url=message.frameInfo.request.url,url.host=="127.0.0.1",url.scheme=="http",
   url.host!+":"+String(url.port ?? 80)==authority else{return}
  NSApp.terminate(nil)
 }
 func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
  guard let url=navigationAction.request.url,url.scheme=="http",url.host=="127.0.0.1",url.host!+":"+String(url.port ?? 80)==authority else{decisionHandler(.cancel);return};decisionHandler(.allow)
 }
 @objc func about(){let alert=NSAlert();alert.messageText="邻控";let version=Bundle.main.object(forInfoDictionaryKey:"LinkoReleaseVersion") as? String ?? "预览版";alert.informativeText=version+"\n每台电脑，都能彼此连接。";alert.runModal()}
 @objc func quit(){NSApp.terminate(nil)}
 func applicationWillTerminate(_ notification:Notification){if let process=process,process.isRunning{process.terminate()}}
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{window.makeKeyAndOrderFront(nil);return true}
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return true}
}
let app=NSApplication.shared
let delegate=UnifiedApp();app.delegate=delegate;app.run()
