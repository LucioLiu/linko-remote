import AppKit
import ApplicationServices
import ScreenCaptureKit
import CryptoKit

struct DesktopFailure: Error { let text: String }
func fail(_ message: String) throws -> Never { throw DesktopFailure(text: message) }
func writeJSON(_ value: Any, _ path: URL? = nil) throws {
 let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
 if let path = path { try data.write(to: path, options: .atomic) }
 else { FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10])) }
}

struct DesktopFrame {
 let id: String
 let created: TimeInterval
 let pixels: Data
 let width: Int
 let height: Int
 let owner: pid_t
 let window: [Int]
 let focus: CFHashCode
 let input: [UInt32]
}

@available(macOS 14.0, *)
final class PeerDesktop {
 let workspace: URL
 var frame: DesktopFrame?
 let session = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
 let display = CGMainDisplayID()
 let inputTypes: [CGEventType] = [.mouseMoved, .leftMouseDown, .rightMouseDown, .keyDown, .scrollWheel]
 init(_ path: String) { workspace = URL(fileURLWithPath: path, isDirectory: true) }
 var stopped: Bool { FileManager.default.fileExists(atPath: workspace.appendingPathComponent("stop").path) }
 func permissions() -> [String: Any] { ["screen": CGPreflightScreenCaptureAccess(), "input": AXIsProcessTrusted(), "platform": "macos"] }
 func ensureAccess() throws {
  guard !stopped else { try fail("WORKER_STOPPING_BEFORE_INPUT") }
  guard CGPreflightScreenCaptureAccess() else { try fail("SCREEN_RECORDING_PERMISSION_REQUIRED") }
  guard AXIsProcessTrusted() else { try fail("ACCESSIBILITY_PERMISSION_REQUIRED") }
  if let dictionary = CGSessionCopyCurrentDictionary() as? [String: Any], dictionary["CGSSessionScreenIsLocked"] as? Bool == true { try fail("LOCKED_OR_SECURE_DESKTOP") }
 }
 func inputToken() -> [UInt32] { inputTypes.map { CGEventSource.counterForEventType(.combinedSessionState, eventType: $0) } }
 func focus() throws -> CFHashCode {
  var value: CFTypeRef?
  guard AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute as CFString, &value) == .success, let value = value else { try fail("FOCUS_STATE_UNKNOWN") }
  return CFHash(value)
 }
 func foreground(_ width: Int, _ height: Int) throws -> (pid_t, [Int], String) {
  guard let app = NSWorkspace.shared.frontmostApplication,
        let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]],
        let window = windows.first(where: { ($0[kCGWindowOwnerPID as String] as? Int) == Int(app.processIdentifier) && ($0[kCGWindowLayer as String] as? Int) == 0 }),
        let bounds = window[kCGWindowBounds as String] as? [String: Any],
        let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary) else { try fail("FOREGROUND_WINDOW_UNKNOWN") }
  let screen = CGDisplayBounds(display)
  let intersection = rect.intersection(screen)
  guard !intersection.isNull && intersection.width > 0 && intersection.height > 0 else { try fail("WINDOW_NOT_ON_SELECTED_DISPLAY") }
  let sx = Double(width) / screen.width, sy = Double(height) / screen.height
  let region = [Int((intersection.minX - screen.minX) * sx), Int((intersection.minY - screen.minY) * sy), Int(intersection.width * sx), Int(intersection.height * sy)]
  return (app.processIdentifier, region, window[kCGWindowName as String] as? String ?? app.localizedName ?? "Mac")
 }
 func capture(showsCursor: Bool = false) async throws -> (CGImage, Data) {
  guard CGPreflightScreenCaptureAccess() else { try fail("SCREEN_RECORDING_PERMISSION_REQUIRED") }
  let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
  guard let chosen = content.displays.first(where: { $0.displayID == display }) else { try fail("DISPLAY_UNAVAILABLE") }
  let config = SCStreamConfiguration()
  config.width = CGDisplayPixelsWide(display); config.height = CGDisplayPixelsHigh(display)
  config.showsCursor = showsCursor
  let image = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(display: chosen, excludingWindows: []), configuration: config)
  let count = image.width * image.height * 4
  var pixels = Data(count: count)
  let rendered = pixels.withUnsafeMutableBytes { buffer -> Bool in
   guard let context = CGContext(data: buffer.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
   context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height)); return true
  }
  guard rendered else { try fail("SCREEN_CAPTURE_FAILED") }
  return (image, pixels)
 }
 func observe() async throws -> [String: Any] {
  try ensureAccess()
  let before = inputToken(), focused = try focus()
  let width = CGDisplayPixelsWide(display), height = CGDisplayPixelsHigh(display)
  let (owner, region, title) = try foreground(width, height)
  let (image, pixels) = try await capture()
  guard before == inputToken(), focused == (try focus()), owner == NSWorkspace.shared.frontmostApplication?.processIdentifier else { try fail("DESKTOP_CHANGED_DURING_CAPTURE") }
  let after = try foreground(image.width, image.height)
  guard after.1 == region else { try fail("WINDOW_MOVED_DURING_CAPTURE") }
  let identifier = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
  frame = DesktopFrame(id: identifier, created: ProcessInfo.processInfo.systemUptime, pixels: pixels, width: image.width, height: image.height, owner: owner, window: region, focus: focused, input: before)
  guard let encoded = NSBitmapImageRep(cgImage: image).representation(using: .jpeg, properties: [.compressionFactor: 0.86]) else { try fail("SCREEN_ENCODING_FAILED") }
  return ["frame_id": identifier, "session": session, "bounds": [0,0,image.width,image.height], "foreground_bounds": region,
          "title": title, "platform": "macos", "input_enabled": true, "target_guard": true, "jpeg_base64": encoded.base64EncodedString()]
 }
 func unchanged(_ before: DesktopFrame, _ after: Data, _ region: [Int]) -> Bool {
  let left = max(0, region[0]), top = max(0, region[1])
  let right = min(before.width, region[0] + region[2]), bottom = min(before.height, region[1] + region[3])
  guard right > left && bottom > top && before.pixels.count == after.count else { return false }
  for y in top..<bottom {
   let start = (y * before.width + left) * 4, end = (y * before.width + right) * 4
   if before.pixels[start..<end] != after[start..<end] { return false }
  }
  return true
 }
 func keyCode(_ name: String) -> CGKeyCode? {
  let map: [String: CGKeyCode] = ["A":0,"S":1,"D":2,"F":3,"H":4,"G":5,"Z":6,"X":7,"C":8,"V":9,"B":11,"Q":12,"W":13,"E":14,"R":15,"Y":16,"T":17,"1":18,"2":19,"3":20,"4":21,"6":22,"5":23,"9":25,"7":26,"8":28,"0":29,"O":31,"U":32,"I":34,"P":35,"ENTER":36,"L":37,"J":38,"K":40,"N":45,"M":46,"TAB":48,"SPACE":49,"BACKSPACE":51,"ESC":53,"LEFT":123,"RIGHT":124,"DOWN":125,"UP":126,"HOME":115,"END":119,"DELETE":117,"PAGEUP":116,"PAGEDOWN":121,"F4":118,"F5":96]
  return map[name]
 }
 func postKey(_ code: CGKeyCode, _ flags: CGEventFlags = []) throws {
  guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true), let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { try fail("INPUT_EVENT_FAILED") }
  down.flags = flags; up.flags = flags; down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
 }
 func process(_ identifier: String, _ request: [String: Any]) async -> [String: Any] {
  var attempted = false
  do {
   try ensureAccess()
   guard let expires = request["expires"] as? Double, expires >= Date().timeIntervalSince1970, expires <= Date().timeIntervalSince1970 + 120 else { try fail("REQUEST_EXPIRED_OR_CLOCK_MISMATCH") }
   let action = request["action"] as? String ?? ""
   if action == "observe" { return ["ok":true,"input_attempted":false,"request_id":identifier,"action":action,"result":try await observe()] }
   if action == "status" { return ["ok":true,"input_attempted":false,"request_id":identifier,"result":["platform":"macos","input_enabled":true,"version":"0.8.0-unified-preview"]] }
   guard ["click","doubleclick","text","key","scroll"].contains(action) else { try fail("UNSUPPORTED_MAC_ACTION") }
   guard let current = frame, request["frame_id"] as? String == current.id, ProcessInfo.processInfo.systemUptime-current.created <= 60 else { try fail("STALE_FRAME_OBSERVE_AGAIN") }
   let initial = try foreground(current.width, current.height)
   guard current.owner == initial.0, current.window == initial.1, current.focus == (try focus()), current.input == inputToken() else { try fail("USER_OR_WINDOW_CHANGED_OBSERVE_AGAIN") }
   let (_, pixels) = try await capture()
   var region = current.window
   var point: CGPoint?
   if action == "click" || action == "doubleclick" {
    guard let x = request["x"] as? Int, let y = request["y"] as? Int, x>=0,y>=0,x<current.width,y<current.height else { try fail("OUT_OF_FRAME") }
    let displayBounds = CGDisplayBounds(display)
    point = CGPoint(x: displayBounds.minX + Double(x)*displayBounds.width/Double(current.width), y: displayBounds.minY + Double(y)*displayBounds.height/Double(current.height))
    region = [x-80,y-80,160,160]
   }
   guard unchanged(current, pixels, region) else { try fail("TARGET_REGION_CHANGED_OBSERVE_AGAIN") }
   guard current.input == inputToken(), current.focus == (try focus()), NSWorkspace.shared.frontmostApplication?.processIdentifier == current.owner else { try fail("INPUT_OWNER_CHANGED_DURING_GUARD") }
   try ensureAccess()
   frame = nil; attempted = true
   switch action {
   case "click", "doubleclick":
    let right = request["button"] as? String == "right"
    let button: CGMouseButton = right ? .right : .left
    let down: CGEventType = right ? .rightMouseDown : .leftMouseDown, up: CGEventType = right ? .rightMouseUp : .leftMouseUp
    guard let point = point else { try fail("OUT_OF_FRAME") }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
    for index in 1...(action == "doubleclick" ? 2 : 1) {
     for type in [down,up] {
      guard let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: button) else { try fail("INPUT_EVENT_FAILED") }
      event.setIntegerValueField(.mouseEventClickState, value: Int64(index)); event.post(tap: .cghidEventTap)
     }
    }
   case "text":
    guard let text = request["text"] as? String, text.utf16.count <= 2000 else { try fail("INVALID_TEXT") }
    for unit in text.utf16 {
     guard !stopped, NSWorkspace.shared.frontmostApplication?.processIdentifier == current.owner, current.focus == (try focus()) else { try fail("TEXT_TARGET_CHANGED_PARTIAL_INPUT_DO_NOT_RETRY") }
     if unit == 10 || unit == 13 { try postKey(36); continue }
     guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true), let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else { try fail("INPUT_EVENT_FAILED") }
     var character = unit
     down.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
     up.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
     down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
     try await Task.sleep(nanoseconds: 15_000_000)
    }
   case "key":
    guard let keys = request["keys"] as? [String], !keys.isEmpty, keys.count <= 4 else { try fail("INVALID_KEY") }
    var flags: CGEventFlags = [], codes: [CGKeyCode] = []
    for key in keys {
     switch key { case "CTRL": flags.insert(.maskControl); case "META": flags.insert(.maskCommand); case "ALT": flags.insert(.maskAlternate); case "SHIFT": flags.insert(.maskShift); default: guard let code = keyCode(key) else { try fail("INVALID_KEY") }; codes.append(code) }
    }
    for code in codes { try postKey(code,flags) }
   case "scroll":
    guard let amount = request["amount"] as? Int, amount != 0, abs(amount) <= 10, let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: Int32(amount), wheel2: 0, wheel3: 0) else { try fail("INVALID_SCROLL") }
    event.post(tap: .cghidEventTap)
   default: try fail("UNSUPPORTED_MAC_ACTION")
   }
   try await Task.sleep(nanoseconds: 350_000_000)
   return ["ok":true,"input_attempted":true,"request_id":identifier,"action":action,"result":try await observe()]
  } catch {
   frame = nil
   return ["ok":false,"input_attempted":attempted,"request_id":identifier,"error":(error as? DesktopFailure)?.text ?? "DESKTOP_OPERATION_UNCONFIRMED", "instruction":"Recover this request; do not replay uncertain input."]
  }
 }
 var liveID = "", liveSeq = 0, liveAck = 0
 var liveEnded = Set<String>(), liveKeys = Set<String>(), liveButtons = Set<Int>()
 var liveCaptured: TimeInterval = 0
 var liveFlags: CGEventFlags {
  var f: CGEventFlags = []
  for k in liveKeys { if k.hasPrefix("Shift") { f.insert(.maskShift) }; if k.hasPrefix("Control") { f.insert(.maskControl) }; if k.hasPrefix("Alt") { f.insert(.maskAlternate) }; if k.hasPrefix("Meta") { f.insert(.maskCommand) } }
  return f
 }
 func liveKeyCode(_ code: String) -> CGKeyCode? {
  let special: [String:CGKeyCode] = ["ShiftLeft":56,"ShiftRight":60,"ControlLeft":59,"ControlRight":62,"AltLeft":58,"AltRight":61,"MetaLeft":55,"MetaRight":54,"Minus":27,"Equal":24,"BracketLeft":33,"BracketRight":30,"Backslash":42,"Semicolon":41,"Quote":39,"Comma":43,"Period":47,"Slash":44,"Backquote":50,"F1":122,"F2":120,"F3":99,"F6":97,"F7":98,"F8":100,"F9":101,"F10":109,"F11":103,"F12":111]
  if let k = special[code] { return k }
  if code.hasPrefix("Key") { return keyCode(String(code.dropFirst(3))) }
  if code.hasPrefix("Digit") { return keyCode(String(code.dropFirst(5))) }
  return keyCode(code.replacingOccurrences(of:"Arrow",with:"").uppercased().replacingOccurrences(of:"ESCAPE",with:"ESC"))
 }
 func liveKey(_ code: String, _ down: Bool) throws {
  guard let key = liveKeyCode(code), let event = CGEvent(keyboardEventSource:nil, virtualKey:key, keyDown:down) else { try fail("UNSUPPORTED_KEY") }
  if down { liveKeys.insert(code) } else { liveKeys.remove(code) }
  event.flags = liveFlags; event.post(tap:.cghidEventTap)
 }
 func releaseLive() {
  for code in Array(liveKeys) { try? liveKey(code,false) }
  let point = CGEvent(source:nil)?.location ?? .zero
  for b in liveButtons { CGEvent(mouseEventSource:nil, mouseType:b == 0 ? .leftMouseUp : b == 2 ? .rightMouseUp : .otherMouseUp, mouseCursorPosition:point, mouseButton:b == 0 ? .left : b == 2 ? .right : .center)?.post(tap:.cghidEventTap) }
  liveButtons.removeAll()
 }
 func stopLive(_ reason: String) {
  releaseLive()
  if !liveID.isEmpty { liveEnded.insert(liveID); try? writeJSON(["id":liveID,"error":reason],workspace.appendingPathComponent("live-ended.json")) }
  liveID = ""
 }
 func liveTick() async {
  let controlURL = workspace.appendingPathComponent("live-control.json")
  guard let data = try? Data(contentsOf:controlURL), let control = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { return }
  do {
   guard let identifier = control["id"] as? String, identifier.range(of:"^[a-f0-9]{32}$",options:.regularExpression) != nil, let expires = control["expires"] as? Double, expires > Date().timeIntervalSince1970, expires < Date().timeIntervalSince1970+4, !stopped else { if !liveID.isEmpty { stopLive("连接已结束，按键已释放") }; return }
   if liveEnded.contains(identifier) { return }
   if liveID != identifier { if !liveID.isEmpty { stopLive("已切换会话") }; liveID=identifier; liveSeq=0; liveAck=0 }
   try ensureAccess()
   let width=CGDisplayPixelsWide(display), height=CGDisplayPixelsHigh(display)
   let geometry="0,0,\(width),\(height)"
   let files=try FileManager.default.contentsOfDirectory(at:workspace,includingPropertiesForKeys:nil).filter { $0.lastPathComponent.hasPrefix(identifier+"-") && $0.lastPathComponent.hasSuffix(".live-input.json") }.sorted { $0.lastPathComponent < $1.lastPathComponent }
   for file in files {
    let claimed=file.appendingPathExtension("executing"); try FileManager.default.moveItem(at:file,to:claimed)
    guard let batch=(try JSONSerialization.jsonObject(with:Data(contentsOf:claimed))) as? [String:Any], batch["id"] as? String == identifier, batch["seq"] as? Int == liveAck+1, batch["geometry"] as? String == geometry, let until=batch["expires"] as? Double, until > Date().timeIntervalSince1970, let events=batch["events"] as? [[String:Any]], events.count <= 64 else { try fail("INPUT_EXPIRED_OR_DISPLAY_CHANGED") }
    for e in events {
     try ensureAccess(); guard Date().timeIntervalSince1970 < expires && Date().timeIntervalSince1970 < until else { try fail("INPUT_LEASE_EXPIRED") }
     let kind=e["type"] as? String ?? ""
     if ["move","down","up"].contains(kind) {
      guard let x=e["x"] as? Int,let y=e["y"] as? Int, (0...65535).contains(x),(0...65535).contains(y) else { try fail("INVALID_POINTER") }
      let rect=CGDisplayBounds(display), b=e["button"] as? Int ?? 0
      let point=CGPoint(x:rect.minX+Double(x)*max(0,rect.width-1)/65535,y:rect.minY+Double(y)*max(0,rect.height-1)/65535)
      let button: CGMouseButton = b == 0 ? .left : b == 2 ? .right : .center
      let type: CGEventType
      if kind == "down" { liveButtons.insert(b); type=b == 0 ? .leftMouseDown : b == 2 ? .rightMouseDown : .otherMouseDown }
      else if kind == "up" { liveButtons.remove(b); type=b == 0 ? .leftMouseUp : b == 2 ? .rightMouseUp : .otherMouseUp }
      else { type=liveButtons.contains(0) ? .leftMouseDragged : liveButtons.contains(2) ? .rightMouseDragged : liveButtons.contains(1) ? .otherMouseDragged : .mouseMoved }
      let event=CGEvent(mouseEventSource:nil,mouseType:type,mouseCursorPosition:point,mouseButton:kind == "move" && liveButtons.contains(2) ? .right : button); event?.flags=liveFlags; event?.post(tap:.cghidEventTap)
     } else if kind == "keydown" || kind == "keyup" { try liveKey(e["code"] as? String ?? "",kind == "keydown") }
     else if kind == "release" { releaseLive() }
     else if kind == "wheel", let amount=e["amount"] as? Int { CGEvent(scrollWheelEvent2Source:nil,units:.line,wheelCount:1,wheel1:Int32(amount),wheel2:0,wheel3:0)?.post(tap:.cghidEventTap) }
     else if kind == "text",let text=e["text"] as? String,text.count<=500 {
      for var unit in text.utf16 { guard Date().timeIntervalSince1970 < until else { try fail("TEXT_EXPIRED_PARTIAL_DO_NOT_RETRY") }; let event=CGEvent(keyboardEventSource:nil,virtualKey:0,keyDown:true); event?.keyboardSetUnicodeString(stringLength:1,unicodeString:&unit); event?.post(tap:.cghidEventTap); let up=CGEvent(keyboardEventSource:nil,virtualKey:0,keyDown:false);up?.keyboardSetUnicodeString(stringLength:1,unicodeString:&unit);up?.post(tap:.cghidEventTap) }
     } else { try fail("INVALID_LIVE_EVENT") }
    }
    liveAck += 1;try writeJSON(["id":identifier,"seq":liveAck],workspace.appendingPathComponent("live-receipt.json"));try FileManager.default.removeItem(at:claimed)
   }
   if ProcessInfo.processInfo.systemUptime-liveCaptured > 0.12 {
    let (image,_)=try await capture(showsCursor:true);let scale=min(1,min(1280.0/Double(image.width),800.0/Double(image.height)))
    let w=max(1,Int(Double(image.width)*scale)), h=max(1,Int(Double(image.height)*scale))
    guard let context=CGContext(data:nil,width:w,height:h,bitsPerComponent:8,bytesPerRow:0,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue) else { try fail("CAPTURE_FAILED") }
    context.draw(image,in:CGRect(x:0,y:0,width:w,height:h))
    guard let scaled=context.makeImage(),let jpeg=NSBitmapImageRep(cgImage:scaled).representation(using:.jpeg,properties:[.compressionFactor:0.6]) else { try fail("ENCODING_FAILED") }
    liveSeq += 1;try writeJSON(["id":identifier,"seq":liveSeq,"geometry":geometry,"created":Date().timeIntervalSince1970,"jpeg_base64":jpeg.base64EncodedString()],workspace.appendingPathComponent("live-frame.json"));liveCaptured=ProcessInfo.processInfo.systemUptime
   }
  } catch { stopLive((error as? DesktopFailure)?.text ?? "连续桌面已停止") }
 }
 func run() async throws {
  try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true, attributes: [.posixPermissions:0o700])
  defer { releaseLive() }
  while !stopped {
   await liveTick()
   let files = try FileManager.default.contentsOfDirectory(at: workspace, includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasSuffix(".request.json") }.sorted { $0.lastPathComponent < $1.lastPathComponent }
   for file in files {
    let identifier = String(file.lastPathComponent.prefix(32))
    guard identifier.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { continue }
    let response = workspace.appendingPathComponent(identifier+".response.json")
    if FileManager.default.fileExists(atPath: response.path) { continue }
    let claimed = workspace.appendingPathComponent(identifier+".executing")
    if FileManager.default.fileExists(atPath: claimed.path) { continue }
    // Claim the request atomically before input. A crash leaves an uncertain
    // receipt and cannot cause the request to execute for a second time.
    try FileManager.default.moveItem(at: file, to: claimed)
    let request = try JSONSerialization.jsonObject(with: Data(contentsOf: claimed)) as? [String:Any] ?? [:]
    try writeJSON(await process(identifier,request),response)
   }
   try await Task.sleep(nanoseconds: liveID.isEmpty ? 150_000_000 : 15_000_000)
  }
 }
}

@main struct Main {
 static func main() async {
  if CommandLine.arguments.contains("--permissions") { try? writeJSON(["screen":CGPreflightScreenCaptureAccess(),"input":AXIsProcessTrusted(),"platform":"macos"]); return }
  if CommandLine.arguments.contains("--request-screen") { try? writeJSON(["allowed":CGRequestScreenCaptureAccess()]); return }
  if CommandLine.arguments.contains("--request-input") { let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String:true] as CFDictionary; try? writeJSON(["allowed":AXIsProcessTrustedWithOptions(options)]); return }
  guard #available(macOS 14.0,*), let index = CommandLine.arguments.firstIndex(of:"--workspace"), index+1 < CommandLine.arguments.count else { return }
  do { try await PeerDesktop(CommandLine.arguments[index+1]).run() } catch { /* Do not log screenshots or typed text. */ }
 }
}
