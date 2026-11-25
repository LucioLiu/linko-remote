import Cocoa
import UniformTypeIdentifiers
import WebKit

final class UnifiedApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKScriptMessageHandlerWithReply, WKDownloadDelegate {
 var window: NSWindow!
 var view: WKWebView!
 var process: Process?
 var authority: String?
 private struct SaveDestination {
  let temporary: URL
  let final: URL
  let replace: Bool
 }
 private var fileDownloads: [ObjectIdentifier: SaveDestination] = [:]
 func applicationDidFinishLaunching(_ notification: Notification) {
  NSApp.setActivationPolicy(.regular)
  window=NSWindow(contentRect:NSRect(x:0,y:0,width:1220,height:860),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
  window.title="邻控";window.minSize=NSSize(width:860,height:680);window.center();window.isReleasedWhenClosed=false
  let configuration=WKWebViewConfiguration()
  configuration.userContentController.add(self,name:"linkoUpdate")
  configuration.userContentController.addScriptMessageHandler(self,contentWorld:.page,name:"linkoInvite")
  view=WKWebView(frame:window.contentView!.bounds,configuration:configuration);view.navigationDelegate=self;view.uiDelegate=self;view.autoresizingMask=[.width,.height];window.contentView=view
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
 func isLocalMainFrame(_ frame: WKFrameInfo) -> Bool {
  guard frame.isMainFrame, let url=frame.request.url, url.host=="127.0.0.1", url.scheme=="http", let port=url.port else {return false}
  return "127.0.0.1:\(port)" == authority
 }
 func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
  guard message.name=="linkoUpdate",message.body as? String=="installing",isLocalMainFrame(message.frameInfo) else{return}
  NSApp.terminate(nil)
 }
 func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage, replyHandler: @escaping (Any?, String?) -> Void) {
  guard message.name=="linkoInvite", isLocalMainFrame(message.frameInfo), let content=message.body as? String,
        let data=content.data(using:.utf8), data.count<=4096 else {
   replyHandler(nil,"邀请文件内容无效。");return
  }
  let panel=NSSavePanel()
  panel.nameFieldStringValue="邻控邀请.linko"
  if let type=UTType(filenameExtension:"linko"){panel.allowedContentTypes=[type]}
  panel.beginSheetModal(for:window){result in
   guard result == .OK, let url=panel.url else {replyHandler(false,nil);return}
   do {
    try data.write(to:url,options:.atomic)
    try? FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:url.path)
    replyHandler(true,nil)
   }
   catch {replyHandler(nil,"邀请文件保存失败。")}
  }
 }
 func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
  guard isLocalMainFrame(frame) else {completionHandler(nil);return}
  let panel=NSOpenPanel()
  panel.canChooseDirectories=false
  panel.allowsMultipleSelection=parameters.allowsMultipleSelection
  panel.beginSheetModal(for:window){result in completionHandler(result == .OK ? panel.urls : nil)}
 }
 func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
  guard let url=navigationAction.request.url,url.scheme=="http",url.host=="127.0.0.1",url.host!+":"+String(url.port ?? 80)==authority else{decisionHandler(.cancel);return}
  decisionHandler(navigationAction.shouldPerformDownload && isFileDownload(url) ? .download : .allow)
 }
 private func isFileDownload(_ url: URL?) -> Bool {
  guard let url, url.scheme == "http", url.host == "127.0.0.1",
        let port = url.port, "127.0.0.1:\(port)" == authority,
        url.query == nil else { return false }
  return url.path.range(of: #"^/file-download/[A-Za-z0-9_-]{43}$"#, options: .regularExpression) != nil
 }
 func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
  guard isFileDownload(response.response.url), let http = response.response as? HTTPURLResponse else {
   decisionHandler(.allow)
   return
  }
  decisionHandler(http.statusCode == 200 ? .download : .cancel)
 }
 func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
  download.delegate = self
 }
 func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
  download.delegate = self
 }
 func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
               completionHandler: @escaping (URL?) -> Void) {
  guard isFileDownload(response.url), (response as? HTTPURLResponse)?.statusCode == 200 else {
   completionHandler(nil)
   showSaveError()
   return
  }
  let panel = NSSavePanel()
  panel.nameFieldStringValue = (suggestedFilename as NSString).lastPathComponent
  panel.canCreateDirectories = true
  panel.beginSheetModal(for: window) { result in
   guard result == .OK, let destination = panel.url else { completionHandler(nil); return }
   let temporary = destination.deletingLastPathComponent().appendingPathComponent(".linko-\(UUID().uuidString).part")
   self.fileDownloads[ObjectIdentifier(download)] = SaveDestination(
    temporary: temporary, final: destination, replace: FileManager.default.fileExists(atPath: destination.path))
   completionHandler(temporary)
  }
 }
 func downloadDidFinish(_ download: WKDownload) {
  guard let saved = fileDownloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
  let files = FileManager.default
  do {
   if saved.replace {
    _ = try files.replaceItemAt(saved.final, withItemAt: saved.temporary)
   } else {
    try files.moveItem(at: saved.temporary, to: saved.final)
   }
  } catch {
   try? files.removeItem(at: saved.temporary)
   showSaveError()
  }
 }
 func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
  if let saved = fileDownloads.removeValue(forKey: ObjectIdentifier(download)) {
   try? FileManager.default.removeItem(at: saved.temporary)
   showSaveError()
  }
 }
 private func showSaveError() {
  let alert = NSAlert()
  alert.messageText = "文件保存失败"
  alert.informativeText = "请回到文件传输记录重新选择保存位置。"
  alert.beginSheetModal(for: window)
 }
 @objc func about(){let alert=NSAlert();alert.messageText="邻控";let version=Bundle.main.object(forInfoDictionaryKey:"LinkoReleaseVersion") as? String ?? "预览版";alert.informativeText=version+"\n每台电脑，都能彼此连接。";alert.runModal()}
 @objc func quit(){NSApp.terminate(nil)}
 func applicationWillTerminate(_ notification: Notification) {
  for saved in fileDownloads.values { try? FileManager.default.removeItem(at: saved.temporary) }
  if let process, process.isRunning { process.terminate() }
 }
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{window.makeKeyAndOrderFront(nil);return true}
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return true}
}
let app=NSApplication.shared
let delegate=UnifiedApp();app.delegate=delegate;app.run()
