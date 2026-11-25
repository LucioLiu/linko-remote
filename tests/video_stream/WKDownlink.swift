import Cocoa
import WebKit
import Vision

final class Harness: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var view: WKWebView!
    var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(self, name: "linkoUpdate")
        config.userContentController.add(self, name: "wanMeasurement")
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1220, height: 860), configuration: config)
        view.navigationDelegate = self
        view.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
        DispatchQueue.main.asyncAfter(deadline: .now() + 50) { exit(1) }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "wanMeasurement",
              let data = try? JSONSerialization.data(withJSONObject: message.body),
              let line = String(data: data, encoding: .utf8) else { return }
        print(line)
        fflush(stdout)
    }

    func webView(_ view: WKWebView, didFinish navigation: WKNavigation!) {
        let script = """
        (() => {
          const duration = \(CommandLine.arguments[3]);
          const post = (kind, fields={}) => webkit.messageHandlers.wanMeasurement.postMessage(
            {kind, t_ms: performance.timeOrigin+performance.now(), ...fields});
          const originalDecode = decodeLiveAvc, originalPresent = presentLiveFrame;
          let pixelAt = -Infinity, qualityAt = -Infinity, finished = false;
          decodeLiveAvc = function(session, frame, capturedAt) {
            post('receive', {seq: frame.seq, idr: frame.idr, estimated_age_ms: performance.now()-capturedAt});
            return originalDecode.apply(this, arguments);
          };
          const firstDraw = new WeakSet();
          const originalJpeg = requestLiveJpeg;
          requestLiveJpeg = function(session) {
            post('viewer_fallback', {phase: firstDraw.has(session) ? 'after_draw' : 'before_draw', trigger: new Error().stack});
            return originalJpeg.apply(this, arguments);
          };
          const pixelCanvas = document.createElement('canvas');
          pixelCanvas.width = 32; pixelCanvas.height = 18;
          const textCanvas = document.createElement('canvas');
          textCanvas.width = 424; textCanvas.height = 104;
          const textContext = textCanvas.getContext('2d');
          const pixelContext = pixelCanvas.getContext('2d', {willReadFrequently: true});
          originalPresent && (presentLiveFrame = function(session, frame, capturedAt) {
            const result = originalPresent.apply(this, arguments);
            const now = performance.now();
            if (frame.codec === 'H.264') firstDraw.add(session);
            post('draw', {seq: frame.seq, codec: frame.codec, estimated_age_ms: now-capturedAt});
            if (now-pixelAt >= 965) {
              pixelAt = now;
              pixelContext.drawImage(liveVideo, 0, 0, 32, 18);
              const pixels = pixelContext.getImageData(0,0,32,18).data;
              let hash = 2166136261;
              for (const byte of pixels) hash = Math.imul(hash ^ byte, 16777619);
              post('pixel', {seq:frame.seq, hash:(hash>>>0).toString(16), width:liveVideo.width, height:liveVideo.height});
            }
            if (frame.codec === 'H.264' && now-qualityAt >= 2600) {
              qualityAt = now;
              const x = liveVideo.width / 1280, y = liveVideo.height / 720;
              textContext.drawImage(liveVideo, 8*x, 52*y, 424*x, 104*y, 0, 0, 424, 104);
              post('text_sample', {seq:frame.seq, png:textCanvas.toDataURL('image/png').split(',')[1]});
            }
            return result;
          });
          const originalFetch = window.fetch;
          window.fetch = async function(path, ...args) {
            const reply = await originalFetch.call(this, path, ...args);
            if (path === '/api/live-open' || path === '/api/live-input') {
              const report = await reply.clone().json();
              if (path.endsWith('open')) post('open', {ok:report.ok, avc:report.value?.live_avc ?? 0});
              else post('input_accepted', {ok:report.ok, seq:report.value?.accepted ?? 0});
            }
            return reply;
          };
          let previousAck=0, pending=0, nextProbe=performance.now()+900, start=performance.now();
          const holdLocalMediaMs = \(CommandLine.arguments.count > 4 ? CommandLine.arguments[4] : "0");
          let delayedMedia, mediaDue;
          if (holdLocalMediaMs > 0) {
            const originalWatch = watchLiveMedia;
            watchLiveMedia = function(session) {
              mediaDue = performance.now() + holdLocalMediaMs;
              delayedMedia = () => { if (live === session) originalWatch(session); };
            };
          }
          window.wanTick=() => {
            const now=performance.now();
            if (delayedMedia && now >= mediaDue) {
              post('release_local_media');
              delayedMedia();
              delayedMedia = null;
            }
            if (live?.ack > previousAck) {
              previousAck=live.ack;
              post('input_ack', {seq:previousAck});
              if (previousAck >= pending) pending=0;
            }
            if (pending && live && live.seq < pending && !live.queue.length && !live.sending) {
              post('input_dropped', {seq:pending});
              pending=0;
            }
            if (!pending && now>=nextProbe && now-start<(duration-2)*1000 && live?.frame?.codec==='H.264' && now-live.frameAt<1150) {
              const seq=live.seq+1;
              queueLive({type:'move',x:seq,y:1});
              if (live.queue.length) {
                pending=seq; nextProbe=now+1050;
                post('input_queue', {seq});
              }
            }
            if (now-start >= duration*1000 && !finished) {
              finished=true;
              window.wanTick=null;
              post('window_end', {state:$('remote-state').textContent, ack:previousAck, pending, active:!!live, jpeg:!!live?.jpeg, halt:!!live?.halt});
              stopLive().then(() => post('done'), () => post('close_error'));
            }
          };
          selectedPeer={id:'\(CommandLine.arguments[2])'};
          $('remote-dialog').showModal();
          post('start');
          startLive();
        })()
        """
        view.evaluateJavaScript(script) { _, error in
            if error != nil { print("{\"kind\":\"javascript_error\"}"); fflush(stdout); exit(2) }
            self.timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
                self.view.evaluateJavaScript("if (window.wanTick) window.wanTick();") { _, error in
                    if error != nil {
                        print("{\"kind\":\"tick_error\"}")
                        fflush(stdout)
                    }
                }
            }
        }
    }
}

if CommandLine.arguments[1] == "--ocr" {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    let encoded = try JSONDecoder().decode([String].self, from: input)
    let results = try encoded.map { sample -> [String] in
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        let handler = VNImageRequestHandler(data: Data(base64Encoded: sample)!)
        try handler.perform([request])
        return request.results?.compactMap { $0.topCandidates(1).first?.string } ?? []
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(results))
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let delegate = Harness()
app.delegate = delegate
app.run()
