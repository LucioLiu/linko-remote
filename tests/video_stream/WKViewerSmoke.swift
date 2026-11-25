import Cocoa
import WebKit

final class Harness: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var view: WKWebView!
    var timer: Timer?
    func applicationDidFinishLaunching(_ notification: Notification) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(self, name: "linkoUpdate")
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1220, height: 860), configuration: config)
        view.navigationDelegate = self
        view.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
        DispatchQueue.main.asyncAfter(deadline: .now() + 18) { exit(1) }
    }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {}
    func webView(_ view: WKWebView, didFinish navigation: WKNavigation!) {
        view.evaluateJavaScript("selectedPeer={id:'"+CommandLine.arguments[2]+"'}; $('remote-dialog').showModal(); startLive(); JSON.stringify([typeof VideoDecoder, typeof EncodedVideoChunk])") {
            print("START", $0 ?? "", $1 as Any); fflush(stdout)
        }
        var stage = 0
        var observedLuma = Set<Int>()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            self.view.evaluateJavaScript("""
                JSON.stringify({frame:live?.frame, geometry:live?.frame?.geometry, ack:live?.ack, status:$('remote-state').textContent, visible:!$('remote-video').classList.contains('hidden'), pixel:[...$('remote-video').getContext('2d').getImageData(0,0,1,1).data]})
                """) { value, error in
                    guard let json=value as? String, let bytes=json.data(using:.utf8),
                          let result=(try? JSONSerialization.jsonObject(with: bytes)) as? [String:Any]
                    else { print(error as Any); return }
                    print(json); fflush(stdout)
                    let frame=result["frame"] as? [String:Any]
                    let codec=frame?["codec"] as? String
                    if codec == "H.264", let luma = (result["pixel"] as? [Int])?.first {
                        observedLuma.insert(luma)
                    }
                    if stage == 0 && observedLuma.count >= 2 {
                        stage=1
                    } else if stage == 1 && (result["status"] as? String ?? "").contains("键鼠已暂停") {
                        stage=2
                        self.view.evaluateJavaScript("queueLive({type:'move',x:1,y:1}); [live.queue.length,live.seq]") { value, error in
                            print("STALE INPUT", value as Any, error as Any); fflush(stdout)
                            guard (value as? [Int]) == [0,0] else { exit(2) }
                        }
                    } else if stage == 2 && codec == "JPEG" {
                        let pixel=result["pixel"] as? [Int] ?? []
                        guard pixel.count==4, pixel[0] > 150, pixel[1]<80,
                              result["visible"] as? Bool == true else { exit(3) }
                        stage=3
                        self.view.evaluateJavaScript("queueLive({type:'move',x:1,y:1}); true") {_,_ in}
                    } else if stage == 3 && result["ack"] as? Int == 1 {
                        exit(0)
                    }
                }
        }
    }
}
let app=NSApplication.shared
app.setActivationPolicy(.prohibited)
let delegate=Harness()
app.delegate=delegate
app.run()
