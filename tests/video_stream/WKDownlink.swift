import Cocoa
import WebKit
import Vision

final class Harness: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var view: WKWebView!
    var timer: Timer?
    let quality = CommandLine.arguments[2] == "--quality"

    func applicationDidFinishLaunching(_ notification: Notification) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(self, name: "linkoUpdate")
        config.userContentController.add(self, name: "wanMeasurement")
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1220, height: 860), configuration: config)
        view.navigationDelegate = self
        view.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
        DispatchQueue.main.asyncAfter(deadline: .now() + (quality ? 90 : 50)) { exit(1) }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "wanMeasurement",
              let data = try? JSONSerialization.data(withJSONObject: message.body),
              let line = String(data: data, encoding: .utf8) else { return }
        print(line)
        fflush(stdout)
    }

    func webView(_ view: WKWebView, didFinish navigation: WKNavigation!) {
        if quality {
            evaluateQuality()
            return
        }
        let script = """
        (() => {
          const duration = \(CommandLine.arguments[3]);
          const post = (kind, fields={}) => webkit.messageHandlers.wanMeasurement.postMessage(
            {kind, t_ms: performance.timeOrigin+performance.now(), ...fields});
          const originalDecode = decodeLiveAvc, originalPresent = presentLiveFrame;
          let pixelAt = -Infinity, finished = false;
          const qualityAt = new Map(), frameKinds = new Map();
          decodeLiveAvc = function(session, frame, capturedAt) {
            frameKinds.set(frame.seq, frame.idr);
            if (frameKinds.size>32) frameKinds.delete(frameKinds.keys().next().value);
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
            const idr = frameKinds.get(frame.seq);
            if (frame.codec === 'H.264' && idr !== undefined && now-(qualityAt.get(idr) ?? -Infinity) >= 2600) {
              qualityAt.set(idr, now);
              const x = liveVideo.width / 1280, y = liveVideo.height / 720;
              textContext.drawImage(liveVideo, 8*x, 52*y, 424*x, 104*y, 0, 0, 424, 104);
              post('text_sample', {seq:frame.seq, idr, png:textCanvas.toDataURL('image/png').split(',')[1]});
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

    func evaluateQuality() {
        let script = """
        window.qualityLoad = async () => {
          const fixture = await fetch('/frames').then(r => r.json());
          $('remote-dialog').showModal();
          live = {pendingFrames: new Map(), queue: []};
          const present = presentLiveFrame;
          let drawn;
          presentLiveFrame = function(session, frame, capturedAt) {
            present.apply(this, arguments);
            drawn?.(frame.seq);
          };
          let previous;
          window.qualityFrame = async (index) => {
            const frame = fixture[index];
            if (frame.seq === 1 && previous) {
              closeLiveDecoder(live);
              live.frame = null;
            }
            if (frame.seq !== (previous ?? 0) + 1 && frame.seq !== 1) throw Error('Frame order');
            previous = frame.seq;
            await new Promise((resolve, reject) => {
              const timer = setTimeout(() => reject(Error('Missing draw for ' + frame.seq)), 2000);
              drawn = (seq) => {
                clearTimeout(timer);
                if (seq !== frame.seq) reject(Error('Wrong draw sequence'));
                resolve();
              };
              decodeLiveAvc(live, frame, performance.now());
            });
            if (!frame.sample) return false;
            const r = liveVideo.getBoundingClientRect();
            const crop = document.createElement('canvas');
            crop.width = 424; crop.height = 104;
            const x = liveVideo.width / 1280, y = liveVideo.height / 720;
            crop.getContext('2d').drawImage(liveVideo, 8*x, 52*y, 424*x, 104*y, 0, 0, 424, 104);
            return {seq: frame.seq, textPng: crop.toDataURL().split(',')[1], png: liveVideo.toDataURL().split(',')[1],
              width:liveVideo.width, height:liveVideo.height, dpr:devicePixelRatio,
              viewport:[innerWidth,innerHeight], rect:[r.x,r.y,r.width,r.height],
              fit:getComputedStyle(liveVideo).objectFit};
          };
          return fixture.length;
        };
        true;
        """
        view.evaluateJavaScript(script) { _, error in
            if let error { self.qualityFail(error) }
            self.view.callAsyncJavaScript("return await window.qualityLoad();", arguments: [:],
                                         in: nil, in: .page) { result in
                switch result {
                case .success(let value): self.nextQualityFrame(0, count: value as! Int)
                case .failure(let error): self.qualityFail(error)
                }
            }
        }
    }

    func qualityFail(_ error: Error) {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(2)
    }

    func nextQualityFrame(_ index: Int, count: Int) {
        if index == count { exit(0) }
        view.callAsyncJavaScript("return await window.qualityFrame(index);", arguments: ["index": index],
                                 in: nil, in: .page) { result in
            switch result {
            case .success(let value):
                guard var frame = value as? [String: Any] else {
                    self.nextQualityFrame(index + 1, count: count)
                    return
                }
                let dpr = frame["dpr"] as! Double
                self.qualitySnapshot(scale: 1 / dpr) { png in
                    frame["snapshot_1x"] = png
                    self.qualitySnapshot(scale: 1) { png in
                        frame["snapshot_dpr"] = png
                        let data = try! JSONSerialization.data(withJSONObject: frame)
                        print(String(decoding: data, as: UTF8.self))
                        fflush(stdout)
                        self.nextQualityFrame(index + 1, count: count)
                    }
                }
            case .failure(let error): self.qualityFail(error)
            }
        }
    }

    func qualitySnapshot(scale: Double, completion: @escaping (String) -> Void) {
        let config = WKSnapshotConfiguration()
        config.rect = view.bounds
        config.snapshotWidth = NSNumber(value: view.bounds.width * scale)
        config.afterScreenUpdates = true
        view.takeSnapshot(with: config) { image, error in
            if let error { self.qualityFail(error) }
            let tiff = image!.tiffRepresentation!
            let png = NSBitmapImageRep(data: tiff)!.representation(using: .png, properties: [:])!
            completion(png.base64EncodedString())
        }
    }

}

if ["--ocr", "--ocr-boxes"].contains(CommandLine.arguments[1]) {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    let encoded = try JSONDecoder().decode([String].self, from: input)
    struct Observation: Encodable {
        let text: String
        let box: [CGFloat]
    }
    let results = try encoded.map { sample -> [Observation] in
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        if #available(macOS 13.0, *) { request.automaticallyDetectsLanguage = false }
        let handler = VNImageRequestHandler(data: Data(base64Encoded: sample)!)
        try handler.perform([request])
        return request.results?.compactMap { observation in
            guard let text = observation.topCandidates(1).first?.string else { return nil }
            let r = observation.boundingBox
            return Observation(text: text, box: [r.minX, 1 - r.maxY, r.width, r.height])
        } ?? []
    }
    let encoder = JSONEncoder()
    let data = CommandLine.arguments[1] == "--ocr-boxes"
        ? try encoder.encode(results) : try encoder.encode(results.map { $0.map(\.text) })
    FileHandle.standardOutput.write(data)
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let delegate = Harness()
app.delegate = delegate
app.run()
